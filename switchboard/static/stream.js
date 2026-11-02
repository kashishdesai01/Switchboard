"use strict";

class StreamCall {
  constructor(onEnd) {
    this.onEnd = onEnd;
    this.sources = new Set();
    this.nextStart = 0;
    this.generation = 0;
    this.connected = false;
  }

  async open(token) {
    this.context = new AudioContext();
    await this.context.resume();
    await this.context.audioWorklet.addModule("/assets/capture.js");
    if (this.ended) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    this.socket = new WebSocket(`${scheme}://${location.host}/voice-lab`);
    this.socket.binaryType = "arraybuffer";
    if (token) this.socket.onopen = () => this.socket.send(token);
    this.socket.onmessage = ({ data }) => {
      if (this.ended) return;
      if (typeof data !== "string") {
        this.play(new Uint8Array(data));
        return;
      }
      const payload = JSON.parse(data);
      switch (payload.type) {
        case "ready":
          this.connected = true;
          $("backend").textContent = payload.mode;
          $("call-status").textContent = "Streaming audio";
          this.capture().catch(() => this.close("Microphone unavailable"));
          break;
        case "reply":
          message("Switchboard", payload.text);
          showVisit(payload);
          if (payload.confirmation) event("Read-back sent");
          break;
        case "caller":
          message("You", payload.text);
          break;
        case "mark":
          this.mark(payload.id);
          break;
        case "clear":
          this.clear();
          event("Playback interrupted");
          break;
        case "ended":
          event(`Outcome · ${payload.outcome.replaceAll("_", " ")}`);
          this.close("Call complete");
          break;
      }
    };
    this.socket.onclose = ({ code }) => this.close(
      !this.connected && code === 1008 && token ? "Check the Deepgram demo access token" : "Call disconnected"
    );
  }

  async capture() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (this.ended) {
      this.stream.getTracks().forEach((track) => track.stop());
      return;
    }
    const capture = new AudioWorkletNode(this.context, "switchboard-capture");
    capture.port.onmessage = ({ data }) => {
      if (this.socket.readyState !== WebSocket.OPEN) return;
      if (this.socket.bufferedAmount > 8000) {
        this.close("Connection too slow for audio");
        return;
      }
      this.socket.send(data);
    };
    this.captureNode = capture;
    this.context.createMediaStreamSource(this.stream).connect(capture);
    capture.connect(this.context.destination);
  }

  play(data) {
    const buffer = this.context.createBuffer(1, data.length, 8000);
    const output = buffer.getChannelData(0);
    data.forEach((encoded, index) => {
      const code = (~encoded) & 255;
      const value = (((code & 15) << 3) + 132) << ((code >> 4) & 7);
      output[index] = (code & 128 ? 132 - value : value - 132) / 32768;
    });
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const start = Math.max(this.nextStart, this.context.currentTime + 0.03);
    this.nextStart = start + buffer.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    source.start(start);
  }

  mark(id) {
    // Schedule an inaudible one-sample buffer after the queued speech.
    // AudioContext's clock tracks playback when a browser tab is throttled.
    const marker = this.context.createBufferSource();
    marker.buffer = this.context.createBuffer(1, 1, 8000);
    marker.connect(this.context.destination);
    const generation = this.generation;
    this.sources.add(marker);
    marker.onended = () => {
      this.sources.delete(marker);
      if (generation === this.generation && this.socket.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify({ type: "mark", id }));
      }
    };
    marker.start(Math.max(this.nextStart, this.context.currentTime));
  }

  clear() {
    this.generation++;
    for (const source of this.sources) source.stop();
    this.sources.clear();
    this.nextStart = 0;
  }

  close(reason) {
    if (this.ended) return;
    this.ended = true;
    this.clear();
    this.socket?.close();
    this.captureNode?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.context?.close();
    this.onEnd(reason);
  }
}

const callButton = $("call-button");
const callMode = $("call-mode");
const tokenInput = $("voice-token");
let streaming;
let streamingAvailable = false;
let tokenRequired = true;
let voiceStatus = "Checking Deepgram streaming…";

function showCallMode() {
  const deepgram = callMode.value === "deepgram";
  $("live-voice").hidden = !deepgram || !tokenRequired;
  $("browser-audio").hidden = deepgram;
  $("voice-status").textContent = deepgram
    ? "Deepgram handles the microphone and plays the agent’s voice."
    : voiceStatus;
}

fetch("/capabilities").then((response) => response.json()).then(({ streaming_audio, streaming_audio_token_required, operations, clinic }) => {
  $("clinic-name").textContent = clinic;
  $("ops-link").hidden = !operations;
  streamingAvailable = Boolean(streaming_audio && window.AudioContext && navigator.mediaDevices?.getUserMedia);
  tokenRequired = streaming_audio_token_required !== false;
  callMode.querySelector('[value="deepgram"]').disabled = !streamingAvailable;
  if (streamingAvailable) callMode.value = "deepgram";
  voiceStatus = streamingAvailable
    ? "Select Deepgram streaming to use the configured Deepgram voice."
    : streaming_audio ? "Deepgram streaming needs microphone support in this browser."
      : "Deepgram streaming is off on this server.";
}).catch(() => {
  voiceStatus = "Could not check Deepgram streaming.";
}).finally(() => {
  callButton.disabled = callMode.disabled = false;
  showCallMode();
});

callMode.addEventListener("change", showCallMode);
callButton.addEventListener("click", async () => {
  if (streaming) {
    streaming.close("Call ended");
    return;
  }
  if (socket) {
    terminal = true;
    stopPlayback();
    socket.close();
    return;
  }
  if (callMode.value === "browser") {
    stopPlayback();
    callMode.disabled = true;
    start();
    return;
  }
  if (!streamingAvailable) return;
  const token = tokenRequired ? tokenInput.value.trim() : "";
  if (tokenRequired && !token) {
    $("call-status").textContent = "Enter the Deepgram demo access token to start";
    tokenInput.focus();
    return;
  }
  tokenInput.value = "";
  stopPlayback();
  resetVisit();
  messages.replaceChildren();
  events.replaceChildren();
  callMode.disabled = true;
  callButton.textContent = "End call";
  $("call-status").textContent = "Connecting…";
  streaming = new StreamCall((reason) => {
    streaming = undefined;
    callMode.disabled = false;
    $("call-status").textContent = reason;
    callButton.textContent = "Start call";
  });
  try {
    await streaming.open(token);
  } catch {
    streaming?.close("Streaming audio unavailable");
  }
});
