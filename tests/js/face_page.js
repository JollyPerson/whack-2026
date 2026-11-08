// The whole dashboard (app.js) against a fake DOM, WebSocket, audio and clock: the transport, reducer and drawing
// rules that tests/js/face_voice.js (the voice section alone) can't reach.
// Run by tests/test_face_speech.py when deno is installed, with index.html's scripts in their load order:
// deno run --allow-read tests/js/face_page.js app/face/static/botcard.js app/face/static/app.js  (app.js last)
const src = Deno.args.map(f => Deno.readTextFileSync(f)).join('\n');
let failures = 0;
function check(name, cond, info) { if (!cond) { failures++; console.log('FAIL', name, JSON.stringify(info)); } else console.log('ok  ', name); }
const flush = () => new Promise(r => setTimeout(r, 0));

class El {                                     // just enough DOM for app.js: no HTML parsing, innerHTML is a string
  constructor(id = '', tag = 'div') {
    Object.assign(this, {id, tagName: tag, dataset: {}, style: {}, hidden: false, className: '', title: '', textContent: '',
                         value: '', children: [], parentNode: null, writes: 0, _html: '', scrollHeight: 0, scrollTop: 0,
                         clientHeight: 0});
    this.classList = {toggle() {}, add() {}, remove() {}, contains: () => false};
  }
  set innerHTML(h) { this._html = h; this.writes++; this.children = []; }
  get innerHTML() { return this._html; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { const c = this.parentNode ? this.parentNode.children : []; return c[c.indexOf(this) + 1] || null; }
  insertBefore(n, at) {
    n.remove();
    const i = at ? this.children.indexOf(at) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    n.parentNode = this;
    return n;
  }
  appendChild(n) { return this.insertBefore(n, null); }
  replaceChildren(...ns) { for (const c of [...this.children]) c.remove(); ns.forEach(n => this.appendChild(n)); }
  remove() { if (this.parentNode) { const c = this.parentNode.children; c.splice(c.indexOf(this), 1); this.parentNode = null; } }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() {}
}

function page(search = '') {
  let now = 1.8e12, timers = [], tid = 0, seq = 0;
  const w = {sockets: [], audios: [], beacons: [], els: new Map(), made: [], on: {}, winOn: {}};
  const el = id => { if (!w.els.has(id)) w.els.set(id, new El(id)); return w.els.get(id); };
  const make = tag => { const n = new El('', tag); w.made.push(n); return n; };
  const document = {getElementById: el, createElement: make, body: new El('body'),
                    documentElement: new El('html'), querySelectorAll: () => [],
                    addEventListener: (t, fn) => (w.on[t] ||= []).push(fn)};
  const setTimeout = (fn, ms) => { const id = ++tid; timers.push({id, at: now + ms, fn}); return id; };
  const clearTimeout = id => { timers = timers.filter(t => t.id !== id); };
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
  w.now = () => now / 1000;
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; w.sockets.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    msg(o) { this.onmessage({data: JSON.stringify(o)}); }
    drop() { this.readyState = 3; this.onclose && this.onclose(); }
  }
  FakeWS.OPEN = 1;
  class FakeAudio {
    constructor(url) { this.url = url; this.paused = true; this.duration = NaN; w.audios.push(this); }
    play() { this.paused = false; return new Promise((res, rej) => { this._res = res; this._rej = rej; }); }
    pause() { this.paused = true; if (this._rej) { const r = this._rej; this._rej = null; r(new Error('AbortError')); } }
    start(dur) { this.duration = dur; this._res && this._res(); this.onplaying && this.onplaying(); }
    end() { this.paused = true; this.onended && this.onended(); }
  }
  const speechSynthesis = {speak() {}, cancel() {}, getVoices: () => []};
  const env = {location: {search, protocol: 'http:', host: 'h'}, document, WebSocket: FakeWS, Audio: FakeAudio,
               SpeechSynthesisUtterance: class { constructor(t) { this.text = t; } }, speechSynthesis,
               window: {speechSynthesis, addEventListener: (t, fn) => (w.winOn[t] ||= []).push(fn)},
               navigator: {sendBeacon: (url, body) => { w.beacons.push([url, JSON.parse(body)]); return true; }},
               setTimeout, clearTimeout, setInterval: () => 0, requestAnimationFrame: () => 0, Date: {now: () => now},
               fetch: () => new Promise(() => {}), alert: () => {}, console};
  const names = Object.keys(env);
  Object.assign(w, new Function(...names, src + '\nreturn {apply, draw, state: () => ({S, banner})};')(...names.map(n => env[n])));
  w.el = el;
  w.ws = () => w.sockets[w.sockets.length - 1];
  w.connect = (events = [], modules = {}, flags = {}) => { // open the socket the page made and send its Snapshot
    w.ws().open();
    w.ws().msg({event: 'Snapshot', last_seq: seq, flags, modules, events});
  };
  w.live = e => { w.ws().msg({seq: ++seq, ts: w.now(), ...e}); };   // a live event from the server
  w.snap = e => ({seq: ++seq, ts: w.now(), ...e});                  // an event for the next Snapshot
  w.click = id => w.on.click.forEach(fn => fn({target: {closest: () => ({id, dataset: {}})}}));
  w.key = key => (w.on.keydown || []).forEach(fn => fn({key, target: {closest: () => null}, preventDefault() {}}));
  w.tts = (sock = w.ws()) => sock.sent.filter(m => m.event && m.event.event === 'TTSState')
                                      .map(m => [m.src, m.event.speaking, m.event.interjection_id]);
  w.pagehide = () => (w.winOn.pagehide || []).forEach(fn => fn({}));
  w.holder = needle => [...w.els.values()].find(e => e.innerHTML.includes(needle));
  w.speakingShown = () => { w.draw(); return w.el('referee').innerHTML.includes('state speaking'); };
  return w;
}
const LINE = {event: 'Interjection', id: 'i-1', text: 'Point of fact.'};
const BOT = (persona, id, text, more = {}) => ({event: 'BotLine', persona, id, text, kind: 'followup', sourced: false, ...more});
// One bot's box in the For / Against strip: its line's class and [class, text] of each part.
const botBox = (p, persona) => {
  const n = p.el(`bot-${persona}`).children[0];
  return n ? {node: n, cls: n.className, parts: n.children.map(x => [x.className, x.textContent])} : {parts: []};
};
// [class, text] of each part of that box's source card (a sourced line), [] without one.
const srcCard = (p, persona) => {
  const n = p.el(`bot-${persona}`).children[0], c = n && n.children.find(x => x.className === 'srccard');
  return c ? c.children.map(x => [x.className, x.textContent]) : [];
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

{ // 1. the line ends while the socket is down: its TTSState false is sent once the page reconnects, and only once
  const p = page('?audio=1'); p.connect(); p.click('enable'); p.live(LINE); p.audios[0].start(2);
  p.ws().drop(); p.audios[0].end(); p.advance(500);
  p.connect();
  check('lost TTSState false: sent on reconnect', JSON.stringify(p.tts()) === JSON.stringify([['face:audio', false, 'i-1']]), p.tts());
  p.ws().drop(); p.advance(1000); p.connect();
  check('lost TTSState false: sent once', p.tts().length === 0, p.tts());
}
{ // 1b. the line starts while the socket is down: on reconnect the bus hears it is speaking (Director, echo guard)
  const p = page('?audio=1'); p.connect(); p.click('enable'); p.live(LINE); p.ws().drop(); p.audios[0].start(2);
  p.advance(500); p.connect(); p.audios[0].end();
  check('started while down: true on reconnect, then false', JSON.stringify(p.tts()) === JSON.stringify(
    [['face:audio', true, 'i-1'], ['face:audio', false, 'i-1']]), p.tts());
}
{ // 2. the audio tab is closed (or refreshed) mid-line: a beacon ends the line; nothing when the line already ended
  const p = page('?audio=1'); p.connect(); p.click('enable'); p.live(LINE); p.audios[0].start(2);
  p.pagehide();
  check('closed mid-line: beacon TTSState false', JSON.stringify(p.beacons) === JSON.stringify(
    [['/publish', {event: 'TTSState', speaking: false, interjection_id: 'i-1', src: 'face:audio'}]]), p.beacons);
  const q = page('?audio=1'); q.connect(); q.click('enable'); q.live(LINE); q.audios[0].start(2); q.audios[0].end();
  q.pagehide();
  check('closed after the line: no beacon', q.beacons.length === 0, q.beacons);
}
{ // 3. a speaking:true whose false never came (in the Snapshot of a refreshed projector tab) stops showing "Speaking"
  const p = page(''); p.connect([p.snap({event: 'TTSState', speaking: true, interjection_id: 'i-1'})]);
  const fresh = p.speakingShown();
  p.advance(25000);
  check('stuck TTSState true expires', fresh && !p.speakingShown(), {fresh});
  const q = page(''); q.connect(); q.live(LINE); q.live({event: 'TTSState', speaking: true, interjection_id: 'i-1'});
  q.advance(12000); q.draw();
  const held = !q.el('banner').hidden;          // past BANNER_MS, but still being said
  q.advance(13000); q.draw();
  check('banner: held while speaking, not forever', held && q.el('banner').hidden, {held});
}
{ // 4. a new debate (New debate, Replay recorded debate) cuts the old debate's line and drops its queue
  const p = page('?audio=1'); p.connect(); p.click('enable');
  p.live(LINE); p.live({...LINE, id: 'i-2', text: 'Second point.'}); p.audios[0].start(5);
  p.live({event: 'SessionStart', motion: 'Next motion'}); await flush();
  check('SessionStart cuts the voice', p.audios[0].paused && JSON.stringify(p.tts()) === JSON.stringify(
    [['face:audio', true, 'i-1'], ['face:audio', false, 'i-1']]), p.tts());
  p.advance(10000); await flush();              // (the old watchdog would have started i-2 by now)
  check('SessionStart drops the queue', p.audios.length === 1, p.audios.map(a => a.url));
}
{ // 5. ./run.sh replay of a log recorded with an audio tab open: the recorded TTSState never drives this room's view
  const p = page(''); p.connect(); p.live(LINE);
  const until = p.state().banner.until;
  p.live({event: 'TTSState', speaking: true, interjection_id: 'i-1', src: 'replay:face:audio'});
  p.live({event: 'TTSState', speaking: false, interjection_id: 'i-1', src: 'replay:face:audio'});
  check('replayed TTSState ignored', p.state().S.tts === null && p.state().banner.until === until && !p.speakingShown(), p.state().S.tts);
  p.live({event: 'TTSState', speaking: true, interjection_id: 'i-1', src: 'face:audio'});
  check('live TTSState still counts', p.speakingShown());
}
{ // 6. a new DirectorDecision never rebuilds the admin buttons under the operator's pointer (the click would be lost)
  const p = page('?admin=1'); p.connect([p.snap({event: 'SessionStart', motion: 'm'})]); p.draw();
  const box = p.holder('data-ctl="mute"'), writes = box && box.writes;
  p.live({event: 'DirectorDecision', key: 'k1', kind: 'correction', action: 'held', reason: 'awaiting pause', text: 'x'});
  p.draw();
  check('decision: buttons untouched', box && p.holder('data-ctl="mute"') === box && box.writes === writes, box && box.id);
  check('decision: listed with its veto', ((p.holder('data-ctl="veto"') || {}).innerHTML || '').includes('awaiting pause'));
}
{ // 7. "Enable audio" isn't rebuilt by the per-second cooldown countdown, and goes once clicked
  const p = page('?audio=1'); p.connect(); p.live({event: 'DirectorStatus', cooldown_left_s: 10}); p.draw();
  const box = p.holder('id="enable"'), writes = box && box.writes;
  p.live({event: 'DirectorStatus', cooldown_left_s: 9}); p.draw();
  check('countdown: Enable audio untouched', (!box || box.writes === writes) && !p.el('enable').hidden, box && box.id);
  p.click('enable'); p.draw();
  check('Enable audio hidden once clicked', p.el('enable').hidden);
}
{ // 8. side bots: hidden until a bot speaks; each box shows its bot's latest line, tagged, as text (never as HTML)
  const p = page(''); p.connect([p.snap({event: 'SessionStart', motion: 'm'})]); p.live(LINE);
  p.live(BOT('constructor', 'b-0', 'No such bot.')); p.draw();
  check('bots: strip hidden until a bot has spoken', p.el('bots').hidden);
  p.live(BOT('for', 'b-1', 'ONS says 4.2%, not 6%. Try again.', {kind: 'rebut', sourced: true,
    evidence: [{url: 'https://www.ons.gov.uk/x', title: 'ONS Labour market', quote: '4.2%'}]}));
  p.draw();
  check('bots: one bot spoke, the strip shows', !p.el('bots').hidden);
  check('bots: sourced rebut shows its source card, no tag', same(botBox(p, 'for').parts,
    [['who', 'For bot'], ['text', 'ONS says 4.2%, not 6%. Try again.'], ['srccard', '']]), botBox(p, 'for'));
  check('bots: the source card has title, domain and quote', same(srcCard(p, 'for'),
    [['src-title', 'ONS Labour market'], ['src-domain', 'ons.gov.uk'], ['src-quote', '4.2%']]), srcCard(p, 'for'));
  check('bots: the other box waits', same(botBox(p, 'against').parts, [['who', 'Against bot'], ['text empty', 'Nothing yet']]),
    botBox(p, 'against'));
  check('bots: a live line animates in', botBox(p, 'for').cls === 'line enter', botBox(p, 'for').cls);
  p.live(BOT('against', 'b-2', 'It was 3%, not 30%. (unsourced)', {kind: 'challenge'})); p.draw();
  check('bots: an unsourced challenge is tagged AI knowledge', same(botBox(p, 'against').parts,
    [['who', 'Against bot'], ['tag', 'AI knowledge'], ['text', 'It was 3%, not 30%. (unsourced)']]), botBox(p, 'against'));
  p.live(BOT('against', 'b-2c', 'Annoyingly, that one checks out. (unsourced)', {kind: 'concede'})); p.draw();
  check('bots: an unsourced concede is tagged AI knowledge', same(botBox(p, 'against').parts,
    [['who', 'Against bot'], ['tag', 'AI knowledge'], ['text', 'Annoyingly, that one checks out. (unsourced)']]),
    botBox(p, 'against').parts);
  const kept = botBox(p, 'for').node;
  p.live({event: 'BotTalk', persona: 'for', line_id: 'b-1', on: false}); p.draw();
  check('bots: an unchanged line is not rebuilt', botBox(p, 'for').node === kept);
  const evil = '<img src=x onerror=alert(1)>';
  p.live(BOT('for', 'b-3', evil, {kind: 'challenge', sourced: true}));
  p.live(BOT('against', 'b-4', 'And the cost?', {kind: 'rebut', evidence: [{url: 'https://x.org', title: 'X'}]}));
  p.draw();
  check('bots: model text stays text', same(botBox(p, 'for').parts, [['who', 'For bot'], ['text', evil]])
    && ![...p.els.values(), ...p.made].some(n => n.innerHTML.includes('<img')), botBox(p, 'for'));
  check('bots: no tag on a sourced challenge, no source on an unsourced rebut',
    same(botBox(p, 'against').parts, [['who', 'Against bot'], ['text', 'And the cost?']]), botBox(p, 'against'));
  p.live(BOT('for', 'b-5', 'Fine, that checks out.', {kind: 'concede', sourced: true}));
  p.live(BOT('against', 'b-6', 'Still waiting on the cost point.', {kind: 'nudge'})); p.draw();
  check('bots: no tag on a sourced concede or an unsourced nudge',
    same(botBox(p, 'for').parts, [['who', 'For bot'], ['text', 'Fine, that checks out.']])
    && same(botBox(p, 'against').parts, [['who', 'Against bot'], ['text', 'Still waiting on the cost point.']]),
    [botBox(p, 'for').parts, botBox(p, 'against').parts]);
  p.live({event: 'Control', flag: 'screen', value: 'final'}); p.draw();
  check('bots: not on the final screen', p.el('bots').hidden);
}
{ // 8b. the source card: model text stays text; a link that is not http(s) gives no domain; an unsourced line has none
  const p = page(''); p.connect([p.snap({event: 'SessionStart', motion: 'm'})]);
  const evil = '<img src=x onerror=alert(1)>';
  p.live(BOT('for', 'b-1', 'Try again.', {kind: 'rebut', sourced: true,
    evidence: [{url: 'javascript:alert(1)', title: evil, quote: evil}]}));
  p.live(BOT('against', 'b-2', 'It was 3%. (unsourced)', {kind: 'challenge', evidence: [{url: 'https://x.org', title: 'X'}]}));
  p.draw();
  check('source card: title and quote as text', same(srcCard(p, 'for'), [['src-title', evil], ['src-quote', evil]])
    && ![...p.els.values(), ...p.made].some(n => n.innerHTML.includes('<img')), srcCard(p, 'for'));
  check('source card: none on an unsourced challenge', same(srcCard(p, 'against'), []), botBox(p, 'against').parts);
}
{ // 9. a refresh shows each bot's latest line at once, without the animation; a new debate hides the strip
  const p = page('');
  p.connect([p.snap({event: 'SessionStart', motion: 'm'}), p.snap(BOT('for', 'b-1', 'Old line.')),
             p.snap(BOT('for', 'b-2', 'Source for that 40%?')), p.snap(BOT('against', 'b-3', 'Who said so?'))]);
  p.draw();
  check('bots: snapshot shows the latest lines', !p.el('bots').hidden
    && same(botBox(p, 'for').parts, [['who', 'For bot'], ['text', 'Source for that 40%?']])
    && same(botBox(p, 'against').parts, [['who', 'Against bot'], ['text', 'Who said so?']]), [botBox(p, 'for'), botBox(p, 'against')]);
  check('bots: snapshot lines do not animate', botBox(p, 'for').cls === 'line' && botBox(p, 'against').cls === 'line');
  p.live({event: 'SessionStart', motion: 'Next motion'}); p.draw();
  check('bots: a new debate hides the strip', p.el('bots').hidden);
}
{ // 10. admin: a "Side bots" switch only when the bots module runs; B toggles the flag
  const p = page('?admin=1'); p.connect([p.snap({event: 'SessionStart', motion: 'm'})], {bots: 'off'}); p.draw();
  check('bots switch: absent when the module is off', !p.holder('data-ctl="bots"'));
  const q = page('?admin=1'); q.connect([q.snap({event: 'SessionStart', motion: 'm'})], {bots: 'real'}); q.draw();
  check('bots switch: shown when it runs', (q.holder('data-ctl="bots"') || {innerHTML: ''}).innerHTML
    .includes('<button data-ctl="bots" data-val="false">Side bots on</button>'));
  q.key('b');
  check('bots switch: B sends Control bots=false', q.ws().sent.some(m => same(m, {op: 'control', flag: 'bots', value: false})),
    q.ws().sent);
}
// The verdict card: [class, text] of every node under #verdict, depth first.
const verdict = p => { const walk = n => [[n.className, n.textContent], ...n.children.flatMap(walk)];
                       return p.el('verdict').children.flatMap(walk); };
const has = (parts, cls, text) => parts.some(([c, t]) => c === cls && t === text);
const WIN = 'MMD-Conqueror', LOSE = 'MMD-Lost One_s Weeping', DRAW = 'MMD-皆大欢喜-卜逻desu';
const RESULT = {event: 'DebateResult', winner: 'prop', margin: 'clear', headline: 'Priya wins on the evidence.',
  scores: {prop: {factuality: 8, evidence_use: 7.5, consistency: 6.7, overall: 7.5},
           opp: {factuality: 5, evidence_use: 6, consistency: 10, overall: 5.25}},
  report: 'logs/summaries/20261108-run-summary.md', dances: {for: WIN, against: LOSE}, music: {for: true, against: false},
  dance_s: 30, via: 'llm'};
const SESSION = {event: 'SessionStart', motion: 'Ban cars', speakers: {A: {name: 'Priya', side: 'prop'}, B: {name: 'Tom', side: 'opp'}}};
{ // 11. the final screen: "Judging…" after the click, then the verdict card; never on the live screen
  const p = page(''); p.connect([p.snap(SESSION)], {judge: 'real'}); p.draw();
  check('verdict: hidden on the live screen', p.el('verdict').hidden);
  p.live({event: 'Control', flag: 'screen', value: 'final'}); p.draw();
  check('verdict: "Judging…" after the Final screen click', !p.el('verdict').hidden
    && p.el('verdict').className === 'verdict judging' && has(verdict(p), 'winner pulse', 'Judging…'), verdict(p));
  p.live(RESULT); p.draw();
  const parts = verdict(p);
  check('verdict: the winner in its side colour, revealed live', p.el('verdict').className === 'verdict prop reveal'
    && has(parts, 'winner', 'Proposition wins') && has(parts, 'names', 'Priya') && has(parts, 'margin', 'Clear margin'),
    [p.el('verdict').className, parts]);
  check('verdict: overall scores per side', same(parts.filter(([c]) => c === 'prop' || c === 'opp').slice(0, 2),
    [['prop', ''], ['opp', '']]) && has(parts, '', '7.5') && has(parts, '', '5.3') && has(parts, '', 'Proposition'), parts);
  check('verdict: headline and one compact row per criterion, without overall', has(parts, 'headline', 'Priya wins on the evidence.')
    && same(parts.filter(([c]) => ['name', 'prop', 'opp'].includes(c)).slice(-9),
            [['name', 'Factuality'], ['prop', '8'], ['opp', '5'], ['name', 'Evidence use'], ['prop', '7.5'], ['opp', '6'],
             ['name', 'Consistency'], ['prop', '6.7'], ['opp', '10']]),
    parts);
  check('verdict: each bot\'s dance, music only for the winner', has(parts, 'dance prop', `For bot dances: ${WIN}`)
    && has(parts, 'music', ' ♪') && has(parts, 'dance opp', `Against bot dances: ${LOSE}`)
    && parts.filter(([c]) => c === 'music').length === 1, parts);
  check('verdict: the report path, no formula note for an LLM verdict',
    has(parts, 'meta', 'Full summary: logs/summaries/20261108-run-summary.md'), parts);
  const kept = p.el('verdict').children[0];
  p.advance(3000); p.draw();
  check('verdict: an unchanged result is not rebuilt', p.el('verdict').children[0] === kept);
  p.live({event: 'Control', flag: 'screen', value: 'live'}); p.draw();
  check('verdict: hidden again on the live screen', p.el('verdict').hidden && p.el('final').hidden);
  p.live({event: 'Control', flag: 'screen', value: 'final'}); p.draw();
  check('verdict: still there on the next Final screen', !p.el('verdict').hidden && p.el('verdict').children[0] === kept);
  p.live({event: 'SessionStart', motion: 'Next motion'}); p.draw();
  check('verdict: a new debate clears it', p.el('verdict').hidden);
}
{ // 12. "Judging…" only while a judge could answer: the judge module runs, and not forever
  const p = page(''); p.connect([p.snap(SESSION)]); p.live({event: 'Control', flag: 'screen', value: 'final'}); p.draw();
  check('judging: not without the judge module', p.el('verdict').hidden && !p.el('final').hidden);
  const q = page(''); q.connect([q.snap(SESSION)], {judge: 'real'}); q.live({event: 'Control', flag: 'screen', value: 'final'});
  q.advance(119000); q.draw();
  const before = !q.el('verdict').hidden;
  q.advance(2000); q.draw();
  check('judging: gives up after JUDGING_MAX_S', before && q.el('verdict').hidden, {before});
  q.live(RESULT); q.draw();
  check('judging: a late verdict still shows', !q.el('verdict').hidden && has(verdict(q), 'winner', 'Proposition wins'));
}
{ // 12b. the judge answered without a verdict (nobody spoke, judging failed, the module restarted): no "Judging…"
  const JS = (state, detail) => ({event: 'ModuleStatus', module: 'judge', state, detail});
  for (const [state, detail] of [['up', 'nothing to judge: no speech this session'],
                                 ['degraded', "judging failed: RuntimeError('boom')"],
                                 ['up', 'llm: claude-opus-5-5; waiting for the final screen']]) {
    const p = page(''); p.connect([p.snap(SESSION), p.snap(JS('up', 'llm: x; waiting for the final screen'))], {judge: 'real'});
    p.live({event: 'Control', flag: 'screen', value: 'final'}); p.advance(500); p.live(JS(state, detail)); p.draw();
    check(`no verdict coming: no "Judging…" (${detail})`, p.el('verdict').hidden, verdict(p));
  }
  const q = page(''); q.connect([q.snap(SESSION)], {judge: 'real'});
  q.live({event: 'Control', flag: 'screen', value: 'final'}); q.advance(500);
  q.live(JS('up', 'waiting up to 8 s for the record to settle: 1 fact check in flight')); q.draw();
  check('judging: still "Judging…" while the judge works', !q.el('verdict').hidden && has(verdict(q), 'winner pulse', 'Judging…'));
}
{ // 12c. a click after more speech re-judges: the earlier verdict gives way to "Judging…" until the new one lands
  const JS = detail => ({event: 'ModuleStatus', module: 'judge', state: 'up', detail});
  const p = page(''); p.connect([p.snap(SESSION)], {judge: 'real'});
  p.live({event: 'Control', flag: 'screen', value: 'final'}); p.advance(1000); p.live(RESULT); p.draw();
  p.live({event: 'Control', flag: 'screen', value: 'live'}); p.advance(20000);
  p.live({event: 'Control', flag: 'screen', value: 'final'}); p.draw();
  check('re-judge: the earlier verdict stays while the judge has said nothing', has(verdict(p), 'winner', 'Proposition wins'));
  p.advance(100); p.live(JS('judging: 6 utterances, 3 claims')); p.draw();
  check('re-judge: "Judging…" once the judge is at it', has(verdict(p), 'winner pulse', 'Judging…'), verdict(p));
  p.advance(5000); p.live({...RESULT, winner: 'opp', headline: 'Tom came back.'}); p.live(JS('opp wins (clear) via llm')); p.draw();
  check('re-judge: then the new verdict', has(verdict(p), 'winner', 'Opposition wins') && has(verdict(p), 'headline', 'Tom came back.'),
    verdict(p));
}
{ // 13. a refreshed tab (Snapshot) of a real recorded debate shows the verdict at once, without the reveal animation
  const golden = Deno.readTextFileSync(Deno.args[Deno.args.length - 1].replace(/app\/face\/static\/app\.js$/, 'fixtures/golden_run.jsonl'))
    .split('\n').filter(Boolean).map(l => JSON.parse(l));
  const p = page('');
  p.connect([...golden.map(e => p.snap(e)), p.snap({event: 'Control', flag: 'screen', value: 'final'}),
             p.snap({...RESULT, winner: 'draw', margin: '', headline: 'Too close to call.', dances: {for: DRAW, against: DRAW},
                     music: {for: false, against: false}, via: 'formula'})], {judge: 'real'}, {screen: 'final'});
  p.draw();
  const parts = verdict(p);
  check('snapshot: shown at once, no animation', !p.el('verdict').hidden && p.el('verdict').className === 'verdict draw', p.el('verdict').className);
  check('snapshot: a draw, no names, both bots dance the same title in silence', has(parts, 'winner', 'Draw')
    && !parts.some(([c]) => c === 'names') && has(parts, 'margin', 'Too close to call')
    && has(parts, 'dance prop', `For bot dances: ${DRAW}`) && has(parts, 'dance opp', `Against bot dances: ${DRAW}`)
    && !parts.some(([c]) => c === 'music'), parts);
  check('snapshot: the formula noted, after the report path', has(parts, 'meta',
    'Full summary: logs/summaries/20261108-run-summary.md · Decided by the scorecard formula'), parts);
  check('snapshot: the rest of the final screen is drawn', p.el('final-prop').innerHTML.includes('Priya')
    && p.el('final-opp').innerHTML.includes('Tom'), p.el('final-prop').innerHTML.slice(0, 200));
}
{ // 14. model text stays text; a missing title, side or score is left out, never "undefined"
  const evil = '<img src=x onerror=alert(1)>';
  const p = page(''); p.connect([p.snap(SESSION), p.snap({event: 'Control', flag: 'screen', value: 'final'}),
    p.snap({event: 'DebateResult', winner: evil, margin: evil, headline: evil, scores: {prop: {[evil]: evil, overall: 6}},
            report: evil, dances: {for: evil}, music: {}, via: 'llm'})], {}, {screen: 'final'});
  p.draw();
  const parts = verdict(p);
  check('escaping: every string lands as text', has(parts, 'headline', evil) && has(parts, 'name', evil)
    && has(parts, 'prop', evil) && has(parts, 'dance prop', `For bot dances: ${evil}`) && has(parts, 'meta', `Full summary: ${evil}`)
    && ![...p.els.values(), ...p.made].some(n => n.innerHTML.includes('<img')), parts);
  check('escaping: an unknown winner is a draw; gaps are dashes', p.el('verdict').className === 'verdict draw'
    && has(parts, 'winner', 'Draw') && has(parts, 'opp', '–') && has(parts, '', '–')
    && !parts.some(([c, t]) => /undefined|null|NaN/.test(t)) && !parts.some(([c]) => c === 'dance opp'), parts);
}
console.log(failures ? `${failures} FAILED` : 'all passed');
Deno.exit(failures ? 1 : 0);
