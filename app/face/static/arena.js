'use strict';
// Arena: the full-screen backdrop the two side-bot avatars stand on (Mate Engine windows, always on top, see
// tools/mate-bots.sh). For (Prop's second) gets a platform bottom left, Against (Opp's second) bottom right, a disc
// under the avatar's feet. Each avatar draws its bubble beside its head on the INNER side (towards the middle), growing
// downwards, so each bot's panel (name plate, then a card: its latest sourced citation (domain, title, quote), else its
// latest line, tagged as in the dashboard's strip) sits on the OUTER side of its avatar, and nothing is drawn under the
// avatars. The centre column (latest fact checks, U-turn counts) sits between the header and the top of the bubbles,
// where nothing of the avatars reaches. Final screen (Control screen=final):
// "Judging…", then the winner's platform glows (a draw lights both), a verdict banner, and each bot's dance on its card.
// Display only: it never sends. Model text goes in with textContent only. Protocol (Snapshot + seq + reconnect) as in
// stage.js; the cast (Sunny, Grumble, Professor Hoot…) comes from stage-cast.js.
const Q = new URLSearchParams(location.search);
const WS_URL = Q.get('ws') || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const SHOW_ALL = Q.get('all') === '1';          // also list "unverifiable" checks
const CHECKS_MAX = 4;
const JUDGING_MAX_S = 120;                       // as app.js: "Judging…" this long after the Final screen click at most
const JUDGE_BUSY = /^(waiting up to |judging: )/;   // the judge's ModuleStatus while it works on a click
const SIDE_NAMES = {prop: 'Proposition', opp: 'Opposition'};
const BOTS = {for: {label: 'For', side: 'prop'}, against: {label: 'Against', side: 'opp'}};
const MARGINS = {narrow: 'Narrow margin', clear: 'Clear margin', decisive: 'Decisive margin'};
// Platforms sit under the launcher's default avatar windows (0,200,900,820 and 1020,200,900,820 on 1920x1080), the
// avatar standing in the middle at the bottom of its window: centre x 450 / 1470 px, feet at 1015 px, 480 px wide. The
// feet line comes from the players' logs (player.log "bubble shown ... right of the head at (x,y)"): the head bone is
// 386-441 px above the window bottom, and the bubble's offsets (0.15 / 0.25 avatar metres = 56 / 93 px) give about
// 373 px per avatar metre, so the head bone is 1.05-1.18 avatar metres up: the feet stand at the window bottom (1020).
const SPOT_DEFAULTS = {for: ['23.44vw', '94vh', '25vw'], against: ['76.56vw', '94vh', '25vw']};
// The centre column: centre x, top, width, height. The same logs put the bubbles' tops at screen y 472-541 (outer
// corners 0.25 avatar metres above the head, KWin's 14 px included), and they grow downwards from there, into the
// middle up to the windows' inner edges (892 / 1028 px), so the column ends at 459 px, under the header at 151 px.
const CENTRE_DEFAULT = ['50vw', '14vh', '46vw', '28.5vh'];
const WIN_DEFAULTS = {for: [0, 200, 900, 820], against: [1020, 200, 900, 820]};
if (Q.get('theme') === 'light') document.documentElement.dataset.theme = 'light';

const $ = id => document.getElementById(id);
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : null;
const host = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return ''; } };
const clip = (s, n) => { s = String(s ?? '').replace(/\s+/g, ' ').trim();
  return s.length <= n ? s : s.slice(0, s.lastIndexOf(' ', n - 1) > n * .6 ? s.lastIndexOf(' ', n - 1) : n - 1) + '…'; };
const scoreText = v => typeof v === 'number' && Number.isFinite(v) ? (Number.isInteger(v) ? String(v) : v.toFixed(1))
                 : typeof v === 'string' && v ? v : '–';   // as app.js's verdict card

// ------------------------------------------------------------------ platform positions (URL params)
// "x,y,w" (a platform: centre x, feet line y, width) or, with axes 'xywh', "x,y,w,h" (the centre column: centre x,
// top, width, height). A bare number (or "px") is pixels; "%" makes x and w a share of the screen width and y and h a
// share of its height. Anything else, or a width or height that is not positive, keeps the default, so a typo never
// hides anything.
function parseSpot(raw, def, axes = 'xyw') {
  const parts = String(raw ?? '').split(',').map(s => s.trim());
  if (raw == null || parts.length !== axes.length) return def;
  const out = [];
  for (const [i, a] of [...axes].entries()) {
    const m = /^(-?\d+(?:\.\d+)?)(px|%)?$/.exec(parts[i]);
    if (!m || ('wh'.includes(a) && !(+m[1] > 0))) return def;
    out.push(m[2] === '%' ? `${+m[1]}${'yh'.includes(a) ? 'vh' : 'vw'}` : `${+m[1]}px`);
  }
  return out;
}
function parseWin(raw, def) {             // "x,y,w,h" in px, as the launcher's FOR_GEOM / AGAINST_GEOM
  const n = String(raw ?? '').split(',').map(s => s.trim());
  return raw != null && n.length === 4 && n.every(x => /^-?\d+$/.test(x)) && +n[2] > 0 && +n[3] > 0 ? n.map(Number) : def;
}
const SPOTS = {for: parseSpot(Q.get('for'), SPOT_DEFAULTS.for), against: parseSpot(Q.get('against'), SPOT_DEFAULTS.against)};
const CENTRE = parseSpot(Q.get('centre'), CENTRE_DEFAULT, 'xywh');
function guide(cls, text, x, y, w, h) {
  const g = document.createElement('div');
  g.className = cls;
  Object.assign(g.style, {left: `${Math.round(x)}px`, top: `${Math.round(y)}px`, width: `${Math.round(w)}px`, height: `${Math.round(h)}px`});
  g.textContent = text;
  return g;
}
function placePlatforms() {
  const st = document.documentElement.style;
  for (const [p, k] of [['for', 'f'], ['against', 'a']]) {
    ['x', 'y', 'w'].forEach((v, i) => st.setProperty(`--${k}${v}`, SPOTS[p][i]));
  }
  ['x', 'y', 'w', 'h'].forEach((v, i) => st.setProperty(`--c${v}`, CENTRE[i]));
  const guides = $('guides');
  guides.hidden = Q.get('guides') !== '1';
  if (guides.hidden) return;
  // Each avatar window, and roughly where its bubble can be (from the players' logs): from the middle of the window
  // to its inner edge, from 35% of its height down to its bottom.
  guides.replaceChildren(...Object.keys(BOTS).flatMap(p => {
    const [x, y, w, h] = parseWin(Q.get(`${p}win`), WIN_DEFAULTS[p]), inner = BOTS[p].side === 'prop';
    return [guide(`guide ${p}`, `${BOTS[p].label} avatar window ${x},${y},${w},${h}`, x, y, w, h),
            guide(`guide bubble ${p}`, `${BOTS[p].label} bubble, about`, inner ? x + w / 2 : x + 8, y + h * .35, w / 2 - 8, h * .65)];
  }));
}

// ------------------------------------------------------------------ state + reducer
let S = null, MODULES = {}, ws = null, lastSeq = 0, backoff = 500;

function reset() {   // SessionStart wipes debate state; flags + module status are process-wide
  S = {session: null, sides: {}, names: {}, claims: new Map(), verdicts: new Map(), order: [], findings: new Set(),
       uturns: {prop: 0, opp: 0}, bots: {}, result: null, finalAt: null, flags: S ? S.flags : {}, status: S ? S.status : {}};
}
function sideOf(spk) {   // 1st speaker heard = prop, 2nd = opp, unless SessionStart said otherwise
  if (!(spk in S.sides)) S.sides[spk] = Object.keys(S.sides).length % 2 ? 'opp' : 'prop';
  return S.sides[spk];
}
const nameOf = spk => S.names[spk] || spk;
const shows = v => v && (SHOW_ALL || v.label !== 'unverifiable');
// A sourced line's first usable source: it has a web link, a title or a quote.
const citationOf = e => e.sourced ? (e.evidence || []).find(x => x && (safeUrl(x.url) || x.title || x.quote)) || null : null;

function apply(e, live) {
  switch (e.event) {
    case 'SessionStart':
      reset(); S.session = e;
      for (const [id, sp] of Object.entries(e.speakers || {})) {
        if (sp.side) S.sides[id] = sp.side;
        S.names[id] = sp.name || id;
      }
      break;
    case 'Utterance': sideOf(e.speaker); break;
    case 'Claim': sideOf(e.speaker); S.claims.set(e.id, e); break;
    case 'Verdict': if (!S.verdicts.has(e.claim_id)) S.order.push(e.claim_id); S.verdicts.set(e.claim_id, e); break;
    case 'Consistency':
      if (!S.findings.has(e.id) && e.kind === 'self_contradiction' && e.side in S.uturns) S.uturns[e.side]++;
      S.findings.add(e.id);
      break;
    case 'ModuleStatus': S.status[e.module] = e; break;
    case 'Control':
      S.flags[e.flag] = e.value;
      if (e.flag === 'screen') S.finalAt = e.value === 'final' ? e.ts : null;   // when the judge was asked
      break;
    case 'DebateResult': S.result = {e, live}; break;                            // live: revealed with an animation
    case 'BotLine': {
      if (!Object.hasOwn(BOTS, e.persona)) break;
      const b = S.bots[e.persona] || (S.bots[e.persona] = {});
      b.line = {e, live};
      const ev = citationOf(e);
      if (ev) b.cite = {e, ev, live};
      break;
    }
  }
}

// The final screen's state, as the dashboard decides it (app.js drawVerdict): the judge's result, or "Judging…" while
// the judge module runs, for JUDGING_MAX_S after the click at most, and not once its status since the click says no
// verdict is coming; a re-judging click hides the earlier result.
function finalState() {
  if (S.flags.screen !== 'final') return {mode: 'live'};
  const clicked = S.finalAt != null;
  const judgeOn = (MODULES.judge && MODULES.judge !== 'off') || !!S.status.judge;
  const said = clicked && S.status.judge && S.status.judge.ts >= S.finalAt ? S.status.judge : null;
  const busy = !!said && said.state === 'up' && JUDGE_BUSY.test(said.detail || '');
  const r = S.result && busy && S.result.e.ts < S.finalAt ? null : S.result;
  if (r) return {mode: 'result', r, winner: ['prop', 'opp'].includes(r.e.winner) ? r.e.winner : 'draw'};
  const judging = judgeOn && clicked && Date.now() / 1000 - S.finalAt < JUDGING_MAX_S && (!said || busy);
  return {mode: judging ? 'judging' : 'final'};
}

// ------------------------------------------------------------------ drawing (textContent only for model text)
function part(parent, tag, cls, text) {
  const x = parent.appendChild(document.createElement(tag));
  x.className = cls;
  if (text != null) x.textContent = text;
  return x;
}
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
const speakersOf = side => Object.keys(S.sides).filter(s => S.sides[s] === side).map(nameOf);

function drawCard(persona, fin) {
  const box = $(`card-${persona}`), b = S.bots[persona] || {}, bot = BOTS[persona];
  const dance = fin.mode === 'result' && String((fin.r.e.dances || {})[persona] || '');
  const key = dance ? `dance ${fin.r.e.seq}` : b.cite ? `cite ${b.cite.e.id}` : b.line ? `line ${b.line.e.id}` : '';
  box.hidden = !key;
  if (box.dataset.key === key) return;    // unchanged: no rebuild, no animation
  box.dataset.key = key;
  box.replaceChildren();
  if (!key) { box.className = 'card'; return; }
  if (dance) {
    const won = fin.winner === bot.side, music = !!(fin.r.e.music || {})[persona];
    box.className = `card dance${fin.r.live ? ' enter' : ''}`;
    const row = part(box, 'div', 'row');
    part(row, 'span', 'tag dance', fin.winner === 'draw' ? 'Draw dance' : won ? 'Victory dance' : 'Consolation dance');
    if (music) part(row, 'span', 'music', '♪ with music');
    part(box, 'p', 'title', dance);
    return;
  }
  if (b.cite) {
    const {ev, live} = b.cite, url = safeUrl(ev.url);
    box.className = `card cite${live ? ' enter' : ''}`;
    part(part(box, 'div', 'row'), 'span', 'tag src', (url && host(url)) || 'Source');   // the source's domain
    if (ev.title) part(box, 'p', 'title', clip(ev.title, 110));
    if (ev.quote) part(box, 'blockquote', 'quote', `“${clip(ev.quote, 200)}”`);
    return;
  }
  const {e, live} = b.line;
  box.className = `card line${live ? ' enter' : ''}`;
  if (!e.sourced && (e.kind === 'challenge' || e.kind === 'concede')) part(part(box, 'div', 'row'), 'span', 'tag ai', 'AI knowledge');
  part(box, 'p', 'text', clip(e.text, 200));
}

function drawVerdict(fin) {
  const box = $('verdict'), on = fin.mode === 'result' || fin.mode === 'judging';
  box.hidden = !on;
  $('live').hidden = on;
  const key = fin.mode === 'result' ? `result ${fin.r.e.seq}` : on ? 'judging' : '';
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren();
  if (!on) { box.className = 'verdict'; return; }
  if (fin.mode === 'judging') {
    box.className = 'verdict judging';
    part(box, 'b', 'winner pulse', 'Judging…');
    part(box, 'p', 'meta', 'Weighing the scorecard and the transcript');
    return;
  }
  const e = fin.r.e, w = fin.winner, scores = e.scores || {};
  box.className = `verdict ${w}${fin.r.live ? ' reveal' : ''}`;
  part(box, 'p', 'kicker', 'The verdict');
  part(box, 'b', 'winner', w === 'draw' ? 'Draw' : `${SIDE_NAMES[w]} wins`);
  const names = w === 'draw' ? [] : speakersOf(w);
  if (names.length) part(box, 'p', 'names', names.join(' & '));
  const margin = w === 'draw' ? 'Too close to call' : MARGINS[e.margin] || (e.margin ? `${e.margin} margin` : '');
  if (margin) part(box, 'p', 'margin', margin);
  if (e.headline) part(box, 'p', 'headline', e.headline);
  if (scores.prop || scores.opp) {
    const row = part(box, 'p', 'overall');
    part(row, 'b', 'prop', scoreText((scores.prop || {}).overall));
    part(row, 'span', 'label', 'overall');
    part(row, 'b', 'opp', scoreText((scores.opp || {}).overall));
  }
  if (e.via === 'formula') part(box, 'p', 'meta', 'Decided by the scorecard formula');
}

function drawChecks() {
  const ids = S.order.filter(id => shows(S.verdicts.get(id)) && S.claims.has(id)).slice(-CHECKS_MAX).reverse();
  const list = $('checks');
  $('cidle').hidden = ids.length > 0;
  const key = ids.map(id => `${id}:${S.verdicts.get(id).label}`).join(' ');
  if (list.dataset.key !== key) {
    list.dataset.key = key;
    list.replaceChildren(...ids.map(id => {
      const v = S.verdicts.get(id), c = S.claims.get(id), who = castOf(v.label);
      const side = c.side || S.sides[c.speaker];
      const row = document.createElement('div');
      row.className = `check ${['prop', 'opp'].includes(side) ? side : 'noside'}`;
      const face = part(row, 'span', `face ${CAST[v.label] ? v.label : 'unverifiable'}`);
      face.innerHTML = who.svg;            // the cast's own constant SVG, never model text
      const body = part(row, 'span', 'body');
      const head = part(body, 'span', 'head');
      part(head, 'span', `sign ${CAST[v.label] ? v.label : 'unverifiable'}`, `${who.icon} ${who.sign}`);
      part(head, 'span', 'who', `${nameOf(c.speaker)} · ${who.name}`);
      part(body, 'span', 'claim', `“${clip(c.text, 110)}”`);
      return row;
    }));
  }
  const u = S.uturns, box = $('uturns');
  box.hidden = !(u.prop || u.opp);
  const ukey = `${u.prop}:${u.opp}`;
  if (box.dataset.key !== ukey) {
    box.dataset.key = ukey;
    box.replaceChildren();
    part(box, 'span', 'prop', `↺ ${u.prop}`);
    part(box, 'span', 'label', 'U-turns');
    part(box, 'span', 'opp', `${u.opp} ↺`);
  }
}

function draw() {
  setText($('motion'), S.session ? S.session.motion || 'Open debate' : 'Waiting for a motion');
  for (const side of ['prop', 'opp']) setText($(`spk-${side}`), speakersOf(side).join(' & ') || SIDE_NAMES[side]);
  const fin = finalState();
  for (const [p, bot] of Object.entries(BOTS)) {
    const glow = fin.mode === 'judging' ? ' judging'
      : fin.mode === 'result' ? (fin.winner === 'draw' || fin.winner === bot.side ? ' win' : ' lose') : '';
    const live = fin.mode === 'result' && fin.r.live ? ' reveal' : '';
    $(`plat-${p}`).className = `platform ${p}${glow}${glow ? live : ''}`;
    $(`spot-${p}`).className = `spot ${p}${glow}`;
    $(`panel-${p}`).className = `panel ${p}${glow}`;
    drawCard(p, fin);
  }
  document.body.className = fin.mode === 'live' ? '' : 'on-final';
  drawVerdict(fin);
  drawChecks();
}
setInterval(() => { if (S) draw(); }, 1000);   // "Judging…" expires on the clock

// ------------------------------------------------------------------ transport (as stage.js; this page never sends)
function setConn(s) { $('conn').className = `dot ${s}`; $('conn').title = `websocket ${s}: ${WS_URL}`; }
function connect() {
  setConn('connecting');
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { backoff = 500; setConn('open'); };
  ws.onmessage = m => {
    let e;
    try { e = JSON.parse(m.data); } catch { return; }
    if (e.event === 'Snapshot') {              // first message after every (re)connect: rebuild quietly
      S = null; reset();
      for (const h of e.events || []) apply(h, false);
      S.flags = {...(e.flags || {})}; MODULES = e.modules || {};
      lastSeq = e.last_seq || 0;
    } else if (e.seq == null) {
      return;
    } else if (e.seq > lastSeq) {
      lastSeq = e.seq; apply(e, true);
    } else return;
    draw();
  };
  ws.onclose = () => {
    setConn('closed');
    setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 5000);
  };
}
placePlatforms(); reset(); draw(); connect();
