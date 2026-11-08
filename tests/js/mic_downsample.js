// The /mic page's AudioWorklet: downsampling to 16 kHz mono and the Int16 packing, plus the processor's framing.
// Run by tests/test_phone_mic.py when deno is installed:
//   deno run --allow-read tests/js/mic_downsample.js app/face/static/mic-worklet.js
const src = Deno.readTextFileSync(Deno.args[0]);
let failures = 0;
function check(name, cond, info) { if (!cond) { failures++; console.log('FAIL', name, JSON.stringify(info)); } else console.log('ok  ', name); }

const Downsampler = new Function(src + '\nreturn Downsampler;')();   // no registerProcessor here: only the class

function run(inRate, samples, block = 128, frame = 1280) {
  const ds = new Downsampler(inRate, 16000, frame);
  const frames = [];
  for (let i = 0; i < samples.length; i += block) frames.push(...ds.push(samples.subarray(i, i + block)));
  return {frames, ds};
}
function concat(frames) {
  const out = new Int16Array(frames.length * (frames[0] ? frames[0].pcm.length : 0));
  frames.forEach((f, i) => out.set(f.pcm, i * f.pcm.length));
  return out;
}
const fill = (n, f) => { const a = new Float32Array(n); for (let i = 0; i < n; i++) a[i] = f(i); return a; };

// 48 kHz: one second in, 16000 samples out = 12 full 80 ms frames (+ 640 samples waiting for the 13th).
{
  const {frames, ds} = run(48000, fill(48000, () => 0.5));
  check('48k: 12 frames of 1280', frames.length === 12 && frames.every(f => f.pcm.length === 1280), frames.length);
  check('48k: 640 samples pending', ds.n === 640, ds.n);
  check('48k: constant 0.5 -> 16384', concat(frames).every(v => v === 16384), concat(frames).slice(0, 4));
  check('48k: peak reported', Math.abs(frames[0].peak - 0.5) < 1e-6, frames[0].peak);
}

// 44.1 kHz (a non-integer ratio): the output rate is still 16 kHz, within a sample.
{
  const {frames, ds} = run(44100, fill(44100 * 2, () => 0.1));
  const total = frames.length * 1280 + ds.n;
  check('44.1k: 2 s -> 32000 samples', Math.abs(total - 32000) <= 1, total);
}

// A 440 Hz tone keeps its pitch and level: ~880 zero crossings a second, peak ~0.8.
{
  const sine = fill(48000, i => 0.8 * Math.sin(2 * Math.PI * 440 * i / 48000));
  const out = concat(run(48000, sine).frames);
  let zc = 0;
  for (let i = 1; i < out.length; i++) if ((out[i - 1] < 0) !== (out[i] < 0)) zc++;
  const peak = Math.max(...out) / 32767;
  check('440 Hz: pitch kept', Math.abs(zc / (out.length / 16000) - 880) < 10, zc);
  check('440 Hz: level kept', peak > 0.75 && peak <= 0.8, peak);
}

// Block size doesn't matter: 128-sample blocks (a real AudioWorklet) == one big push.
{
  const noise = fill(48000, i => Math.sin(i * 12.9898) * 0.7);
  const a = concat(run(48000, noise, 128).frames), b = concat(run(48000, noise, 48000).frames);
  check('blocking invariant', a.length === b.length && a.every((v, i) => v === b[i]), [a.length, b.length]);
}

// Int16 conversion: clamps, full scale both ways, NaN -> 0.
{
  const t = Downsampler.toInt16;
  check('toInt16 clamps', t(1.5) === 32767 && t(-1.5) === -32768 && t(1) === 32767 && t(-1) === -32768, [t(1.5), t(-1.5)]);
  check('toInt16 zero/NaN', t(0) === 0 && t(NaN) === 0 && t(undefined) === 0, [t(NaN)]);
  check('toInt16 half', t(0.5) === 16384 && t(-0.5) === -16384, [t(0.5), t(-0.5)]);
}

// The bytes on the wire are s16le, which is what the server's pacat --format=s16le expects.
{
  const ds = new Downsampler(16000, 16000, 2);
  const [f] = ds.push(new Float32Array([1 / 32767, -2 / 32768]));
  const bytes = [...new Uint8Array(f.pcm.buffer)];
  check('s16le packing', JSON.stringify(bytes) === JSON.stringify([1, 0, 254, 255]), bytes);
}

// 16 kHz in: a pass-through. 8 kHz in: every sample twice.
{
  const x = fill(1280, i => ((i % 7) - 3) / 4);
  const same = run(16000, x).frames;
  check('16k pass-through', same.length === 1 && same[0].pcm.every((v, i) => v === Downsampler.toInt16(x[i])), same.length);
  const up = run(8000, fill(1280, () => 0.25)).frames;
  check('8k doubles', up.length === 2 && up.every(f => f.pcm.every(v => v === 8192)), up.length);
}

// The processor: registered inside an AudioWorklet, it posts one transferable 2560-byte frame per 80 ms.
{
  const posted = [];
  let registered = null;
  class AudioWorkletProcessor { constructor() { this.port = {postMessage: (m, t) => posted.push({m, t})}; } }
  new Function('AudioWorkletProcessor', 'registerProcessor', 'sampleRate', src)(
    AudioWorkletProcessor, (name, cls) => { registered = {name, cls}; }, 48000);
  check('registers mic-downsampler', registered && registered.name === 'mic-downsampler', registered && registered.name);
  const p = new registered.cls({processorOptions: {}});
  let keep = true;
  for (let i = 0; i < 30; i++) keep = p.process([[fill(128, () => 0.25)]]) && keep;   // 3840 samples = 1280 out
  check('process keeps running', keep === true);
  check('one frame posted', posted.length === 1, posted.length);
  check('frame is 2560 bytes, transferred', posted[0] && posted[0].m.pcm.byteLength === 2560 &&
        posted[0].t[0] === posted[0].m.pcm, posted[0] && posted[0].m.pcm.byteLength);
  check('no input: no crash', p.process([[]]) === true && p.process([]) === true);
}

console.log(failures ? `${failures} failed` : 'all passed');
if (failures) Deno.exit(1);
