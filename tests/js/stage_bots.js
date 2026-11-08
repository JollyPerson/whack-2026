// The stage (/stage) with the side bots: the whole page (stage.html's scripts, in their load order) against a fake
// DOM and WebSocket, checking each bot's card on its team's half. Run by tests/test_face_bots.py when deno is
// installed: deno run --allow-read tests/js/stage_bots.js app/face/static/{botcard,stage-cast,stage-centre,stage-bots,stage}.js
const src = Deno.args.map(f => Deno.readTextFileSync(f)).join('\n');
let failures = 0;
function check(name, cond, info) { if (!cond) { failures++; console.log('FAIL', name, JSON.stringify(info)); } else console.log('ok  ', name); }

class El {                                     // just enough DOM for the stage: innerHTML is a string, never parsed
  constructor(id = '', tag = 'div') {
    Object.assign(this, {id, tagName: tag, dataset: {}, hidden: false, className: '', title: '', textContent: '',
                         value: '', children: [], parentNode: null, _html: '', sel: new Map()});
    this.style = {setProperty() {}};
    this.classList = {toggle() {}, add() {}, remove() {}, contains: () => false};
  }
  set innerHTML(h) { this._html = h; this.children = []; }
  get innerHTML() { return this._html; }
  get firstChild() { return this.children[0] || (this._stub ||= new El()); }
  get firstElementChild() { return this.firstChild; }
  appendChild(n) { n.remove(); this.children.push(n); n.parentNode = this; return n; }
  replaceChildren(...ns) { for (const c of [...this.children]) c.remove(); ns.forEach(n => this.appendChild(n)); }
  remove() { if (this.parentNode) { const c = this.parentNode.children; c.splice(c.indexOf(this), 1); this.parentNode = null; } }
  querySelector(s) { if (!this.sel.has(s)) this.sel.set(s, new El()); return this.sel.get(s); }
  querySelectorAll() { return []; }
  focus() {}
  reportValidity() {}
}

function page(search = '') {
  let seq = 0;
  const w = {sockets: [], els: new Map(), made: []};
  const el = id => { if (!w.els.has(id)) w.els.set(id, new El(id)); return w.els.get(id); };
  const bySel = new Map();                     // document.querySelector: "#prop .botcard" etc., one element each
  const document = {getElementById: el, createElement: tag => { const n = new El('', tag); w.made.push(n); return n; },
                    querySelector: s => { if (!bySel.has(s)) bySel.set(s, new El()); return bySel.get(s); },
                    body: new El('body'), documentElement: new El('html'), addEventListener() {}};
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; w.sockets.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    msg(o) { this.onmessage({data: JSON.stringify(o)}); }
  }
  FakeWS.OPEN = 1;
  const env = {location: {search, protocol: 'http:', host: 'h'}, document, WebSocket: FakeWS,
               setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, fetch: () => new Promise(() => {}), console};
  const names = Object.keys(env);
  new Function(...names, src)(...names.map(n => env[n]));
  w.card = side => bySel.get(`#${side} .botcard`) || new El();
  w.ws = () => w.sockets[w.sockets.length - 1];
  w.connect = (events = []) => { w.ws().open(); w.ws().msg({event: 'Snapshot', last_seq: seq, flags: {}, events}); };
  w.live = e => w.ws().msg({seq: ++seq, ts: 1.8e9, ...e});
  w.snap = e => ({seq: ++seq, ts: 1.8e9, ...e});
  return w;
}
const SESSION = {event: 'SessionStart', motion: 'm', speakers: {A: {name: 'Ann', side: 'prop'}, B: {name: 'Bob', side: 'opp'}}};
const BOT = (persona, id, text, more = {}) => ({event: 'BotLine', persona, id, text, kind: 'followup', sourced: false, ...more});
const ONS = {url: 'https://www.ons.gov.uk/x', title: 'Labour market overview', quote: 'The rate was 4.2%.'};
// A half's bot card: hidden?, the speech card's class, [class, text] of each part, and of the source card's parts.
const view = (p, side) => {
  const box = p.card(side), n = box.children[0];
  const kids = x => x.children.map(c => [c.className, c.textContent]);
  const src = n && n.children.find(c => c.className === 'srccard');
  return {hidden: box.hidden, cls: n ? n.className : '', node: n, parts: n ? kids(n) : [], src: src ? kids(src) : []};
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

{ // 1. no bot lines: both cards hidden and empty (bots off = the stage as before)
  const p = page(); p.connect([p.snap(SESSION)]);
  p.live({event: 'Utterance', id: 'u-1', speaker: 'A', text: 'Hello', final: true});
  check('no bots: both cards hidden', view(p, 'prop').hidden && view(p, 'opp').hidden
    && !view(p, 'prop').node && !view(p, 'opp').node, [view(p, 'prop'), view(p, 'opp')]);
  p.live(BOT('constructor', 'b-0', 'No such bot.'));
  check('an unknown persona is ignored', view(p, 'prop').hidden && view(p, 'opp').hidden);
}
{ // 2. each bot on its TEAM's half: For on the Proposition half, Against on the Opposition half; tag and source card
  const p = page(); p.connect([p.snap(SESSION)]);
  p.live(BOT('for', 'b-1', 'The ONS says 4.2%, not 6%.', {kind: 'rebut', sourced: true, evidence: [ONS]}));
  const f = view(p, 'prop');
  check('For bot: on the Proposition half, the Against card still hidden', !f.hidden && view(p, 'opp').hidden, f);
  check('For bot: labelled, the line, then the source card', same(f.parts,
    [['who', 'For bot'], ['text', 'The ONS says 4.2%, not 6%.'], ['srccard', '']]), f.parts);
  check('For bot: source card has title, domain and quote', same(f.src,
    [['src-title', 'Labour market overview'], ['src-domain', 'ons.gov.uk'], ['src-quote', 'The rate was 4.2%.']]), f.src);
  check('a live line animates in', f.cls === 'botsay for enter', f.cls);
  p.live(BOT('against', 'b-2', 'It was 3%, not 30%. (unsourced)', {kind: 'challenge'}));
  const a = view(p, 'opp');
  check('Against bot: on the Opposition half, tagged AI knowledge, no source card', !a.hidden && same(a.parts,
    [['who', 'Against bot'], ['tag', 'AI knowledge'], ['text', 'It was 3%, not 30%. (unsourced)']]), a.parts);
  p.live(BOT('against', 'b-3', 'Fair, that checks out. (unsourced)', {kind: 'concede', evidence: [ONS]}));
  check('an unsourced concede: tagged, no source card even with evidence', same(view(p, 'opp').parts,
    [['who', 'Against bot'], ['tag', 'AI knowledge'], ['text', 'Fair, that checks out. (unsourced)']]), view(p, 'opp').parts);
  const kept = view(p, 'prop').node;
  p.live({event: 'BotTalk', persona: 'for', line_id: 'b-1', on: false});
  p.live({event: 'Utterance', id: 'u-2', speaker: 'B', text: 'Well', final: true});
  check('an unchanged line is not rebuilt', view(p, 'prop').node === kept);
  p.live(BOT('for', 'b-4', 'Source for that 40%?', {kind: 'followup', sourced: true}));
  check('sourced with no evidence: no card, no tag', same(view(p, 'prop').parts,
    [['who', 'For bot'], ['text', 'Source for that 40%?']]), view(p, 'prop').parts);
  p.live(SESSION);
  check('a new debate hides both cards', view(p, 'prop').hidden && view(p, 'opp').hidden);
}
{ // 3. model text stays text: nothing reaches innerHTML
  const p = page(); p.connect([p.snap(SESSION)]);
  const evil = '<img src=x onerror=alert(1)>';
  p.live(BOT('against', 'b-1', evil, {kind: 'rebut', sourced: true,
    evidence: [{url: 'javascript:alert(1)', title: evil, quote: evil}]}));
  const a = view(p, 'opp');
  check('escaping: text, title and quote land as text', same(a.parts.slice(0, 2), [['who', 'Against bot'], ['text', evil]])
    && same(a.src, [['src-title', evil], ['src-quote', evil]]), a);
  check('escaping: no markup anywhere', ![...p.els.values(), ...p.made].some(n => n.innerHTML.includes('<img')));
}
{ // 4. a refresh (Snapshot) shows each bot's latest line at once, without the animation
  const p = page();
  p.connect([p.snap(SESSION), p.snap(BOT('for', 'b-1', 'First.')), p.snap(BOT('for', 'b-2', 'Latest.')),
             p.snap(BOT('against', 'b-3', 'Who said so?', {kind: 'nudge'}))]);
  const f = view(p, 'prop'), a = view(p, 'opp');
  check('snapshot: latest lines shown', !f.hidden && !a.hidden && same(f.parts, [['who', 'For bot'], ['text', 'Latest.']])
    && same(a.parts, [['who', 'Against bot'], ['text', 'Who said so?']]), [f.parts, a.parts]);
  check('snapshot: no animation', f.cls === 'botsay for' && a.cls === 'botsay against', [f.cls, a.cls]);
  const q = page(); q.connect([q.snap(SESSION), q.snap(BOT('for', 'b-1', 'Old.')), q.snap(SESSION)]);
  check('snapshot: lines from an earlier debate are not shown', view(q, 'prop').hidden);
}
{ // 5. the shared card: the stage and the dashboard use the same names, tag and source parts
  const BotCard = new Function('document', Deno.readTextFileSync(Deno.args[0]) + '\nreturn BotCard;')(
    {createElement: tag => new El('', tag)});
  check('shared: names', same(BotCard.NAMES, {for: 'For bot', against: 'Against bot'}) && same(BotCard.SIDE, {for: 'prop', against: 'opp'}));
  check('shared: tag only on unsourced challenge/concede', BotCard.tagOf({kind: 'challenge'}) === 'AI knowledge'
    && BotCard.tagOf({kind: 'concede'}) === 'AI knowledge' && BotCard.tagOf({kind: 'challenge', sourced: true}) === ''
    && BotCard.tagOf({kind: 'rebut'}) === '' && BotCard.tagOf({kind: 'banter'}) === '');
  const long = 'word '.repeat(80);
  const s = BotCard.sourceOf({sourced: true, evidence: [{}, {url: 'notaurl', title: long, quote: long}]});
  check('shared: first usable evidence, title and quote clipped', s && s.domain === '' && s.title.length <= 91
    && s.quote.length <= 161 && s.title.endsWith('…'), s);
  check('shared: nothing for unsourced or empty evidence', BotCard.sourceOf({evidence: [ONS]}) === null
    && BotCard.sourceOf({sourced: true, evidence: []}) === null && BotCard.sourceOf({sourced: true}) === null);
}
console.log(failures ? `${failures} FAILED` : 'all passed');
Deno.exit(failures ? 1 : 0);
