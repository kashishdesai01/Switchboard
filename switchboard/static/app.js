"use strict";

const $ = (id) => document.getElementById(id);
const messages = $("messages");
const events = $("events");
const input = $("message");
const audio = $("audio");
const mic = $("mic");
const sampleDetails = $("sample-details");
const confirmation = $("confirmation");
const browserVoice = $("browser-voice");
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let socket;
let waiting = false;
let playback;
let recognizer;
let voiceEnabled = false;
let listenTimer;
let terminal = false;
let replyTo;
let playbackTimer;

mic.hidden = !SpeechRecognition;
audio.disabled = !window.speechSynthesis;
if (audio.disabled) audio.checked = false;

function transmit(type, data = {}) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type, ...data }));
    return true;
  }
  return false;
}

function controls(active) {
  input.disabled = !active;
  $("composer").querySelector("button[type=submit]").disabled = !active;
  mic.disabled = !active && !voiceEnabled;
  sampleDetails.querySelectorAll("button").forEach((button) => {
    button.disabled = !active;
  });
  $("suggestions").querySelectorAll("button").forEach((button) => {
    button.disabled = !active;
  });
}

function event(text) {
  const item = document.createElement("li");
  item.textContent = text;
  events.prepend(item);
}

function message(role, text) {
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "You") bubble.classList.add("caller");
  const author = document.createElement("strong");
  author.textContent = role;
  bubble.append(author, document.createTextNode(text));
  messages.append(bubble);
  messages.scrollTop = messages.scrollHeight;
}

function stopPlayback() {
  clearTimeout(playbackTimer);
  if (!playback) return;
  playback = undefined;
  window.speechSynthesis.cancel();
}

function conversationVoices() {
  const novelty = /^(Albert|Bad News|Bahh|Bells|Boing|Bubbles|Cellos|Fred|Good News|Jester|Organ|Ralph|Superstar|Trinoids|Whisper|Wobble|Zarvox)$/i;
  return window.speechSynthesis.getVoices().filter((voice) => /^en[-_]/i.test(voice.lang) && !novelty.test(voice.name));
}

function updateVoices() {
  if (!window.speechSynthesis) return;
  const selected = browserVoice.value;
  const voices = conversationVoices();
  browserVoice.replaceChildren(new Option("Suggested voice", ""), ...voices.map(
    (voice) => new Option(voice.name, voice.voiceURI)
  ));
  if (voices.some((voice) => voice.voiceURI === selected)) browserVoice.value = selected;
  browserVoice.hidden = $("browser-voice-label").hidden = voices.length < 2;
}

if (window.speechSynthesis) {
  updateVoices();
  window.speechSynthesis.addEventListener("voiceschanged", updateVoices);
}

function speak(reply) {
  if (!audio.checked) {
    resumeListening();
    return;
  }
  const utterance = new SpeechSynthesisUtterance(reply.text);
  playback = utterance;
  utterance.lang = "en-US";
  utterance.rate = 0.89;
  utterance.pitch = 0.98;
  const englishVoices = conversationVoices();
  const selectedVoice = englishVoices.find((voice) => voice.voiceURI === browserVoice.value)
    || englishVoices.find((voice) => /natural|premium|enhanced/i.test(voice.name) && voice.lang === "en-US")
    || englishVoices.find((voice) => /^(Ava|Samantha|Allison)( |$)/i.test(voice.name));
  if (selectedVoice) {
    utterance.voice = selectedVoice;
    utterance.lang = selectedVoice.lang;
  }
  utterance.onend = () => {
    if (playback !== utterance) return;
    clearTimeout(playbackTimer);
    playback = undefined;
    resumeListening();
  };
  utterance.onerror = () => {
    if (playback !== utterance) return;
    clearTimeout(playbackTimer);
    playback = undefined;
    audio.checked = false;
    event("Speech unavailable. The response is shown in the conversation.");
    resumeListening();
  };
  playbackTimer = setTimeout(() => {
    if (playback !== utterance) return;
    stopPlayback();
    audio.checked = false;
    event("Voice playback paused. You can continue with the microphone or text.");
    resumeListening();
  }, Math.max(6000, reply.text.length * 85 + 3000));
  window.speechSynthesis.speak(utterance);
}

function resetVisit() {
  $("email-panel").hidden = true;
  $("email-status").textContent = "";
  $("calendar-slots").replaceChildren();
  $("calendar-status").textContent = "Appointment options will appear here.";
}

function showVisit(reply) {
  const slots = reply.calendar || [];
  const labels = { offered: "Available to select", held: "Held while you confirm", booked: "Confirmed appointment" };
  $("calendar-status").textContent = slots.length ? labels[slots[0].state] : "No appointment currently selected.";
  $("calendar-slots").replaceChildren(...slots.map((slot, index) => {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = slot.start;
    time.textContent = slot.label;
    if (slot.state === "offered" && socket && reply.text.includes("Option 1")) {
      const choose = document.createElement("button");
      choose.type = "button";
      choose.textContent = `Option ${index + 1}`;
      choose.addEventListener("click", () => send(`option ${index + 1}`));
      item.append(choose);
    }
    item.append(time);
    return item;
  }));
  if (!reply.email_status) return;
  const emailLabels = {
    queued: "Confirmation email queued for delivery.",
    sending: "Sending the confirmation email.",
    sent: "Confirmation accepted by the mail server.",
    captured: "Confirmation saved in the demo outbox.",
    needs_address: "The front desk needs an email address for the confirmation.",
  };
  $("email-status").textContent = emailLabels[reply.email_status] || "The front desk can check your email status.";
  $("email-panel").hidden = false;
}

function send(text) {
  const clean = text.trim();
  if (!clean || waiting || !socket || terminal) return;
  stopPlayback();
  if (!transmit("text", { text: clean, reply_to: replyTo })) return;
  replyTo = undefined;
  waiting = true;
  recognizer?.abort();
  updateMic();
  controls(false);
  input.value = "";
  message("You", clean);
  confirmation.hidden = true;
  $("suggestions").hidden = true;
}

function start() {
  terminal = false;
  resetVisit();
  replyTo = undefined;
  waiting = true;
  messages.replaceChildren();
  events.replaceChildren();
  confirmation.hidden = true;
  $("call-status").textContent = "Connecting…";
  $("call-button").textContent = "End call";
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const activeSocket = new WebSocket(`${scheme}://${location.host}/call`);
  socket = activeSocket;
  activeSocket.onmessage = ({ data }) => {
    if (socket !== activeSocket) return;
    const reply = JSON.parse(data);
    if (reply.type === "ready") {
      $("backend").textContent = `${reply.mode} · ${audio.checked ? "Browser voice" : "Text replies"}`;
      $("call-status").textContent = "Call in progress";
      $("suggestions").hidden = false;
      event("Call connected");
      return;
    }
    waiting = false;
    terminal = Boolean(reply.outcome);
    if (terminal) setVoice(false);
    message("Switchboard", reply.text);
    showVisit(reply);
    replyTo = reply.confirmation ? reply.id : undefined;
    confirmation.hidden = !reply.confirmation;
    if (reply.confirmation) {
      event("Read-back displayed");
    }
    event(reply.outcome
      ? `Outcome · ${reply.outcome.replaceAll("_", " ")}`
      : `Response prepared · ${reply.latency_ms} ms`);
    speak(reply);
    controls(!terminal);
  };
  activeSocket.onclose = () => {
    if (socket !== activeSocket) return;
    socket = undefined;
    $("call-mode").disabled = false;
    setVoice(false);
    controls(false);
    confirmation.hidden = true;
    $("suggestions").hidden = true;
    $("call-status").textContent = terminal ? "Call complete" : "Call disconnected";
    $("call-button").textContent = "Start call";
    if (!terminal) {
      stopPlayback();
      event("Call disconnected");
    }
  };
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  send(input.value);
});

$("suggestions").addEventListener("click", (e) => {
  if (e.target.dataset.message) send(e.target.dataset.message);
});

confirmation.addEventListener("click", (e) => {
  if (e.target.dataset.message) send(e.target.dataset.message);
});

sampleDetails.addEventListener("click", (e) => {
  if (input.disabled || !e.target.dataset.fill) return;
  setVoice(false);
  input.value = e.target.dataset.fill;
  input.focus();
});

audio.addEventListener("change", () => {
  if (!audio.checked) {
    stopPlayback();
    resumeListening();
  }
});

function updateMic() {
  mic.setAttribute("aria-pressed", String(voiceEnabled));
  mic.textContent = !voiceEnabled ? "Enable mic" : recognizer && !waiting ? "Listening…" : "Mic on";
}

function setVoice(enabled) {
  voiceEnabled = enabled;
  if (enabled) {
    resumeListening();
  } else {
    clearTimeout(listenTimer);
    recognizer?.abort();
  }
  updateMic();
}

function resumeListening(delay = 0) {
  updateMic();
  clearTimeout(listenTimer);
  if (delay) {
    listenTimer = setTimeout(resumeListening, delay);
    return;
  }
  if (!voiceEnabled || recognizer || playback || waiting || terminal ||
      socket?.readyState !== WebSocket.OPEN) return;
  const activeSocket = socket;
  const activeRecognizer = new SpeechRecognition();
  recognizer = activeRecognizer;
  activeRecognizer.lang = "en-US";
  activeRecognizer.onresult = (e) => {
    if (voiceEnabled && activeSocket === socket && recognizer === activeRecognizer) {
      send(e.results[e.resultIndex][0].transcript);
    }
  };
  activeRecognizer.onerror = ({ error }) => {
    if (activeSocket !== socket || !voiceEnabled || error === "aborted" || error === "no-speech") return;
    setVoice(false);
    controls(!waiting && !terminal);
    event("Microphone recognition unavailable. You can type your reply.");
  };
  activeRecognizer.onend = () => {
    if (recognizer !== activeRecognizer) return;
    recognizer = undefined;
    resumeListening(300);
  };
  try {
    activeRecognizer.start();
    updateMic();
  } catch {
    recognizer = undefined;
    setVoice(false);
    event("Microphone recognition unavailable. You can type your reply.");
  }
}

mic.addEventListener("click", () => {
  if (SpeechRecognition && socket && !terminal) {
    setVoice(!voiceEnabled);
    controls(!waiting);
  }
});
