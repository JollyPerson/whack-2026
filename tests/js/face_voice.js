// app.js voice rules (the ?audio=1 tab is the one audio master), against fake audio, speech and timers.
// Run by tests/test_face_speech.py when deno is installed: deno run --allow-read tests/js/face_voice.js app/face/static/app.js
const src = Deno.readTextFileSync(Deno.args[0]);
const a = src.indexOf('// ------------------------------------------------------------------ Moderator voice');
const b = src.indexOf('// ------------------------------------------------------------------ rendering helpers');
const section = src.slice(a, b);
let failures = 0;
function check(name, cond, info) { if (!cond) { failures++; console.log('FAIL', name, JSON.stringify(info)); } else console.log('ok  ', name); }

function world() {
  let now = 0, timers = [], tid = 0;
  const w = {log: [], audios: [], utterances: []};
  w.setTimeout = (fn, ms) => { const id = ++tid; timers.push({id, at: now + ms, fn}); return id; };
  w.clearTimeout = id => { timers = timers.filter(t => t.id !== id); };
  w.advance = ms => {
    const end = now + ms;
    for (;;) {
      timers.sort((x, y) => x.at - y.at);
      const t = timers[0];
      if (!t || t.at > end) break;
      timers.shift(); now = t.at; t.fn();
    }
    now = end;
  };
  w.now = () => now;
  class FakeAudio {
    constructor(url) { this.url = url; this.paused = true; this.duration = NaN; w.audios.push(this); }
    play() { this.paused = false; return new Promise((res, rej) => { this._res = res; this._rej = rej; }); }
    pause() { this.paused = true; w.log.push(['pause', now]); if (this._rej) { const r = this._rej; this._rej = null; r(new Error('AbortError')); } }
    start(dur) { this.duration = dur; this._res && this._res(); this.onplaying && this.onplaying(); }
    end() { this.paused = true; this.onended && this.onended(); }
    fail() { this.onerror && this.onerror(); this._rej && this._rej(new Error('NotSupportedError')); }
  }
  class Utt { constructor(t) { this.text = t; w.utterances.push(this); } }
  const speechSynthesis = {speak: u => w.log.push(['speak', u.text]), cancel: () => w.log.push(['cancel', now]), getVoices: () => []};
  const S = {flags: {}};
  const env = {AUDIO: true, audioReady: true, S, STALE_SPEECH_MS: 15000,
               ttsState: (on, id) => w.log.push(['tts', on, id, now]),
               setTimeout: w.setTimeout, clearTimeout: w.clearTimeout, Date: {now: () => now},
               Audio: FakeAudio, SpeechSynthesisUtterance: Utt, window: {speechSynthesis}, speechSynthesis};
  const names = Object.keys(env);
  const api = new Function(...names, section + '\nreturn {speak, cutSpeech, onControl};')(...names.map(n => env[n]));
  Object.assign(w, api);
  w.tts = () => w.log.filter(x => x[0] === 'tts').map(x => [x[1], x[3]]);
  return w;
}
const flush = () => new Promise(r => setTimeout(r, 0));
const LONG = 'Correction: the deficit was £1.2bn in 2009/10, not £12bn; spending rose 27% to £834,000.';   // 14 words

{ // 1. a 14.2 s WAV for a 14-word line: the watchdog (old: 11.4 s) must not cut it
  const w = world(); w.speak({id: 'i-1', text: LONG}); w.advance(300); w.audios[0].start(14.16);
  w.advance(14160); w.audios[0].end(); await flush();
  check('long WAV plays to its end', JSON.stringify(w.tts()) === JSON.stringify([[true, 300], [false, 14460]]), w.log);
}
{ // 2. onended lost: watchdog cuts at length + 2 s, pausing before TTSState false
  const w = world(); w.speak({id: 'i-2', text: LONG}); w.audios[0].start(14.16); w.advance(30000); await flush();
  const p = w.log.findIndex(x => x[0] === 'pause'), f = w.log.findIndex(x => x[0] === 'tts' && !x[1]);
  check('lost onended: watchdog at dur+2s, pause first', p >= 0 && p < f && w.log[f][3] === 16160, w.log);
}
{ // 3. 503 -> browser speech; its onstart is audible-now; estimate watchdog
  const w = world(); w.speak({id: 'i-3', text: 'Point of fact.'}); w.audios[0].fail(); await flush();
  check('fallback speaks once', w.log.filter(x => x[0] === 'speak').length === 1, w.log);
  w.advance(200); w.utterances[0].onstart({type: 'start'}); w.advance(1000); w.utterances[0].onend();
  check('fallback TTSState true/false', JSON.stringify(w.tts()) === JSON.stringify([[true, 200], [false, 1200]]), w.log);
}
{ // 4. fallback speech never ends: estimate watchdog (3 + 3*0.6 = 4.8 s), cancel first
  const w = world(); w.speak({id: 'i-4', text: 'Point of fact.'}); w.audios[0].fail(); await flush();
  w.utterances[0].onstart({type: 'start'}); w.advance(10000);
  const c = w.log.findIndex(x => x[0] === 'cancel'), f = w.log.findIndex(x => x[0] === 'tts' && !x[1]);
  check('speech watchdog uses the estimate', c >= 0 && c < f && w.log[f][3] === 4800, w.log);
}
{ // 5. mute mid-line
  const w = world(); w.speak({id: 'i-5', text: LONG}); w.audios[0].start(14.16); w.advance(2000);
  w.onControl({flag: 'mute', value: true}); await flush();
  check('mute cuts: pause then false', JSON.stringify(w.tts()) === JSON.stringify([[true, 0], [false, 2000]]) && w.audios[0].paused, w.log);
}
{ // 6. never audible: no TTSState at all, line given up at 8 s, next line runs
  const w = world(); w.speak({id: 'i-6', text: 'A.'}); w.speak({id: 'i-7', text: 'B.'}); w.advance(8000); await flush();
  check('never audible: no TTSState, next line starts', w.tts().length === 0 && w.audios.length === 2, w.log);
}
{ // 7. unknown duration: estimate
  const w = world(); w.speak({id: 'i-8', text: 'Point of fact.'}); w.audios[0].start(NaN); w.advance(10000); await flush();
  const f = w.log.find(x => x[0] === 'tts' && !x[1]);
  check('NaN duration: estimate', f && f[3] === 4800, w.log);
}
{ // 8. short WAV: estimate still the floor (no regression for D)
  const w = world(); w.speak({id: 'i-9', text: 'Point of fact.'}); w.audios[0].start(1.36); w.advance(10000); await flush();
  const f = w.log.find(x => x[0] === 'tts' && !x[1]);
  check('short WAV: estimate floor', f && f[3] === 4800, w.log);
}
{ // 9. mute while the WAV is still loading: no TTSState, and no browser speech starts afterwards
  const w = world(); w.speak({id: 'i-10', text: 'Point of fact.'}); w.advance(500);
  w.onControl({flag: 'mute', value: true}); await flush(); w.audios[0].fail(); await flush(); w.advance(10000);
  check('mute before audible: silent', w.tts().length === 0 && !w.log.some(x => x[0] === 'speak'), w.log);
}
console.log(failures ? `${failures} FAILED` : 'all passed');
Deno.exit(failures ? 1 : 0);
