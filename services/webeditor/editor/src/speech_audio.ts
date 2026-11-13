/** Stateful resampling: chunk boundaries cannot reset the sampling phase. */
export class Pcm16Resampler {
  private samples: number[] = [];
  private position = 0;
  constructor(private inputRate: number) {
    if (!Number.isFinite(inputRate) || inputRate < 16000) throw new Error("Unsupported microphone sample rate");
  }
  push(chunk: Float32Array, finish = false): ArrayBuffer {
    for (const sample of chunk) this.samples.push(sample);
    const output: number[] = [];
    const step = this.inputRate / 16000;
    // Average target intervals to reduce aliasing when downsampling.
    while (this.position + step <= this.samples.length + 1e-7 || (finish && this.position < this.samples.length - 1e-7)) {
      const end = Math.min(this.position + step, this.samples.length);
      let total = 0;
      for (let i = Math.floor(this.position); i < Math.ceil(end); i++) {
        total += this.samples[i]! * (Math.min(i + 1, end) - Math.max(i, this.position));
      }
      output.push(Math.max(-1, Math.min(1, total / (end - this.position))));
      this.position = end;
    }
    const consumed = Math.floor(this.position);
    this.samples.splice(0, consumed);
    this.position -= consumed;
    const buffer = new ArrayBuffer(output.length * 2);
    const view = new DataView(buffer);
    output.forEach((sample, index) => view.setInt16(index * 2, Math.round(sample < 0 ? sample * 32768 : sample * 32767), true));
    return buffer;
  }
}
