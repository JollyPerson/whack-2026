// The /mic page (mic.js) against a fake DOM, mic, AudioContext and WebSocket: the insecure-context and
// permission errors, the side picker, the WebSocket URL (channel + token), frames sent only once the server said ok,
// a refusal stops without retrying, a dropped link retries with backoff, Stop releases everything.
// Run by tests/test_phone_mic.py: deno run --allow-read tests/js/mic_page.js app/face/static/mic.js
const src = Deno.readTextFileSync(Deno.args[0]);
let failures = 0;
function check(name, cond, info) { if (!cond) { failures++; console.log('FAIL', name, JSON.stringify(info)); } else console.log('ok  ', name); }
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 10; i++) await tick(); }

function page({search = '?ch=A', secure = true, protocol = 'http:', host = 'localhost:8000', gum = 'ok'} = {}) {
  const els = {};
  const el = id => els[id] || (els[id] = {id, hidden: true, textContent: '', className: '', href: '', disabled: false,
    style: {}, listeners: {}, classList: {set: new Set(), toggle(c, on) { on ? this.set.add(c) : this.set.delete(c); },
      add(c) { this.set.add(c); }},
    addEventListener(t, f) { this.listeners[t] = f; }});
  const w = {sockets: [], timers: [], tracks: [], ctxs: [], nodes: [], wakes: 0, released: 0, gumArgs: null};
  for (const id of ['error', 'state', 'state-text']) el(id).hidden = id === 'error';
  const document = {getElementById: el, body: el('body'), title: '', visibilityState: 'visible',
    addEventListener() {}};
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; this.bufferedAmount = 0; w.sockets.push(this); }
    send(d) { this.sent.push(d); }
    close(code) { this.readyState = 3; this.closedWith = code; }
    open() { this.readyState = 1; }
    msg(obj) { this.onmessage({data: JSON.stringify(obj)}); }
    drop(code, reason = '') { this.readyState = 3; this.onclose({code, reason}); }
  }
  class FakeNode {
    constructor(ctx, name, opts) { this.name = name; this.opts = opts; this.port = {}; w.nodes.push(this); }
    connect(x) { this.to = x; } disconnect() { this.cut = true; }
    frame(bytes = 2560, peak = 0.3) { this.port.onmessage && this.port.onmessage({data: {pcm: new ArrayBuffer(bytes), peak}}); }
  }
  class FakeCtx {
    constructor() { this.destination = {}; this.audioWorklet = {addModule: async u => { this.module = u; }}; w.ctxs.push(this); }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    async resume() {} close() { this.closed = true; return Promise.resolve(); }
  }
  const navigator = {
    mediaDevices: {getUserMedia: async args => {
      w.gumArgs = args;
      if (gum !== 'ok') { const e = new Error('nope'); e.name = gum; throw e; }
      const t = {stop() { t.stopped = true; }};
      w.tracks.push(t);
      return {getAudioTracks: () => [t], getTracks: () => [t]};
    }},
    wakeLock: {request: async () => { w.wakes++; return {addEventListener() {}, release: async () => { w.released++; }}; }},
  };
  const window = {isSecureContext: secure};
  const location = {search, protocol, host, hostname: host.split(':')[0]};
  const env = {document, window, navigator, location, WebSocket: FakeWS, AudioContext: FakeCtx, AudioWorkletNode: FakeNode,
    URLSearchParams, Date, setTimeout: (f, ms) => { w.timers.push({f, ms}); return w.timers.length; },
    clearTimeout: id => { if (id && w.timers[id - 1]) w.timers[id - 1].cancelled = true; }};
  new Function(...Object.keys(env), src)(...Object.values(env));
  w.el = el; w.api = window.__mic;
  w.click = async () => { el('toggle').listeners.click(); await settle(); };
  return w;
}

// No side in the URL: the picker, keeping the token in both links.
{
  const w = page({search: '?token=s3cret'});
  check('picker shown', w.el('picker').hidden === false && w.el('toggle').hidden === true);
  check('picker keeps token', w.el('pick-A').href === '?token=s3cret&ch=A' && w.el('pick-B').href.includes('ch=B'),
        w.el('pick-A').href);
}

// Not a secure context (plain http on a LAN IP): says so up front, and Start doesn't ask for the mic.
{
  const w = page({secure: false, host: '192.168.1.20:8000'});
  check('insecure: error shown', !w.el('error').hidden && w.el('error').textContent.startsWith('Not a secure context') &&
        w.el('error').textContent.includes('adb reverse'), w.el('error').textContent);
  await w.click();
  check('insecure: no getUserMedia', w.gumArgs === null && w.sockets.length === 0);
}

// Permission denied: a clear message, nothing left running.
{
  const w = page({gum: 'NotAllowedError'});
  await w.click();
  check('denied: message', w.el('error').textContent.startsWith('Microphone permission denied'), w.el('error').textContent);
  check('denied: stopped', w.el('toggle').textContent === 'Start' && w.sockets.length === 0);
}

// The happy path, then a dropped link, then a refusal.
{
  const w = page({search: '?ch=b&token=t%26x&agc=0'});
  check('side B label', w.el('side-name').textContent === 'Opposition' && w.el('letter').textContent === 'B');
  await w.click();
  check('gum constraints', w.gumArgs.audio.echoCancellation === true && w.gumArgs.audio.noiseSuppression === true &&
        w.gumArgs.audio.autoGainControl === false, w.gumArgs);
  check('worklet loaded', w.ctxs[0].module === '/static/mic-worklet.js' && w.nodes[0].name === 'mic-downsampler');
  check('wake lock', w.wakes === 1);
  const s = w.sockets[0];
  check('ws url', s.url === 'ws://localhost:8000/mic/ws?ch=B&token=t%26x', s.url);
  s.open();
  w.nodes[0].frame();
  check('nothing sent before ok', s.sent.length === 0);
  s.msg({ok: true, ch: 'B', sink: 'whack_phone_B'});
  check('live state', w.el('state').className === 'state live', w.el('state').className);
  w.nodes[0].frame(); w.nodes[0].frame();
  check('frames sent', s.sent.length === 2 && s.sent[0].byteLength === 2560);
  s.bufferedAmount = 100000; w.nodes[0].frame();
  check('stalled link: dropped, not queued', s.sent.length === 2);
  s.drop(1006);
  check('drop: retry scheduled', w.timers.length === 1 && w.timers[0].ms === 500 &&
        w.el('state').className === 'state retrying', w.timers);
  w.timers[0].f();
  const s2 = w.sockets[1];
  check('reconnects', !!s2);
  s2.drop(1006);
  check('backoff doubles', w.timers[1].ms === 1000, w.timers[1]);
  w.timers[1].f();
  const s3 = w.sockets[2];
  s3.open();
  s3.msg({error: 'another phone is already microphone B: stop it first', code: 4409});
  s3.drop(4409, 'busy');
  check('refused: stops, no retry', w.timers.length === 2 && w.el('toggle').textContent === 'Start', w.timers.length);
  check('refused: says why', w.el('error').textContent.includes('Another phone') &&
        w.el('error').textContent.includes('already microphone B'), w.el('error').textContent);
  check('refused: mic released', w.tracks[0].stopped === true && w.ctxs[0].closed === true && w.released === 1);
}

// Stop: closes the socket with 1000 and releases the mic; a later close event doesn't retry.
{
  const w = page({protocol: 'https:', host: '10.0.0.5:8443'});
  await w.click();
  const s = w.sockets[0];
  check('wss on https', s.url === 'wss://10.0.0.5:8443/mic/ws?ch=A', s.url);
  s.open(); s.msg({ok: true});
  await w.click();
  check('stop closes ws', s.closedWith === 1000 && w.tracks[0].stopped === true);
  s.onclose({code: 1000});
  check('no retry after stop', w.timers.length === 0);
}

check('backoff caps at 10 s', w_backoff());
function w_backoff() { const a = page().api; return a.backoffMs(0) === 500 && a.backoffMs(3) === 4000 && a.backoffMs(10) === 10000; }

console.log(failures ? `${failures} failed` : 'all passed');
if (failures) Deno.exit(1);
