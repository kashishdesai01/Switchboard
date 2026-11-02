/* Send 160-sample μ-law frames from the browser's native capture rate. */
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Uint8Array(160);
    this.offset = 0;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const sample of channel) {
      this.sum += sample;
      this.count++;
      this.phase += 8000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const pcm = Math.round(32767 * Math.max(-1, Math.min(1, this.sum / this.count)));
      this.sum = 0;
      this.count = 0;
      const sign = pcm < 0 ? 128 : 0;
      const magnitude = Math.min(Math.abs(pcm), 32635) + 132;
      const segment = 24 - Math.clz32(magnitude);
      this.frame[this.offset++] = (~(sign | (segment << 4) | ((magnitude >> (segment + 3)) & 15))) & 255;
      if (this.offset === this.frame.length) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Uint8Array(160);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("switchboard-capture", Capture);
