'use strict';
// Stage view: proposition on the left, opposition on the right. Each Verdict pops a character (stage-cast.js)
// onto the side that made the claim, holding a sign, with a speech bubble that gives the context and the source.
// Protocol (Snapshot + seq + reconnect) follows the reference client app/tools/ws_debug.html.
const Q = new URLSearchParams(location.search);
const WS_URL = Q.get('ws') || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const SHOW_ALL = Q.get('all') === '1';          // also pop "unverifiable" (no evidence either way)
const MIN_DWELL_MS = 9000, MAX_DWELL_MS = 20000; // how long one character stays up
const EXIT_MS = 450;                             // matches the .leaving animation in stage.css
const HISTORY_MAX = 8;
const SIDES = ['prop', 'opp'];
if (Q.get('theme') === 'light') document.documentElement.dataset.theme = 'light';

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : null;
const host = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return ''; } };
const clip = (s, n) => { s = String(s || '').replace(/\s+/g, ' ').trim();
  return s.length <= n ? s : s.slice(0, s.lastIndexOf(' ', n - 1) > n * .6 ? s.lastIndexOf(' ', n - 1) : n - 1) + '…'; };
const fmt = x => x == null ? '?' : Math.abs(x) >= 1000 ? Math.round(x).toLocaleString('en-GB') : String(+x.toFixed(2));

// ------------------------------------------------------------------ state + reducer
let S = null, ws = null, lastSeq = 0, backoff = 500;
const ui = Object.fromEntries(SIDES.map(s => [s, {queue: [], active: null, history: []}]));

function reset() {
  S = {session: null, sides: {}, names: {}, utts: new Map(), claims: new Map(), verdicts: new Map()};
  for (const side of SIDES) {
    const u = ui[side];
    if (u.active) u.active.el.remove();
    u.queue = []; u.active = null; u.history = [];
  }
}
function sideOf(spk) {   // 1st speaker heard = prop, 2nd = opp, unless SessionStart said otherwise
  if (!(spk in S.sides)) S.sides[spk] = Object.keys(S.sides).length % 2 ? 'opp' : 'prop';
  return S.sides[spk];
}
const nameOf = spk => S.names[spk] || spk;
const shows = v => v && (SHOW_ALL || v.label !== 'unverifiable');

function apply(e, live) {
  switch (e.event) {
    case 'SessionStart':
      reset(); S.session = e;
      for (const [id, sp] of Object.entries(e.speakers || {})) {
        if (sp.side) S.sides[id] = sp.side;
        S.names[id] = sp.name || id;
      }
      break;
    case 'Utterance': sideOf(e.speaker); S.utts.set(e.id, e); break;
    case 'Claim': sideOf(e.speaker); S.claims.set(e.id, e); if (S.verdicts.has(e.id)) onVerdict(e.id, live); break;
    case 'Verdict': S.verdicts.set(e.claim_id, e); onVerdict(e.claim_id, live); break;
  }
}

// A verdict (or a better one for the same claim) arrived: queue its character, or refresh the one on stage.
function onVerdict(id, live) {
  const c = S.claims.get(id), v = S.verdicts.get(id);
  if (!c) return;
  const side = c.side || sideOf(c.speaker), u = ui[side];
  if (!u) return;
  if (!shows(v)) { u.queue = u.queue.filter(x => x !== id); return; }
  if (!live) { pushHistory(u, id); return; }           // history from the Snapshot: no popping on refresh
  if (u.active && u.active.id === id) { showNext(side, id, true); return; }
  if (!u.queue.includes(id)) u.queue.push(id);
  tick();
}
function pushHistory(u, id) {
  if (!u.history.includes(id)) u.history.push(id);   // arrival order: re-showing one doesn't move it
}

// ------------------------------------------------------------------ the stage: one character per side at a time
function dwellFor(id, queued) {
  const v = S.verdicts.get(id), words = `${v.explanation} ${v.short_fact}`.split(/\s+/).length;
  const ms = Math.min(MAX_DWELL_MS, Math.max(MIN_DWELL_MS, 6000 + words * 260));
  return queued ? Math.max(MIN_DWELL_MS * .7, ms * .6) : ms;   // speed up when claims pile up
}
function showNext(side, id, animate = true) {
  const u = ui[side], stage = document.querySelector(`#${side} .stage`);
  if (u.active) leave(u.active, side);
  const el = document.createElement('div');
  el.innerHTML = actorHtml(id, side);
  const actor = el.firstElementChild;
  if (!animate) actor.classList.add('still');
  stage.appendChild(actor);
  const dwell = dwellFor(id, u.queue.length > 0);
  actor.style.setProperty('--dwell', `${dwell}ms`);
  u.active = {id, el: actor, until: Date.now() + dwell};
  pushHistory(u, id);
  draw();
}
function leave(active, side) {
  active.el.classList.add('leaving');
  setTimeout(() => active.el.remove(), EXIT_MS);
  if (ui[side].active === active) ui[side].active = null;
}
function tick() {
  const now = Date.now();
  for (const side of SIDES) {
    const u = ui[side];
    if (u.active && now >= u.active.until) { leave(u.active, side); draw(); }
    if (!u.active && u.queue.length) showNext(side, u.queue.shift());
  }
}
setInterval(tick, 250);

function bodyOf(v) {
  const ex = String(v.explanation || '');
  return clip(ex && !ex.startsWith('[mock]') ? ex : v.short_fact || ex, 260);
}
function actorHtml(id, side) {
  const c = S.claims.get(id), v = S.verdicts.get(id), who = castOf(v.label);
  const ev = (v.evidence || [])[0] || {}, url = safeUrl(ev.url);
  const n = v.numeric && (v.numeric.claimed != null || v.numeric.actual != null) ? v.numeric : null;
  return `<div class="actor ${esc(v.label)}" data-claim="${esc(id)}">
    <div class="figure">
      <div class="sign"><span class="icon">${who.icon}</span>${esc(who.sign)}</div>
      <div class="art-wrap">${who.svg}</div>
      <div class="nametag">${esc(who.name)} <small>${esc(who.role)}</small></div>
    </div>
    <div class="bubble">
      <p class="opener">${esc(openerOf(v.label, id))}</p>
      <p class="said"><b>${esc(nameOf(c.speaker))}:</b> “${esc(clip(c.text, 140))}”</p>
      <p class="context">${esc(bodyOf(v))}</p>
      ${n ? `<p class="numbers"><span>claimed <b>${fmt(n.claimed)}</b></span><span class="arrow">→</span>`
          + `<span>sources <b>${fmt(n.actual)}</b></span> <small>${esc(n.unit)}</small></p>` : ''}
      ${ev.quote ? `<blockquote>“${esc(clip(ev.quote, 150))}”</blockquote>` : ''}
      ${url ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(host(url))}`
          + `${ev.title ? ` · ${esc(clip(ev.title, 60))}` : ''}</a>` : ''}
      <div class="meta">${Math.round((v.confidence || 0) * 100)}% sure${v.via && v.via !== 'none' ? ` · via ${esc(v.via)}` : ''}</div>
      <div class="timer"></div>
    </div>
  </div>`;
}

// ------------------------------------------------------------------ the rest of each half (cheap, re-rendered)
function setHtml(el, html) { if (el.$html !== html) { el.innerHTML = html; el.$html = html; } }
function faceHtml(label) { return `<span class="face ${esc(label)}">${castOf(label).svg}</span>`; }

function draw() {
  $('motion').textContent = S.session ? S.session.motion || 'Open debate' : 'Waiting for a motion';
  for (const side of SIDES) {
    const box = $(side), u = ui[side];
    const speakers = Object.keys(S.sides).filter(s => S.sides[s] === side).map(nameOf);
    setHtml(box.querySelector('.speakers'), esc(speakers.join(' & ') || '—'));

    const counts = {supported: 0, contradicted: 0, missing_context: 0};
    let checking = 0;
    for (const c of S.claims.values()) {
      if ((c.side || S.sides[c.speaker]) !== side) continue;
      const v = S.verdicts.get(c.id);
      if (v && v.label in counts) counts[v.label]++;
      else if (!v && c.checkable && Date.now() / 1000 - (c.ts || 0) < 25) checking++;
    }
    setHtml(box.querySelector('.tally'), Object.entries(counts).map(([k, n]) =>
      `<span class="count ${k}" title="${esc(castOf(k).name)}">${faceHtml(k)}<b>${n}</b></span>`).join(''));

    const last = [...S.utts.values()].filter(x => S.sides[x.speaker] === side).pop();
    const cap = box.querySelector('.caption');
    setHtml(cap, last ? `<b>${esc(nameOf(last.speaker))}</b> ${esc(clip(last.text, 220))}` : '');
    cap.classList.toggle('interim', !!last && last.final === false);

    const stage = box.querySelector('.stage');
    let idle = stage.querySelector('.idle');
    if (!idle) { idle = document.createElement('p'); idle.className = 'idle'; stage.appendChild(idle); }
    idle.hidden = !!u.active;
    setHtml(idle, checking ? `<span class="dots"></span> checking ${checking} claim${checking > 1 ? 's' : ''}…`
                           : 'Waiting for a claim to check');

    setHtml(box.querySelector('.history'), u.history.slice(-HISTORY_MAX).reverse().map(id => {
      const v = S.verdicts.get(id), c = S.claims.get(id);
      if (!v || !c) return '';
      return `<button class="past ${esc(v.label)}${u.active && u.active.id === id ? ' on' : ''}" data-side="${side}"`
        + ` data-claim="${esc(id)}" title="${esc(c.text)}">${faceHtml(v.label)}<span>${esc(clip(c.text, 70))}</span></button>`;
    }).join(''));
  }
}
document.addEventListener('click', ev => {   // a past check: bring its character back
  const b = ev.target.closest && ev.target.closest('button.past');
  if (b) showNext(b.dataset.side, b.dataset.claim);
});

$('cast').innerHTML = ['supported', 'contradicted', 'missing_context'].map(k => {
  const w = castOf(k);
  return `<span class="who ${k}">${faceHtml(k)}<b>${esc(w.name)}</b> ${esc(w.role)}: “${esc(w.sign.toLowerCase())}”</span>`;
}).join('');

// ------------------------------------------------------------------ transport (same as the reference client)
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
      lastSeq = e.last_seq || 0;
      for (const side of SIDES) {              // put the latest check back on stage, without the pop
        const last = ui[side].history[ui[side].history.length - 1];
        if (last) showNext(side, last, false);
      }
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
reset(); draw(); connect();
