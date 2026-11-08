// The arena page (arena.js, with the stage's cast) against a fake DOM, WebSocket and clock: platform params, the
// cards beside the avatars, the centre column, the final screen's states, escaping and hidden-when-empty.
// Run by tests/test_face.py when deno is installed:
//   deno run --allow-read tests/js/arena_page.js app/face/static/stage-cast.js app/face/static/arena.js
const src = Deno.args.map(f => Deno.readTextFileSync(f)).join('\n');
let failures = 0;
function check(name, cond, info) { if (!cond) { failures++; console.log('FAIL', name, JSON.stringify(info)); } else console.log('ok  ', name); }

class El {                                     // just enough DOM for arena.js: innerHTML is recorded, never parsed
  constructor(id = '', tag = 'div') {
    Object.assign(this, {id, tagName: tag, dataset: {}, hidden: false, className: '', title: '', textContent: '',
                         children: [], parentNode: null, _html: ''});
    const props = {};
    this.style = {setProperty: (k, v) => { props[k] = v; }, getPropertyValue: k => props[k] ?? ''};
  }
  set innerHTML(h) { this._html = h; this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(n) { if (n.parentNode) n.remove(); this.children.push(n); n.parentNode = this; return n; }
  replaceChildren(...ns) { for (const c of [...this.children]) c.remove(); ns.forEach(n => this.appendChild(n)); }
  remove() { if (this.parentNode) { const c = this.parentNode.children; c.splice(c.indexOf(this), 1); this.parentNode = null; } }
}
// [className, text] for every node under n that has text, depth first
const parts = n => n.children.flatMap(c => [...(c.textContent ? [[c.className, c.textContent]] : []), ...parts(c)]);
const has = (ps, cls, text) => ps.some(([c, t]) => c === cls && t === text);

function page(search = '') {
  let now = 1.8e12, seq = 0;
  const w = {sockets: [], els: new Map(), made: [], timers: []};
  const el = id => { if (!w.els.has(id)) w.els.set(id, new El(id)); return w.els.get(id); };
  const document = {getElementById: el, createElement: tag => { const n = new El('', tag); w.made.push(n); return n; },
                    body: new El('body'), documentElement: new El('html')};
  class FakeWS {
    constructor(url) { this.url = url; this.sent = []; w.sockets.push(this); }
    send(s) { this.sent.push(s); }
    msg(o) { this.onmessage({data: JSON.stringify(o)}); }
  }
  const env = {location: {search, protocol: 'http:', host: 'h'}, document, WebSocket: FakeWS, URLSearchParams, URL,
               setTimeout: (fn, ms) => w.timers.push([fn, ms]), setInterval: () => 0, Date: {now: () => now}, console};
  const names = Object.keys(env);
  Object.assign(w, new Function(...names, src + '\nreturn {apply, draw, parseSpot, parseWin, state: () => S};')(...names.map(n => env[n])));
  w.el = el;
  w.doc = document;
  w.ws = () => w.sockets[w.sockets.length - 1];
  w.advance = ms => { now += ms; };
  w.now = () => now / 1000;
  w.connect = (events = [], modules = {}, flags = {}) => { w.ws().onopen(); w.ws().msg({event: 'Snapshot', last_seq: seq, flags, modules, events}); };
  w.live = e => w.ws().msg({seq: ++seq, ts: w.now(), ...e});
  w.snap = e => ({seq: ++seq, ts: w.now(), ...e});
  w.card = p => ({hidden: el(`card-${p}`).hidden, cls: el(`card-${p}`).className, parts: parts(el(`card-${p}`))});
  return w;
}
const SESSION = {event: 'SessionStart', motion: 'This house would ban cars from city centres',
                 speakers: {A: {name: 'Priya', side: 'prop'}, B: {name: 'Tom', side: 'opp'}}};
const BOT = (persona, id, text, more = {}) => ({event: 'BotLine', persona, id, text, kind: 'followup', sourced: false, ...more});
const EV = {url: 'https://www.ons.gov.uk/report', title: 'Road traffic estimates 2024', quote: 'Car traffic fell 3% in 2024.'};
const RESULT = {event: 'DebateResult', winner: 'prop', margin: 'clear', headline: 'Priya carried the evidence.',
                scores: {prop: {evidence: 8, overall: 7.5}, opp: {evidence: 5, overall: 6}},
                dances: {for: 'MMD-Conqueror', against: 'MMD-Lost One_s Weeping'}, music: {for: true, against: false}, via: 'llm'};

{ // 1. platform and centre params: px, %, defaults, junk keeps the default, guides
  const p = page('');
  const st = p.doc.documentElement.style;
  check('defaults: under the launcher windows, feet at the window bottom', st.getPropertyValue('--fx') === '23.44vw'
    && st.getPropertyValue('--fy') === '94vh' && st.getPropertyValue('--fw') === '25vw' && st.getPropertyValue('--ax') === '76.56vw'
    && st.getPropertyValue('--ay') === '94vh', [st.getPropertyValue('--fy'), st.getPropertyValue('--ax')]);
  check('defaults: centre column under the header, above the bubbles', ['--cx', '--cy', '--cw', '--ch'].map(k => st.getPropertyValue(k)).join()
    === '50vw,14vh,46vw,28.5vh', ['--cx', '--cy', '--cw', '--ch'].map(k => st.getPropertyValue(k)));
  check('defaults: no guides', p.el('guides').hidden === true);
  const D = ['1', '2', '3'];
  check('parse: px numbers', JSON.stringify(p.parseSpot('450,950,480', D)) === '["450px","950px","480px"]');
  check('parse: % maps x,w to vw and y to vh', JSON.stringify(p.parseSpot('20%, 85.5%,30%', D)) === '["20vw","85.5vh","30vw"]');
  check('parse: mixed and explicit px', JSON.stringify(p.parseSpot('-40px,90%,500', D)) === '["-40px","90vh","500px"]');
  for (const bad of [null, '', '1,2', '1,2,3,4', 'a,b,c', '1,2,0', '1,2,-5', '1em,2,3', '1;2;3', '50%,50%,calc(1px)'])
    check(`parse: junk keeps the default (${bad})`, p.parseSpot(bad, D) === D);
  const C = ['c'];
  check('parse centre: x,y,w,h with %, y and h of the height', JSON.stringify(p.parseSpot('50%,10%,900,25%', C, 'xywh'))
    === '["50vw","10vh","900px","25vh"]');
  for (const bad of ['1,2,3', '1,2,3,4,5', '1,2,3,0', '1,2,0,4', '1,2,3,-4', '1,2,3,x'])
    check(`parse centre: junk keeps the default (${bad})`, p.parseSpot(bad, C, 'xywh') === C);
  check('parseWin', JSON.stringify(p.parseWin('0,200,900,820', null)) === '[0,200,900,820]'
    && p.parseWin('0,200,900', 'd') === 'd' && p.parseWin('0,200,0,820', 'd') === 'd' && p.parseWin('a,b,c,d', 'd') === 'd');
  const q = page('?for=400,900,500&against=80%,90%,20%&guides=1&againstwin=1000,100,920,900&centre=960,120,600,30%');
  const s = q.doc.documentElement.style;
  check('params: set per side', s.getPropertyValue('--fx') === '400px' && s.getPropertyValue('--fy') === '900px'
    && s.getPropertyValue('--fw') === '500px' && s.getPropertyValue('--ax') === '80vw' && s.getPropertyValue('--ay') === '90vh'
    && s.getPropertyValue('--aw') === '20vw');
  check('params: centre column', ['--cx', '--cy', '--cw', '--ch'].map(k => s.getPropertyValue(k)).join() === '960px,120px,600px,30vh');
  const g = q.el('guides');
  const [fw, fb, aw, ab] = g.children;
  check('guides: one outline per avatar window, defaults or overridden', !g.hidden && g.children.length === 4
    && fw.style.left === '0px' && fw.style.width === '900px' && fw.className === 'guide for'
    && aw.style.left === '1000px' && aw.style.height === '900px'
    && aw.textContent === 'Against avatar window 1000,100,920,900', g.children.map(c => c.textContent));
  check('guides: each bubble on its inner side, from 35% of the window down', fb.className === 'guide bubble for'
    && fb.style.left === '450px' && fb.style.width === '442px' && fb.style.top === '487px' && fb.style.height === '533px'
    && ab.style.left === '1008px' && ab.style.width === '452px' && ab.style.top === '415px' && ab.textContent === 'Against bubble, about',
    [fb.style, ab.style]);
  const r = page('?theme=light');
  check('theme=light', r.doc.documentElement.dataset.theme === 'light');
  check('never sends', q.ws().sent.length === 0);
}
{ // 2. hidden when empty: no cards, no U-turns, the idle line, default speakers
  const p = page(''); p.connect();
  check('empty: both cards hidden', p.card('for').hidden && p.card('against').hidden);
  check('empty: centre idle, no U-turn row', !p.el('cidle').hidden && p.el('uturns').hidden && p.el('checks').children.length === 0);
  check('empty: verdict hidden, live column shown', p.el('verdict').hidden && !p.el('live').hidden);
  check('empty: waiting for a motion, side names', p.el('motion').textContent === 'Waiting for a motion'
    && p.el('spk-prop').textContent === 'Proposition' && p.el('spk-opp').textContent === 'Opposition');
  check('empty: platforms unlit', p.el('plat-for').className === 'platform for' && p.el('plat-against').className === 'platform against'
    && p.el('panel-for').className === 'panel for' && p.el('panel-against').className === 'panel against');
}
{ // 3. cards: latest line, AI-knowledge tag, then a citation that stays until a newer citation
  const p = page(''); p.connect([p.snap(SESSION)]);
  check('header: motion and speakers', p.el('motion').textContent === SESSION.motion
    && p.el('spk-prop').textContent === 'Priya' && p.el('spk-opp').textContent === 'Tom');
  p.live(BOT('for', 'b-1', 'Tom, where is the evidence for that?'));
  let c = p.card('for');
  check('line: shown with its text, animated in', !c.hidden && c.cls === 'card line enter'
    && has(c.parts, 'text', 'Tom, where is the evidence for that?') && !c.parts.some(([k]) => k.startsWith('tag')), c);
  check('line: the other card stays hidden', p.card('against').hidden);
  p.live(BOT('for', 'b-2', 'That figure is off by half. (unsourced)', {kind: 'challenge'}));
  c = p.card('for');
  check('line: an unsourced challenge is tagged AI knowledge', has(c.parts, 'tag ai', 'AI knowledge'), c.parts);
  p.live(BOT('against', 'b-3', 'Fine, that one is true. (unsourced)', {kind: 'concede'}));
  check('line: an unsourced concede too, on its own card', has(p.card('against').parts, 'tag ai', 'AI knowledge'));
  p.live(BOT('for', 'b-4', 'The ONS says traffic fell.', {kind: 'rebut', sourced: true, evidence: [EV]}));
  c = p.card('for');
  check('cite: domain tag, title, quote', c.cls === 'card cite enter' && has(c.parts, 'tag src', 'ons.gov.uk')
    && has(c.parts, 'title', EV.title) && has(c.parts, 'quote', `“${EV.quote}”`), c.parts);
  p.live(BOT('for', 'b-5', 'And another thing.', {kind: 'nudge'}));
  check('cite: kept over a newer unsourced line', has(p.card('for').parts, 'title', EV.title));
  p.live(BOT('for', 'b-6', 'Sourced but no evidence', {kind: 'rebut', sourced: true, evidence: []}));
  check('cite: a "sourced" line with no evidence is not a citation', has(p.card('for').parts, 'title', EV.title));
  p.live(BOT('for', 'b-7', 'x', {kind: 'rebut', sourced: true, evidence: [{url: 'javascript:alert(1)', title: 'T2', quote: ''}]}));
  c = p.card('for');
  check('cite: a non-web link shows "Source", no empty quote', has(c.parts, 'title', 'T2') && has(c.parts, 'tag src', 'Source')
    && !c.parts.some(([k]) => k === 'quote'), c.parts);
  const before = p.el('card-for').children[0];
  p.draw();
  check('cards: not rebuilt when unchanged', p.el('card-for').children[0] === before);
  p.live(BOT('nobody', 'b-8', 'stray persona'));
  check('cards: an unknown persona is ignored', !p.made.some(n => n.textContent === 'stray persona'));
  p.live({...SESSION, event: 'SessionStart'});
  check('cards: a new debate clears them', p.card('for').hidden && p.card('against').hidden);
}
{ // 4. a refreshed tab: cards from the Snapshot, without the entry animation
  const p = page(''); p.connect([p.snap(SESSION), p.snap(BOT('against', 'b-1', 'Hello', {kind: 'banter'}))]);
  check('snapshot: card shown still', p.card('against').cls === 'card line' && has(p.card('against').parts, 'text', 'Hello'));
}
{ // 5. centre column: latest checks (newest first, max 4, no unverifiable unless ?all=1), U-turn counts
  const p = page(''); p.connect([p.snap(SESSION)]);
  for (let i = 1; i <= 6; i++) {
    p.live({event: 'Claim', id: `c${i}`, speaker: i % 2 ? 'A' : 'B', text: `claim ${i}`, checkable: true});
    p.live({event: 'Verdict', claim_id: `c${i}`, label: i === 6 ? 'unverifiable' : i % 2 ? 'supported' : 'contradicted', confidence: .9});
  }
  const rows = p.el('checks').children;
  check('checks: newest first, max 4, unverifiable left out', rows.length === 4
    && rows.map(r => parts(r).find(([k]) => k === 'claim')[1]).join('|') === '“claim 5”|“claim 4”|“claim 3”|“claim 2”',
    rows.map(r => parts(r)));
  check('checks: side colour, sign and cast name', rows[0].className === 'check prop' && rows[1].className === 'check opp'
    && has(parts(rows[0]), 'sign supported', '✓ CHECKS OUT') && has(parts(rows[0]), 'who', 'Priya · Sunny')
    && has(parts(rows[1]), 'sign contradicted', '✗ NOPE') && has(parts(rows[1]), 'who', 'Tom · Grumble'), rows.map(r => parts(r)));
  check('checks: the face is the cast SVG', rows[0].children[0].innerHTML.includes('<svg'));
  check('checks: idle line hidden', p.el('cidle').hidden);
  const q = page('?all=1'); q.connect([q.snap(SESSION), q.snap({event: 'Claim', id: 'c1', speaker: 'A', text: 't'}),
    q.snap({event: 'Verdict', claim_id: 'c1', label: 'unverifiable'})]);
  check('checks: ?all=1 lists unverifiable (Shrug)', q.el('checks').children.length === 1
    && has(parts(q.el('checks').children[0]), 'who', 'Priya · Shrug'));
  p.live({event: 'Consistency', id: 'k1', kind: 'self_contradiction', side: 'opp'});
  p.live({event: 'Consistency', id: 'k1', kind: 'self_contradiction', side: 'opp'});   // a revision: counted once
  p.live({event: 'Consistency', id: 'k2', kind: 'agreement', side: 'prop'});
  const u = parts(p.el('uturns'));
  check('uturns: shown, counted once per finding', !p.el('uturns').hidden && has(u, 'prop', '↺ 0') && has(u, 'opp', '1 ↺'), u);
}
{ // 6. final screen: Judging…, then the result (winner glows, loser dims, banner, dances on the cards)
  const p = page(''); p.connect([p.snap(SESSION), p.snap(BOT('for', 'b-1', 'line'))], {judge: 'real'});
  p.live({event: 'Control', flag: 'screen', value: 'final'});
  check('judging: banner, both platforms shimmer, live column hidden', !p.el('verdict').hidden && p.el('live').hidden
    && p.el('verdict').className === 'verdict judging' && has(parts(p.el('verdict')), 'winner pulse', 'Judging…')
    && p.el('plat-for').className === 'platform for judging' && p.el('plat-against').className === 'platform against judging'
    && p.doc.body.className === 'on-final', p.el('plat-for').className);
  p.live(RESULT);
  const v = parts(p.el('verdict'));
  check('result: banner with winner, names, margin, headline, scores', p.el('verdict').className === 'verdict prop reveal'
    && has(v, 'winner', 'Proposition wins') && has(v, 'names', 'Priya') && has(v, 'margin', 'Clear margin')
    && has(v, 'headline', RESULT.headline) && has(v, 'prop', '7.5') && has(v, 'opp', '6') && !v.some(([k]) => k === 'meta'), v);
  check('result: winner glows, loser dims', p.el('plat-for').className === 'platform for win reveal'
    && p.el('plat-against').className === 'platform against lose reveal'
    && p.el('spot-for').className === 'spot for win' && p.el('spot-against').className === 'spot against lose'
    && p.el('panel-for').className === 'panel for win' && p.el('panel-against').className === 'panel against lose');
  const f = p.card('for'), a = p.card('against');
  check('result: each card shows its bot dance', f.cls === 'card dance enter' && has(f.parts, 'tag dance', 'Victory dance')
    && has(f.parts, 'music', '♪ with music') && has(f.parts, 'title', 'MMD-Conqueror')
    && !a.hidden && has(a.parts, 'tag dance', 'Consolation dance') && has(a.parts, 'title', 'MMD-Lost One_s Weeping')
    && !a.parts.some(([k]) => k === 'music'), [f, a]);
  p.live({event: 'Control', flag: 'screen', value: 'live'});
  check('back to live: banner hidden, platforms unlit, card back to the line', p.el('verdict').hidden && !p.el('live').hidden
    && p.el('plat-for').className === 'platform for' && has(p.card('for').parts, 'text', 'line') && p.card('against').hidden
    && p.doc.body.className === '');
}
{ // 7. a draw lights both; a refreshed tab shows it at once; the formula is noted
  const p = page(''); p.connect([p.snap(SESSION), p.snap({event: 'Control', flag: 'screen', value: 'final'}),
    p.snap({...RESULT, winner: 'draw', margin: '', dances: {for: 'D', against: 'D'}, music: {}, via: 'formula'})],
    {judge: 'real'}, {screen: 'final'});
  const v = parts(p.el('verdict'));
  check('draw: both platforms win, no reveal on refresh', p.el('plat-for').className === 'platform for win'
    && p.el('plat-against').className === 'platform against win' && p.el('verdict').className === 'verdict draw');
  check('draw: banner', has(v, 'winner', 'Draw') && has(v, 'margin', 'Too close to call') && !v.some(([k]) => k === 'names')
    && has(v, 'meta', 'Decided by the scorecard formula'), v);
  check('draw: both cards dance the draw', has(p.card('for').parts, 'tag dance', 'Draw dance')
    && has(p.card('against').parts, 'title', 'D') && p.card('for').cls === 'card dance');
}
{ // 8. no judge, or it gave up: the final screen keeps the live column; Judging… expires
  const p = page(''); p.connect([p.snap(SESSION)]);
  p.live({event: 'Control', flag: 'screen', value: 'final'});
  check('no judge: no Judging…, live column stays', p.el('verdict').hidden && !p.el('live').hidden
    && p.el('plat-for').className === 'platform for');
  const q = page(''); q.connect([q.snap(SESSION)], {judge: 'real'});
  q.live({event: 'Control', flag: 'screen', value: 'final'});
  q.advance(121000); q.draw();
  check('judging: expires after 2 minutes', q.el('verdict').hidden && q.el('plat-for').className === 'platform for');
  const r = page(''); r.connect([r.snap(SESSION)], {judge: 'real'});
  r.live({event: 'Control', flag: 'screen', value: 'final'});
  r.live({event: 'ModuleStatus', module: 'judge', state: 'up', detail: 'nothing to judge'});
  check('judging: stops when the judge says nothing is coming', r.el('verdict').hidden);
  const s = page(''); s.connect([s.snap(SESSION)], {judge: 'real'});
  s.live({event: 'Control', flag: 'screen', value: 'final'}); s.live(RESULT);
  s.live({event: 'Control', flag: 'screen', value: 'live'}); s.advance(5000);
  s.live({event: 'Control', flag: 'screen', value: 'final'});
  s.live({event: 'ModuleStatus', module: 'judge', state: 'up', detail: 'judging: 12 claims'});
  check('re-judging: the earlier result gives way to Judging…', s.el('verdict').className === 'verdict judging'
    && s.card('for').hidden, s.el('verdict').className);
}
{ // 9. model text stays text: nothing from the bus reaches innerHTML; gaps never print undefined
  const evil = '<img src=x onerror=alert(1)>';
  const p = page(''); p.connect([p.snap({...SESSION, motion: evil, speakers: {A: {name: evil, side: 'prop'}, B: {name: 'Tom', side: 'opp'}}})],
    {judge: 'real'});
  p.live(BOT('for', 'b-1', evil, {kind: 'challenge'}));
  p.live(BOT('against', 'b-2', evil, {kind: 'rebut', sourced: true, evidence: [{url: `https://e.org/${evil}`, title: evil, quote: evil}]}));
  p.live({event: 'Claim', id: 'c1', speaker: 'A', text: evil});
  p.live({event: 'Verdict', claim_id: 'c1', label: evil});
  const all = [...p.els.values(), ...p.made];
  check('escaping: text lands as text', p.el('motion').textContent === evil && p.el('spk-prop').textContent === evil
    && has(p.card('for').parts, 'text', evil) && has(p.card('against').parts, 'title', evil)
    && has(p.card('against').parts, 'quote', `“${evil}”`) && has(parts(p.el('checks')), 'claim', `“${evil}”`), p.card('against').parts);
  check('escaping: an unknown label is Shrug', has(parts(p.el('checks')), 'sign unverifiable', '? NO IDEA'));
  p.live({event: 'Control', flag: 'screen', value: 'final'});
  p.live({event: 'DebateResult', winner: evil, margin: evil, headline: evil, scores: {prop: {overall: evil}}, dances: {for: evil}, music: {}});
  const v = parts(p.el('verdict'));
  check('escaping: an unknown winner is a draw; a string score is text, as on the dashboard; none is a dash',
    p.el('verdict').className === 'verdict draw reveal'
    && has(v, 'headline', evil) && has(v, 'prop', evil) && has(v, 'opp', '–') && has(p.card('for').parts, 'title', evil)
    && !v.some(([, t]) => /undefined|null|NaN/.test(t)), v);
  check('escaping: no model text in any innerHTML', !all.some(n => n.innerHTML.includes('<img')));
}
console.log(failures ? `${failures} FAILED` : 'all passed');
Deno.exit(failures ? 1 : 0);
