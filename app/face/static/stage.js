'use strict';
// Stage view: proposition on the left, opposition on the right. Each Verdict pops a character (stage-cast.js)
// onto the side that made the claim, holding a sign, with a speech bubble that gives the context and the source.
// Protocol (Snapshot + seq + reconnect) follows the reference client app/tools/ws_debug.html.
// The setup screen ("New debate") takes a motion + brief, sends a ResearchRequest so Evidence researches it before the
// debate, shows progress and the key facts found, then starts the session with the brief. During the debate the brief
// sits under the motion.
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
// Research outlives sessions: a ResearchRequest runs before its SessionStart. Keyed by motion text.
let PREP = {progress: new Map(), facts: new Map()};
let setupOpen = false, requested = null;   // requested: {motion, brief} sent from this tab
const norm = m => String(m || '').trim().replace(/\s+/g, ' ').toLowerCase();
const ui = Object.fromEntries(SIDES.map(s => [s, {queue: [], active: null, history: []}]));

function reset() {
  S = {session: null, sides: {}, names: {}, utts: new Map(), claims: new Map(), verdicts: new Map(), facts: []};
  for (const side of SIDES) {
    const u = ui[side];
    if (u.active) u.active.el.remove();
    u.queue = []; u.active = null; u.history = [];
  }
  Centre.reset();
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
      S.facts = PREP.facts.get(norm(e.motion)) || [];
      if (live) closeSetup();
      break;
    case 'ResearchProgress': PREP.progress.set(norm(e.motion || (S.session && S.session.motion)), e); break;
    case 'FactSheet':
      PREP.facts.set(norm(e.motion), e.facts || []);
      if (S.session && norm(S.session.motion) === norm(e.motion)) S.facts = e.facts || [];
      break;
    case 'Utterance': sideOf(e.speaker); S.utts.set(e.id, e); break;
    case 'Claim': sideOf(e.speaker); S.claims.set(e.id, e); if (S.verdicts.has(e.id)) onVerdict(e.id, live); break;
    case 'Verdict': S.verdicts.set(e.claim_id, e); onVerdict(e.claim_id, live); break;
    case 'Consistency': case 'DebateSummary': case 'Control': Centre.apply(e, live); break;
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

function researchChip() {
  const p = S.session && PREP.progress.get(norm(S.session.motion));
  if (!p) return '';
  if (p.stage === 'ready') return `<span class="chip ok" title="${esc(p.detail)}">researched · ${(S.facts || []).length} key facts</span>`;
  if (p.stage === 'error') return `<span class="chip bad" title="${esc(p.detail)}">research failed: web only</span>`;
  return `<span class="chip pend">researching ${esc(p.stage)} ${p.total ? Math.round(100 * p.done / p.total) : 0}%</span>`;
}
function draw() {
  $('motion').textContent = S.session ? S.session.motion || 'Open debate' : 'Waiting for a motion';
  const brief = S.session && (S.session.brief || '').trim();
  $('brief').hidden = !brief;
  if (brief && $('brief').textContent !== brief) $('brief').textContent = brief;
  setHtml($('research-chip'), researchChip());
  if (setupOpen) drawSetup();
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
    if (!idle) {
      idle = document.createElement('div'); idle.className = 'idle';
      idle.innerHTML = '<p class="waiting"></p>';
      stage.appendChild(idle);
    }
    idle.hidden = !!u.active;
    setHtml(idle.firstChild, checking ? `<span class="dots"></span> checking ${checking} claim${checking > 1 ? 's' : ''}…`
                                     : 'Waiting for a claim to check');

    setHtml(box.querySelector('.history'), u.history.slice(-HISTORY_MAX).reverse().map(id => {
      const v = S.verdicts.get(id), c = S.claims.get(id);
      if (!v || !c) return '';
      return `<button class="past ${esc(v.label)}${u.active && u.active.id === id ? ' on' : ''}" data-side="${side}"`
        + ` data-claim="${esc(id)}" title="${esc(c.text)}">${faceHtml(v.label)}<span>${esc(clip(c.text, 70))}</span></button>`;
    }).join(''));
  }
  Centre.draw();
}
setInterval(() => { if (S) draw(); }, 2000);   // ages "checking…" between events

// ------------------------------------------------------------------ setup screen
let MOTIONS = [];
fetch('/static/motions.json').then(r => r.ok ? r.json() : []).then(m => { MOTIONS = Array.isArray(m) ? m : []; draw(); })
  .catch(() => {});
function openSetup() {
  setupOpen = true; requested = null;
  const s = S.session;
  $('s-motion').value = s ? s.motion || '' : '';
  $('s-brief').value = s ? s.brief || '' : '';
  const names = side => Object.keys(S.sides).filter(k => S.sides[k] === side).map(nameOf)[0];
  $('s-prop').value = names('prop') || 'Proposition';
  $('s-opp').value = names('opp') || 'Opposition';
  $('setup').hidden = false;
  drawSetup();
  $('s-motion').focus();
}
function closeSetup() { setupOpen = false; $('setup').hidden = true; }
function drawSetup() {
  const motion = $('s-motion').value.trim(), p = PREP.progress.get(norm(motion));
  setHtml($('s-picks'), MOTIONS.map(m => `<button type="button" class="pick${norm(m) === norm(motion) ? ' on' : ''}"`
    + ` data-motion="${esc(m)}" title="Pre-researched demo motion">★ ${esc(m.replace(/^This house would /i, '…'))}</button>`).join(''));
  const show = !!(p || (requested && norm(requested.motion) === norm(motion)));
  $('s-research').hidden = !show;
  if (show) {
    const label = !p ? 'Asking the researcher…'
      : p.stage === 'ready' ? `✓ Ready: ${p.detail}`
      : p.stage === 'error' ? `✗ ${p.detail}`
      : p.stage === 'searching' ? `Searching the web: ${p.done}/${p.total} queries`
      : p.stage === 'embedding' ? `Indexing ${p.total} passages`
      : p.stage === 'facts' ? `Pulling out key facts: ${p.done}/${p.total}` : `${p.stage} ${p.done}/${p.total}`;
    $('s-status').textContent = label;
    $('s-status').className = p ? p.stage : '';
    const pct = !p ? 3 : p.stage === 'ready' ? 100 : p.stage === 'error' ? 100
      : p.stage === 'searching' ? 5 + 60 * p.done / (p.total || 1) : p.stage === 'embedding' ? 70
      : p.stage === 'facts' ? 75 + 25 * p.done / (p.total || 1) : 10;
    $('s-bar').style.width = `${pct}%`;
    $('s-bar').className = p ? p.stage : '';
  }
  const busy = p && !['ready', 'error'].includes(p.stage);
  $('s-start').textContent = busy ? 'Start anyway (research continues)' : 'Start debate';
  $('s-go-research').disabled = !motion || busy;
  $('s-go-research').textContent = p && p.stage === 'ready' ? 'Research again' : 'Research this debate';
}
$('open-setup').onclick = openSetup;
$('open-final').onclick = () => Centre.openFinal();
$('s-close').onclick = closeSetup;
$('s-motion').oninput = $('s-brief').oninput = drawSetup;
$('s-go-research').onclick = () => {
  const motion = $('s-motion').value.trim(), brief = $('s-brief').value.trim();
  if (!motion) return $('s-motion').reportValidity();
  requested = {motion, brief};
  PREP.progress.delete(norm(motion));
  send({op: 'publish', src: 'stage', event: {event: 'ResearchRequest', motion, brief}});
  drawSetup();
};
$('setup-form').onsubmit = ev => {
  ev.preventDefault();
  const motion = $('s-motion').value.trim(), brief = $('s-brief').value.trim();
  if (!motion) return;
  send({op: 'session_start', motion, brief, mode: 'live', speakers: {
    A: {name: $('s-prop').value.trim() || 'Proposition', side: 'prop'},
    B: {name: $('s-opp').value.trim() || 'Opposition', side: 'opp'}}});
};
$('brief').onclick = () => $('brief').classList.toggle('open');
document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && setupOpen) closeSetup(); });

document.addEventListener('click', ev => {   // a past check, or a demo motion on the setup screen
  const pick = ev.target.closest && ev.target.closest('button.pick');
  if (pick) { $('s-motion').value = pick.dataset.motion; drawSetup(); return; }
  const b = ev.target.closest && ev.target.closest('button.past');
  if (b) showNext(b.dataset.side, b.dataset.claim);
});

$('cast').innerHTML = ['supported', 'contradicted', 'missing_context', 'self_contradiction', 'concession'].map(k => {
  const w = castOf(k);
  return `<span class="who ${k}">${faceHtml(k)}<b>${esc(w.name)}</b> ${esc(w.role)}: “${esc(w.sign.toLowerCase())}”</span>`;
}).join('');

// ------------------------------------------------------------------ transport (same as the reference client)
function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
  return false;
}
function setConn(s) { $('conn').className = `dot ${s}`; $('conn').title = `websocket ${s}: ${WS_URL}`; }
function connect() {
  setConn('connecting');
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { backoff = 500; setConn('open'); };
  ws.onmessage = m => {
    let e;
    try { e = JSON.parse(m.data); } catch { return; }
    if (e.event === 'Snapshot') {              // first message after every (re)connect: rebuild quietly
      S = null; reset(); PREP = {progress: new Map(), facts: new Map()};
      for (const h of e.events || []) apply(h, false);
      lastSeq = e.last_seq || 0;
      for (const side of SIDES) {              // put the latest check back on stage, without the pop
        const last = ui[side].history[ui[side].history.length - 1];
        if (last) showNext(side, last, false);
      }
      Centre.afterSnapshot(e.flags || {});
      if (!S.session && !setupOpen) openSetup();   // nothing running yet: start on the setup screen
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
