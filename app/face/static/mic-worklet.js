// AudioWorklet for /mic: the phone's mic at the context's rate (44.1/48 kHz) -> 16 kHz mono Int16 frames.
// Loaded by mic.js with audioWorklet.addModule('/static/mic-worklet.js'). The Downsampler is plain JS so
// tests/js/mic_downsample.js can run it in deno; the processor part only registers inside a real AudioWorklet.

class Downsampler {
  // inRate: the context's sample rate. Each output sample is the mean of the input samples in its slot (a box
  // low-pass, plenty for speech into Whisper). A ratio below 1 (a context slower than 16 kHz) repeats samples.
  constructor(inRate, outRate = 16000, frameSamples = 1280) {
    this.ratio = inRate / outRate;
    this.frameSamples = frameSamples;          // 1280 = 80 ms at 16 kHz = 2560 bytes per WebSocket frame
    this.frame = new Int16Array(frameSamples);
    this.n = 0;                                // samples filled in this.frame
    this.acc = 0; this.count = 0; this.t = 0;  // the slot being averaged
    this.peak = 0;                             // max |x| of the frame being filled, 0..1
  }

  static toInt16(v) {
    const s = v > 1 ? 1 : v < -1 ? -1 : (v || 0);      // clamps, and NaN -> 0
    return Math.round(s < 0 ? s * 0x8000 : s * 0x7fff);
  }

  // Feed one block of Float32 samples; returns the frames it completed: [{pcm: Int16Array, peak}].
  push(input) {
    const out = [];
    for (let i = 0; i < input.length; i++) {
      const x = input[i];
      this.acc += x; this.count++; this.t += 1;
      while (this.t >= this.ratio) {
        const v = this.count ? this.acc / this.count : x;
        this.acc = 0; this.count = 0; this.t -= this.ratio;
        const a = Math.abs(v);
        if (a > this.peak) this.peak = a;
        this.frame[this.n++] = Downsampler.toInt16(v);
        if (this.n === this.frameSamples) {
          out.push({pcm: this.frame, peak: Math.min(1, this.peak)});
          this.frame = new Int16Array(this.frameSamples);
          this.n = 0; this.peak = 0;
        }
      }
    }
    return out;
  }
}

if (typeof registerProcessor === 'function') {
  class MicProcessor extends AudioWorkletProcessor {
    constructor(options) {
      super();
      const o = (options && options.processorOptions) || {};
      this.ds = new Downsampler(sampleRate, o.outRate || 16000, o.frameSamples || 1280);
    }
    process(inputs) {
      const ch = inputs[0] && inputs[0][0];       // mono: the first channel (getUserMedia asks for one)
      if (ch) {
        for (const f of this.ds.push(ch)) this.port.postMessage({pcm: f.pcm.buffer, peak: f.peak}, [f.pcm.buffer]);
      }
      return true;
    }
  }
  registerProcessor('mic-downsampler', MicProcessor);
}
