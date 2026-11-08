'use strict';
// The dashboard. Protocol reference: app/tools/ws_debug.html. Plain JS, no build step, no CDN (venue wifi).
const Q = new URLSearchParams(location.search);
const AUDIO = Q.get('audio') === '1', ADMIN = Q.get('admin') === '1';
const WS_URL = Q.get('ws') || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const BANNER_MS = 9000;         // how long a line stays on screen once it has been said
const STALE_CHECK_S = 20;       // a claim still unanswered after this stops pulsing "checking…"
const STALE_SPEECH_MS = 15000;  // a queued line this old is no longer worth saying
const INTERIM_MAX_S = 6;         // an interim that never got its final (dropped stream, stopped replay) fades out
const TTS_STUCK_S = 20;         // a TTSState{speaking:true} this old lost its false (audio tab closed or cut off)
const JUDGING_MAX_S = 120;      // "Judging…" this long after the Final screen click (the judge answers within ~60 s)

if (Q.get('theme') === 'light') document.documentElement.dataset.theme = 'light';   // for a washed-out projector
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : null;
const LABELS = {supported: 'Supported', contradicted: 'Contradicted', missing_context: 'Missing context',
                unverifiable: 'Unverifiable'};
const ICONS = {supported: '✓', contradicted: '✗', missing_context: '~', unverifiable: '?'};
const SIDE_NAMES = {prop: 'Proposition', opp: 'Opposition'};
const BOTS = {for: 'For', against: 'Against'};   // the side bots (BotLine.persona): Prop's second and Opp's
const sideClass = s => s === 'prop' || s === 'opp' ? s : 'noside';
const host = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return ''; } };

// ------------------------------------------------------------------ state + reducer
let S = null, MODULES = {}, ws = null, lastSeq = 0, snapSeq = 0, backoff = 500;
let banner = null, pickerOpen = false, adminHidden = false, audioReady = false;

function reset() {   // SessionStart wipes debate state; flags + module status are process-wide
  S = {session: null, sides: {}, names: {}, utts: new Map(), claims: new Map(), verdicts: new Map(), cards: new Map(),
       map: null, research: null, decisions: [], considered: new Set(), dstatus: null, interjections: [], tts: null, lastVerdict: null,
       bots: {}, result: null, finalAt: null, flags: S ? S.flags : {}, status: S ? S.status : {}};
  banner = null;
}
function sideOf(spk) {   // 1st speaker heard = prop, 2nd = opp, unless SessionStart said otherwise
  if (!(spk in S.sides)) S.sides[spk] = Object.keys(S.sides).length % 2 ? 'opp' : 'prop';
  return S.sides[spk];
}
const nameOf = spk => S.names[spk] || spk;
// The referee is audible now (saying line `id`, if given). A speaking:true whose false never came stops counting.
const voiceOn = id => !!(S.tts && S.tts.speaking && Date.now() / 1000 - S.tts.ts < TTS_STUCK_S
                         && (id === undefined || S.tts.interjection_id === id));

// One reducer for snapshot history (live=false) and live events, so a refresh rebuilds the same state.
function apply(e, live) {
  switch (e.event) {
    case 'SessionStart':
      if (live && AUDIO) cutSpeech();          // a new debate: the old one's line and queue go (as on Mate Engine)
      reset(); S.session = e;
      for (const [id, sp] of Object.entries(e.speakers || {})) {
        if (sp.side) S.sides[id] = sp.side;
        S.names[id] = sp.name || id;
      }
      if (live) pickerOpen = false;
      break;
    case 'Utterance': sideOf(e.speaker); S.utts.set(e.id, e); break;
    case 'Claim': sideOf(e.speaker); S.claims.set(e.id, e); break;
    case 'Verdict': S.verdicts.set(e.claim_id, e); S.lastVerdict = e; break;
    case 'SourceCard': {
      const list = S.cards.get(e.claim_id) || S.cards.set(e.claim_id, []).get(e.claim_id);
      if (!list.some(c => c.url === e.url)) list.push(e);
      break;
    }
    case 'ArgMapSnapshot': S.map = e; break;
    case 'ResearchProgress': S.research = e; break;
    case 'ModuleStatus': S.status[e.module] = e; break;
    case 'DirectorDecision': S.considered.add(e.key || e.claim_id); S.decisions.push(e); S.decisions.splice(0, S.decisions.length - 30); break;
    case 'DirectorStatus': S.dstatus = e; break;
    case 'TTSState':
      if (/^replay(:|$)/.test(e.src || '')) break;   // a recorded tab's voice (./run.sh replay): only this room's counts
      S.tts = e;
      if (!e.speaking && banner) banner.until = Math.min(banner.until, Date.now() + 4000);
      break;
    case 'Control':
      S.flags[e.flag] = e.value;
      if (e.flag === 'screen') S.finalAt = e.value === 'final' ? e.ts : null;   // when the judge was asked
      if (live) onControl(e);
      break;
    case 'DebateResult': S.result = {e, live}; break;                            // live: revealed with an animation
    case 'BotLine': if (Object.hasOwn(BOTS, e.persona)) S.bots[e.persona] = {e, live}; break;   // live: animates in
    case 'Interjection':
      S.interjections.push(e);
      if (live && e.seq > snapSeq) {           // NEVER replay history: a refresh mid-debate stays silent
        banner = {e, until: Date.now() + BANNER_MS};
        speak(e);
      }
      break;
  }
}

// ------------------------------------------------------------------ transport
function connect() {
  setConn('connecting');
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    backoff = 500; setConn('open');
    if (ttsWant && ttsWant !== ttsSent) ttsState(ttsWant.speaking, ttsWant.interjection_id);   // missed while down
  };
  ws.onmessage = m => {
    let e;
    try { e = JSON.parse(m.data); } catch { return; }
    if (e.event === 'Snapshot') {              // first message after every (re)connect
      S = null; reset();
      for (const h of e.events || []) apply(h, false);
      S.flags = {...(e.flags || {})}; MODULES = e.modules || {};
      lastSeq = snapSeq = e.last_seq || 0;
    } else if (e.seq == null) {
      console.warn('server:', e);
    } else if (e.seq > lastSeq) {
      lastSeq = e.seq; apply(e, true);
    } else return;
    render();
  };
  ws.onclose = () => {                         // also after 1013 (we were too slow): reconnect + resync
    setConn('closed');
    setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 5000);
  };
}
function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(msg)); return true; }
  return false;
}
const control = (flag, value) => send({op: 'control', flag, value});
// The bus must hear how each line ends, or every view (and the avatar's mouth) stays "Speaking": a TTSState the socket
// was down for is sent on reconnect, and a line still audible when the tab closes or refreshes is ended by a beacon.
let ttsWant = null, ttsSent = null;            // the TTSState this tab means, and the last one the bus got
function ttsState(speaking, id) {
  ttsWant = {event: 'TTSState', speaking, interjection_id: id ?? null};
  if (send({op: 'publish', src: 'face:audio', event: ttsWant})) ttsSent = ttsWant;
}
window.addEventListener('pagehide', () => {
  if (ttsSent && ttsSent.speaking && navigator.sendBeacon) {
    navigator.sendBeacon('/publish', JSON.stringify({...ttsSent, speaking: false, src: 'face:audio'}));
  }
  ttsWant = ttsSent = null;
});
function setConn(s) { $('conn').className = `dot ${s}`; $('conn').title = `websocket ${s}: ${WS_URL}`; }

// ------------------------------------------------------------------ Moderator voice (?audio=1 only)
// say() plays the server's WAV (/face/tts: ElevenLabs, or espeak-ng) and falls back to browser speech if the server
// has no voice or the line isn't ready in time. A replacement must call started(seconds long, if known) when sound is
// audible, done() once when it ends, and return a function that cuts it off. TTSState{speaking:true} = "audible now".
const START_TIMEOUT_MS = 8000;  // nothing audible by then (no voice anywhere): give the line up
let speechQueue = [], speaking = null;

function pickVoice() {
  const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
  return voices.find(v => /^(Daniel|Google UK English Male)/.test(v.name))
      || voices.find(v => v.lang === 'en-GB') || voices.find(v => /^en/.test(v.lang)) || null;
}
function say(text, done, started) {
  const audio = new Audio(`/face/tts?text=${encodeURIComponent(text)}`);
  let playing = false, cut = false, fallback = null;
  const toSpeech = () => {                     // only before the WAV has made a sound, and only once
    if (playing) done();
    else if (!cut && !fallback) fallback = saySpeech(text, done, started);
  };
  audio.onplaying = () => { if (!playing && !cut) { playing = true; started(audio.duration); } };
  audio.onended = done;
  audio.onerror = toSpeech;
  audio.play().catch(toSpeech);
  return () => { cut = true; audio.pause(); if (fallback) fallback(); };
}
function saySpeech(text, done, started) {
  if (!window.speechSynthesis) { done(); return () => {}; }
  const u = new SpeechSynthesisUtterance(text), voice = pickVoice();
  if (voice) u.voice = voice;
  u.rate = 1.05;
  u.onstart = () => started();
  u.onend = u.onerror = done;
  speechSynthesis.speak(u);
  say.keep = u;                                // Chrome drops events if the utterance is garbage collected
  return () => speechSynthesis.cancel();
}
function speak(e) {
  if (!AUDIO || !audioReady || S.flags.tts === false) return;
  speechQueue.push({e, at: Date.now()});
  nextLine();
}
function nextLine() {
  if (speaking) return;
  let item;
  do { item = speechQueue.shift(); } while (item && Date.now() - item.at > STALE_SPEECH_MS);
  if (!item) return;
  let finished = false, audible = false, stop = () => {};
  const finish = () => {
    if (finished) return;
    finished = true;
    clearTimeout(watchdog);
    speaking = null;
    if (audible) ttsState(false, item.e.id);
    nextLine();
  };
  const cut = () => { stop(); finish(); };     // TTSState false only once the sound has really stopped
  let watchdog = setTimeout(cut, START_TIMEOUT_MS);
  const started = dur => {                     // dur: the WAV's length in s, when known
    if (finished || audible) return;
    audible = true;
    ttsState(true, item.e.id);
    // Browsers sometimes never fire onend, and a stuck TTSState would silence the Director for good. Never before
    // the WAV's own end: spoken numbers ("£1.2bn" -> "1.2 billion pounds") outrun the word-count estimate.
    clearTimeout(watchdog);
    const est = 3000 + item.e.text.split(/\s+/).length * 600;
    watchdog = setTimeout(cut, Number.isFinite(dur) ? Math.max(est, dur * 1000 + 2000) : est);
  };
  stop = say(item.e.text, finish, started);
  if (!finished) speaking = {stop, finish};
}
function cutSpeech() {
  speechQueue = [];
  if (speaking) { const s = speaking; s.stop(); s.finish(); }
}
function onControl(e) {                        // panic: mute / director off / tts off cut the voice at once
  if (AUDIO && ((e.flag === 'mute' && e.value) || (['director', 'tts'].includes(e.flag) && e.value === false))) cutSpeech();
}

// ------------------------------------------------------------------ rendering helpers
let queued = false;
function render() { if (!queued) { queued = true; requestAnimationFrame(draw); } }

// Keyed update: untouched rows stay in place, so only new rows animate and scrolling doesn't jump.
function patch(box, items) {
  const old = new Map([...box.children].map(n => [n.dataset.key, n]));
  let prev = null;
  for (const {key, html} of items) {
    let n = old.get(key);
    if (n) old.delete(key);
    else { n = document.createElement('div'); n.dataset.key = key; n.className = 'enter'; }
    if (n.$html !== html) { n.innerHTML = html; n.$html = html; }
    const at = prev ? prev.nextSibling : box.firstChild;
    if (n !== at) box.insertBefore(n, at);
    prev = n;
  }
  for (const n of old.values()) n.remove();
}
function setHtml(el, html) { if (el.$html !== html) { el.innerHTML = html; el.$html = html; } }

function pct(xs, q) {   // same formula as app/latency.py
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b), k = (s.length - 1) * q, f = Math.floor(k), c = Math.min(f + 1, s.length - 1);
  return s[f] + (s[c] - s[f]) * (k - f);
}
const secs = v => v == null ? '–' : `${v.toFixed(1)} s`;
const verdictDelay = v => v.latency_ms ? v.latency_ms / 1000
                                       : ((v.marks || {}).verdict || v.ts) - ((v.marks || {}).speech_end || Infinity);
const verdictDelays = () => [...S.verdicts.values()].map(verdictDelay).filter(x => x > 0);

const sinceOf = c => (c.marks || {}).speech_end || c.t_end || c.ts || Date.now() / 1000;
function chipHtml(c) {
  const v = S.verdicts.get(c.id);
  if (v) return `<span class="chip ${esc(v.label)}"><i aria-hidden="true">${ICONS[v.label] || '?'}</i>${esc(LABELS[v.label] || v.label)}</span>`;
  if (Date.now() / 1000 - sinceOf(c) > STALE_CHECK_S) return '<span class="chip stale">No verdict</span>';
  return `<span class="chip checking"><i class="pulse" aria-hidden="true">●</i>Checking <span class="clock" data-since="${esc(sinceOf(c))}"></span></span>`;
}
const shown = c => c.checkable || S.verdicts.has(c.id);
const claimsOf = uttId => [...S.claims.values()].filter(c => c.utterance_id === uttId && shown(c));

// Lights up the spoken words of each claim, when the claim's text appears verbatim in the utterance.
function markedText(u, claims) {
  const text = u.text || '', lower = text.toLowerCase(), spans = [];
  for (const c of claims) {
    const needle = (c.text || '').trim().replace(/[.!?]+$/, '').toLowerCase();
    const at = needle ? lower.indexOf(needle) : -1;
    if (at >= 0) spans.push({from: at, to: at + needle.length, c});
  }
  spans.sort((a, b) => a.from - b.from);
  let out = '', pos = 0;
  for (const {from, to, c} of spans) {
    if (from < pos) continue;
    const v = S.verdicts.get(c.id);
    out += `${esc(text.slice(pos, from))}<mark class="${v ? esc(v.label) : 'checking'}">${esc(text.slice(from, to))}</mark>`;
    pos = to;
  }
  return out + esc(text.slice(pos));
}
function sourceHtml(v, cards) {
  const ev = v && v.evidence && v.evidence[0], card = cards[0];
  const url = safeUrl((ev && ev.url) || (card && card.url));
  if (!url) return '';
  const title = (ev && ev.title) || (card && card.title) || host(url);
  const via = v && v.via && v.via !== 'none' ? ` · via ${esc(v.via)}` : '';
  return `<div class="src"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(title)}</a> · ${esc(host(url))}${via}</div>`;
}
function factOf(v) {
  if (!v) return '';
  const n = v.numeric;
  if (n && n.claimed != null && n.actual != null) {
    const unit = n.unit === '%' ? '%' : n.unit ? ` ${esc(n.unit)}` : '';
    return `Said ${esc(n.claimed)}${unit}. Source says ${esc(n.actual)}${unit}.`;
  }
  return esc(v.short_fact || '');
}
function cardHtml(c) {
  const v = S.verdicts.get(c.id), cards = S.cards.get(c.id) || [];
  const ev = v && v.evidence && v.evidence[0];
  const quote = (ev && ev.quote) || (!v && cards[0] && cards[0].snippet) || '';
  const fact = factOf(v), time = v && verdictDelay(v) > 0 ? `${verdictDelay(v).toFixed(1)} s` : '';
  return `<div class="card ${sideClass(S.sides[c.speaker])} ${v ? `landed v-${esc(v.label)}` : ''}"><div class="head">${chipHtml(c)}`
    + `<span class="who">${esc(nameOf(c.speaker))}</span>`
    + (time ? `<span class="meta">checked in ${time}</span>` : '')
    + (v && v.confidence ? `<span class="meta">${Math.round(v.confidence * 100)}% confidence</span>` : '') + '</div>'
    + `<div class="text">${esc(c.text)}</div>`
    + (fact ? `<div class="fact">${fact}</div>` : '')
    + (quote ? `<blockquote>“${esc(quote)}”</blockquote>` : '')
    + sourceHtml(v, cards) + '</div>';
}
function tally(side) {
  const t = {supported: 0, contradicted: 0, missing_context: 0, unverifiable: 0, checking: 0};
  for (const c of S.claims.values()) {
    if (sideOf(c.speaker) !== side || !shown(c)) continue;
    const v = S.verdicts.get(c.id);
    t[v ? v.label : 'checking'] += 1;
  }
  return t;
}
function countsHtml(t) {
  const rows = Object.keys(LABELS).filter(k => t[k]).map(k =>
    `<span class="count"><i class="${k}" aria-hidden="true">${ICONS[k]}</i>${t[k]} ${esc(LABELS[k].toLowerCase())}</span>`);
  if (t.checking) rows.push(`<span class="count"><i class="checking" aria-hidden="true">●</i>${t.checking} checking</span>`);
  return rows.join('') || '<span class="count">Nothing checked yet</span>';
}
function verdictBarHtml(t) {
  const total = Object.keys(LABELS).reduce((n, k) => n + t[k], 0);
  if (!total) return '';
  return `<div class="vbar" role="img" aria-label="${esc(Object.keys(LABELS).map(k => `${t[k]} ${LABELS[k]}`).join(', '))}">`
    + Object.keys(LABELS).filter(k => t[k]).map(k => `<span class="${k}" style="flex-grow:${t[k]}" title="${t[k]} ${esc(LABELS[k])}"></span>`).join('') + '</div>';
}

// ------------------------------------------------------------------ draw
function stagesOf(v) {
  const m = v.marks || {}, out = [];
  const add = (name, a, b) => { if (a && b && b > a) out.push({name, d: b - a}); };
  add('Heard', m.speech_end, m.stt_final);
  add('Spotted', m.stt_final || m.speech_end, m.claim);
  if (m.retrieved) { add('Searched', m.claim, m.retrieved); add('Judged', m.retrieved, m.verdict); }
  else add('Checked', m.claim, m.verdict);
  return out;
}
function stagesHtml(v) {
  const stages = v ? stagesOf(v) : [];
  if (!stages.length) return '<div class="stages empty">Stage timings appear with the first verdict</div>';
  return '<div class="stages"><div class="sbar">'
    + stages.map((s, i) => `<span class="s${i}" style="flex-grow:${s.d.toFixed(3)}" title="${s.name}: ${s.d.toFixed(2)} s"></span>`).join('') + '</div>'
    + `<div class="slabels">${stages.map((s, i) => `<span><i class="s${i}" aria-hidden="true"></i>${s.name} ${s.d.toFixed(1)}</span>`).join('')}</div></div>`;
}
function drawHeader() {
  $('motion').textContent = S.session ? S.session.motion || 'Open debate' : 'Waiting for a motion';
  const m = S.session && S.session.mode;
  $('mode').textContent = S.status.replay && /^replaying/.test(S.status.replay.detail || '') ? '· replay of a recorded debate'
    : m === 'replay' ? '· recorded run' : m === 'drive' ? '· recorded debate' : m === 'mock' ? '· mock data' : '· live';
  if (!S.session) $('mode').textContent = '';
  const r = S.research, active = r && !['ready', 'error'].includes(r.stage);
  $('research').hidden = !active;
  if (active) {
    $('research-text').textContent = `Evidence index: ${r.stage}${r.total ? ` ${r.done} of ${r.total}` : ''}`;
    $('research-bar').style.width = r.total ? `${Math.min(100, 100 * r.done / r.total)}%` : '10%';
  }
  const delays = verdictDelays(), last = S.lastVerdict ? verdictDelay(S.lastVerdict) : null;
  setHtml($('meter'), `<div class="hero"><b>${secs(last > 0 ? last : null)}</b><span>last claim checked after it was said</span></div>`
    + `<div class="meter-side">${stagesHtml(S.lastVerdict)}<div class="median">Median <b>${secs(pct(delays, .5))}</b> over ${delays.length} verdict${delays.length === 1 ? '' : 's'}</div></div>`);
  setHtml($('referee'), refereeHtml());
  $('enable').hidden = !AUDIO || audioReady;   // its own node: the cooldown countdown re-renders #referee every second
}
function refereeHtml() {
  const d = S.dstatus, speaking = voiceOn();
  const why = {nothing_pending: ['listening', 'Listening'], awaiting_pause: ['waiting', 'Waiting for a pause'],
               floor_closed: ['waiting', 'Waiting for a pause'], own_tts: ['speaking', 'Speaking'], muted: ['muted', 'Muted']};
  const [cls, text] = speaking ? ['speaking', 'Speaking']
    : !d ? ['listening', 'Listening']
    : d.muted ? ['muted', 'Muted']
    : d.cooldown_left_s > 0 ? ['cooling', `Can speak again in ${Math.ceil(d.cooldown_left_s)} s`]
    : why[d.blocked] || ['listening', 'Listening'];
  const spoke = S.interjections.length, seen = S.considered.size;
  const record = seen ? `<span class="record">Considered ${seen} point${seen === 1 ? '' : 's'}, spoke ${spoke} time${spoke === 1 ? '' : 's'}</span>` : '';
  return `<span class="state ${cls}">${bars}<b>Referee</b> ${esc(text)}</span>${record}`;
}
const bars = '<span class="bars" aria-hidden="true"><i></i><i></i><i></i></span>';
function drawLive() {
  const now = Date.now() / 1000;
  const utts = [...S.utts.values()].filter(u => u.final || (S.flags.interims !== false && now - (u.ts || now) < INTERIM_MAX_S));
  const box = $('transcript'), pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  let prevSpk = null;
  const rows = utts.slice(-80).map(u => {
    const claims = claimsOf(u.id), chips = claims.map(chipHtml).join('');
    const html = `<div class="utt ${sideClass(S.sides[u.speaker])}${u.final ? '' : ' interim'}${u.speaker !== prevSpk ? ' turn' : ''}">`
      + (u.speaker !== prevSpk ? `<div class="who">${esc(nameOf(u.speaker))}</div>` : '')
      + `<span class="said">${markedText(u, claims)}</span>${chips ? `<div class="chips">${chips}</div>` : ''}</div>`;
    prevSpk = u.speaker;
    return {key: u.id, html};
  });
  patch(box, rows.length ? rows : [{key: 'empty', html: `<p class="empty">${S.session ? 'Waiting for speech…' : 'The debate appears here.'}</p>`}]);
  if (pinned) box.scrollTop = box.scrollHeight;

  const claims = [...S.claims.values()].filter(shown).slice(-30).reverse();
  patch($('feed'), claims.length ? claims.map(c => ({key: c.id, html: cardHtml(c)}))
                                 : [{key: 'empty', html: '<p class="empty">Checkable claims appear here as they are spoken.</p>'}]);
  setHtml($('tally'), ['prop', 'opp'].map(side => `<div class="${side}"><b class="who">${SIDE_NAMES[side]}</b>${countsHtml(tally(side))}</div>`).join(''));
}
function sideHtml(side) {
  const m = S.map, nodes = new Map(((m && m.nodes) || []).map(n => [n.claim_id, n]));
  const textOf = id => { const c = S.claims.get(id); return c ? c.text : id; };
  const speakers = Object.keys(S.sides).filter(s => S.sides[s] === side).map(nameOf).join(', ');
  const claims = [...S.claims.values()].filter(c => S.sides[c.speaker] === side && shown(c));
  const t = tally(side), score = m && m.score && m.score[side];
  const rows = claims.map(c => {
    const v = S.verdicts.get(c.id), n = nodes.get(c.id), fact = factOf(v);
    const links = ((m && m.edges) || []).filter(e => e.src === c.id)
      .map(e => `<div class="link">${e.relation === 'attacks' ? 'Challenges' : 'Backs up'}: ${esc(textOf(e.dst).slice(0, 90))}</div>`).join('');
    return `<div class="card ${side} ${v ? `v-${esc(v.label)}` : ''}"><div class="head">${chipHtml(c)}`
      + (n && ['unanswered', 'conceded', 'answered'].includes(n.status) ? `<span class="status ${esc(n.status)}">${esc(n.status)}</span>` : '') + '</div>'
      + `<div class="text">${esc(c.text)}</div>`
      + (fact ? `<div class="fact">${fact}</div>` : '')
      + sourceHtml(v, S.cards.get(c.id) || []) + links + '</div>';
  }).join('');
  const unanswered = score && score.unanswered ? `<span class="count">${esc(score.unanswered)} left unanswered</span>` : '';
  return `<h3 class="who">${SIDE_NAMES[side]}${speakers ? ` <span>${esc(speakers)}</span>` : ''}</h3>`
    + verdictBarHtml(t) + `<div class="counts">${countsHtml(t)}${unanswered}</div>`
    + `<div class="scroll">${rows || '<p class="empty">No checkable claims.</p>'}</div>`;
}
function drawFinal() {
  setHtml($('call'), esc((S.map && S.map.call) || 'The evidence so far'));
  const checked = [...S.claims.keys()].filter(id => S.verdicts.has(id)).length;
  const delays = verdictDelays(), spoke = S.interjections, seen = S.considered.size;
  setHtml($('effect'), [[String(checked), `claim${checked === 1 ? '' : 's'} checked`],
    [secs(pct(delays, .5)), 'median time to check'],
    [String(spoke.length), `time${spoke.length === 1 ? '' : 's'} the referee stepped in${seen ? `, of ${seen} considered` : ''}`]]
    .map(([n, l]) => `<div class="stat"><b>${esc(n)}</b><span>${esc(l)}</span></div>`).join(''));
  setHtml($('final-prop'), sideHtml('prop'));
  setHtml($('final-opp'), sideHtml('opp'));
  setHtml($('final-referee'), '<h3>What the referee said</h3><div class="scroll">' + (spoke.map(e => {
    const to = e.target ? `To ${esc(nameOf(e.target))}` : 'To the room';
    return `<div class="line"><span class="meta">${to}</span>${esc(e.text)}</div>`;
  }).join('') || '<p class="empty">The referee stayed silent.</p>') + '</div>');
}
// Each side bot's latest line, under its side's colour. The strip stays hidden until a bot has spoken (bots off: no
// change). Model text, so textContent only. "AI knowledge" = an unsourced challenge or concede; a sourced rebut names
// its source.
function drawBots() {
  $('bots').hidden = !Object.keys(S.bots).length || S.flags.screen === 'final';
  for (const [persona, label] of Object.entries(BOTS)) {
    const box = $(`bot-${persona}`), b = S.bots[persona], key = b ? b.e.id : '';
    if (box.dataset.key === key) continue;    // unchanged: no rebuild, no animation
    box.dataset.key = key;
    const n = document.createElement('div');
    const part = (tag, cls, text) => { const x = n.appendChild(document.createElement(tag)); x.className = cls; x.textContent = text; };
    n.className = b && b.live ? 'line enter' : 'line';
    part('b', 'who', label);
    if (!b) part('span', 'text empty', 'Nothing yet');
    else {
      const e = b.e, ev = e.sourced && e.kind === 'rebut' && (e.evidence || [])[0];
      if (!e.sourced && (e.kind === 'challenge' || e.kind === 'concede')) part('span', 'tag', 'AI knowledge');
      part('span', 'text', e.text);
      if (ev && (ev.title || host(ev.url))) part('span', 'src', `Source: ${ev.title || host(ev.url)}`);
    }
    box.replaceChildren(n);
  }
}
// The judge's verdict (DebateResult) on the final screen: winner, margin, headline, overall and per-criterion scores,
// each bot's dance. Before it arrives, "Judging…" (only when the judge module runs, for JUDGING_MAX_S after the
// Final screen click at most, and not once the judge's status since the click says no verdict is coming: nothing to
// judge, judging failed, or the module restarted). A click that re-judges (more speech since) replaces the earlier
// verdict with "Judging…" as soon as the judge says it is at it. Model text, so textContent only. Rebuilt only when
// the result changes.
const MARGINS = {narrow: 'Narrow margin', clear: 'Clear margin', decisive: 'Decisive margin'};
const scoreText = v => typeof v === 'number' && Number.isFinite(v) ? (Number.isInteger(v) ? String(v) : v.toFixed(1))
                 : typeof v === 'string' && v ? v : '–';
const criterionName = k => { const t = String(k).replace(/_/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); };
// The judge's ModuleStatus while it works on a click (app/judge/run.py: settling, the summary wait, judging).
const JUDGE_BUSY = /^(waiting up to |judging: )/;
function drawVerdict() {
  const box = $('verdict'), clicked = S.finalAt != null;
  const judgeOn = (MODULES.judge && MODULES.judge !== 'off') || !!S.status.judge;
  const said = clicked && S.status.judge && S.status.judge.ts >= S.finalAt ? S.status.judge : null;  // since the click
  const busy = !!said && said.state === 'up' && JUDGE_BUSY.test(said.detail || '');
  const r = S.result && busy && S.result.e.ts < S.finalAt ? null : S.result;   // an earlier click's, being redone
  const judging = !r && judgeOn && clicked && Date.now() / 1000 - S.finalAt < JUDGING_MAX_S && (!said || busy);
  box.hidden = S.flags.screen !== 'final' || (!r && !judging);
  const key = r ? `result ${r.e.seq}` : 'judging';
  if (box.hidden || box.dataset.key === key) return;
  box.dataset.key = key;
  const part = (parent, tag, cls, text) => {
    const x = parent.appendChild(document.createElement(tag));
    x.className = cls;
    if (text != null) x.textContent = text;
    return x;
  };
  box.replaceChildren();
  if (!r) {
    box.className = 'verdict judging';
    part(part(box, 'div', 'head'), 'b', 'winner pulse', 'Judging…');
    part(box, 'p', 'meta', 'Weighing the scorecard and the transcript');
    return;
  }
  const e = r.e, winner = ['prop', 'opp'].includes(e.winner) ? e.winner : 'draw', scores = e.scores || {};
  box.className = `verdict ${winner === 'draw' ? 'draw' : winner}${r.live ? ' reveal' : ''}`;
  const head = part(box, 'div', 'head');
  part(head, 'b', 'winner', winner === 'draw' ? 'Draw' : `${SIDE_NAMES[winner]} wins`);
  const names = winner === 'draw' ? '' : Object.keys(S.sides).filter(s => S.sides[s] === winner).map(nameOf).join(', ');
  if (names) part(head, 'span', 'names', names);
  const margin = winner === 'draw' ? 'Too close to call' : MARGINS[e.margin] || (e.margin ? `${e.margin} margin` : '');
  if (margin) part(head, 'span', 'margin', margin);
  const overall = part(head, 'span', 'overall');
  for (const side of ['prop', 'opp']) {
    const o = part(overall, 'span', side);
    part(o, 'b', '', scoreText((scores[side] || {}).overall));
    part(o, 'span', '', SIDE_NAMES[side]);
  }
  if (e.headline) part(box, 'p', 'headline', e.headline);
  const crits = [...new Set([...Object.keys(scores.prop || {}), ...Object.keys(scores.opp || {})])].filter(k => k !== 'overall');
  if (crits.length) {
    const row = part(box, 'div', 'criteria');
    for (const k of crits) {
      const c = part(row, 'span', 'criterion');
      part(c, 'span', 'name', criterionName(k));
      part(c, 'b', 'prop', scoreText((scores.prop || {})[k]));
      part(c, 'b', 'opp', scoreText((scores.opp || {})[k]));
    }
  }
  const dances = Object.entries(BOTS).filter(([p]) => (e.dances || {})[p]);
  if (dances.length) {
    const row = part(box, 'div', 'dances');
    for (const [p, label] of dances) {
      const d = part(row, 'span', `dance ${p === 'for' ? 'prop' : 'opp'}`, `${label} bot dances: ${e.dances[p]}`);
      if ((e.music || {})[p]) { const m = part(d, 'span', 'music', ' ♪'); m.title = 'with its music'; }
    }
  }
  const meta = [e.report ? `Full summary: ${e.report}` : '', e.via === 'formula' ? 'Decided by the scorecard formula' : '']
    .filter(Boolean).join(' · ');
  if (meta) part(box, 'p', 'meta', meta);
}
function drawBanner() {
  const on = banner && (Date.now() < banner.until || voiceOn(banner.e.id));
  $('banner').hidden = !on;
  document.body.classList.toggle('referee-on', !!on);
  if (!on) return;
  const e = banner.e, ev = e.evidence && e.evidence[0], url = ev && safeUrl(ev.url);
  const to = e.target ? ` to ${esc(nameOf(e.target))}` : '', claim = e.claim_id && S.claims.get(e.claim_id);
  const talking = voiceOn(e.id);
  setHtml($('banner'), `<span class="label ${talking ? 'speaking' : ''}">${bars}Referee${to}</span><span class="line">${esc(e.text)}</span>`
    + (claim ? `<span class="src">On: ${esc(claim.text)}</span>` : '')
    + (url ? `<span class="src">Source: ${esc(ev.title || host(url))} · ${esc(host(url))}</span>` : ''));
}
function drawFooter() {
  const names = [...new Set([...Object.keys(MODULES), ...Object.keys(S.status)])].filter(n => MODULES[n] !== 'off' || S.status[n]);
  setHtml($('modules'), names.map(n => {
    const st = S.status[n];
    return `<span class="dot ${esc(st ? st.state : '')}" title="${esc(st ? `${st.state}: ${st.detail}` : MODULES[n] || '')}"></span>${esc(n)}`;
  }).join(''));
  // The silent-demo trap: the Director spoke and no tab said it.
  const last = S.interjections[S.interjections.length - 1], now = Date.now() / 1000;
  const voiceless = ADMIN && last && S.flags.tts !== false && !(S.dstatus && S.dstatus.muted)
    && now - last.ts > 3 && (!S.tts || S.tts.ts < last.ts);
  setHtml($('audio'), voiceless ? '<span class="warn">No tab spoke the last line. Open a tab with ?audio=1 and enable audio.</span>' : '');
}
function drawAdmin() {
  $('admin').hidden = !ADMIN || adminHidden;
  if (!ADMIN || adminHidden) return;
  const muted = S.dstatus ? S.dstatus.muted : !!S.flags.mute, aggr = S.dstatus ? S.dstatus.aggr : 3;
  const isFinal = S.flags.screen === 'final', web = S.flags.web_fallback !== false, bots = S.flags.bots !== false;
  const hasBots = (MODULES.bots && MODULES.bots !== 'off') || Object.keys(S.bots).length > 0;
  const decs = S.decisions.slice(-6).reverse().map(x => `<div class="dec">${esc(x.action)} ${esc(x.kind)} · ${esc(x.reason)}`
    + `${x.text ? ` · “${esc(x.text)}”` : ''}${['candidate', 'held'].includes(x.action) ? ` <button data-ctl="veto" data-val="${esc(JSON.stringify(x.key))}">veto</button>` : ''}</div>`).join('');
  // Separate nodes: a new decision must not rebuild the buttons under the operator's pointer (the click is lost).
  setHtml($('admin-controls'), `<button data-ctl="mute" data-val="${!muted}" class="${muted ? 'on' : ''}">${muted ? 'Unmute' : 'Mute'}</button>`
    + '<button data-ctl="force_next" data-val="true">Say next now</button>'
    + `<span class="gap">Talkative</span>${[1, 2, 3, 4, 5].map(n => `<button data-ctl="aggr" data-val="${n}" class="${n === aggr ? 'on' : ''}">${n}</button>`).join('')}`
    + `<button data-ctl="web_fallback" data-val="${!web}">Web search ${web ? 'on' : 'off'}</button>`
    + (hasBots ? `<button data-ctl="bots" data-val="${!bots}">Side bots ${bots ? 'on' : 'off'}</button>` : '')
    + `<span class="gap"></span><button data-ctl="screen" data-val='"${isFinal ? 'live' : 'final'}"'>${isFinal ? 'Back to live' : 'Final screen'}</button>`
    + '<button data-act="picker">New debate</button><button data-act="replay">Replay recorded debate</button><button data-act="stop">Stop replay</button>'
    + '<button data-act="hide">Hide (H)</button>');
  $('admin-why').hidden = !decs;
  setHtml($('decisions'), decs);
}
function draw() {
  queued = false;
  const isFinal = S.flags.screen === 'final';
  $('live').hidden = isFinal;
  $('final').hidden = !isFinal;
  drawHeader();
  if (isFinal) drawFinal(); else drawLive();
  drawVerdict();
  drawBots();
  drawBanner();
  drawFooter();
  drawAdmin();
  $('picker').hidden = !(ADMIN && (pickerOpen || !S.session));
}

// ------------------------------------------------------------------ operator
async function replay(action) {
  try {
    const r = await fetch(`/face/replay${action === 'stop' ? '/stop' : ''}`, {method: 'POST'});
    if (!r.ok) throw new Error(await r.text());
    if (action !== 'stop') { pickerOpen = false; render(); }
  } catch (err) { console.error(err); alert(`Replay failed: ${err.message}`); }
}
document.addEventListener('click', ev => {
  const b = ev.target.closest && ev.target.closest('button');
  if (!b) return;
  if (b.id === 'enable') {                     // browsers block audio and speech until the page has had a click
    if (window.speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(' '));
    audioReady = true;
  } else if (b.dataset.ctl) control(b.dataset.ctl, JSON.parse(b.dataset.val));
  else if (b.dataset.act === 'picker') pickerOpen = true;
  else if (b.dataset.act === 'replay') replay('start');
  else if (b.dataset.act === 'stop') replay('stop');
  else if (b.dataset.act === 'hide') adminHidden = true;
  else if (b.dataset.motion != null) {
    $('motion-input').value = b.dataset.motion;
    document.querySelectorAll('#motions button').forEach(x => x.classList.toggle('chosen', x === b));
  } else if (b.id === 'picker-replay') replay('start');
  else if (b.id === 'picker-close') pickerOpen = false;
  render();
});
if (ADMIN) {
  fetch('/static/motions.json').then(r => r.json()).then(list => {
    $('motions').innerHTML = list.map(m => `<button type="button" data-motion="${esc(m)}">${esc(m)}</button>`).join('');
  }).catch(err => console.warn('motions.json:', err));
  $('session').onsubmit = ev => {
    ev.preventDefault();
    const motion = $('motion-input').value.trim();
    if (!motion) { $('motion-input').focus(); return; }
    control('screen', 'live');
    send({op: 'session_start', motion, mode: 'live', speakers: {
      A: {name: $('name-a').value.trim() || 'A', side: 'prop'},
      B: {name: $('name-b').value.trim() || 'B', side: 'opp'}}});
    pickerOpen = false;
  };
  document.addEventListener('keydown', ev => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.target.closest && ev.target.closest('input,select,textarea') && ev.key !== 'Escape') return;
    const k = ev.key.toLowerCase(), muted = S.dstatus ? S.dstatus.muted : !!S.flags.mute;
    if (ev.key === 'Escape') control('mute', true);
    else if (k === 'm') control('mute', !muted);
    else if (k === 'f') control('force_next', true);
    else if (/^[1-5]$/.test(k)) control('aggr', +k);
    else if (k === 'w') control('web_fallback', S.flags.web_fallback === false);
    else if (k === 'i') control('interims', S.flags.interims === false);
    else if (k === 'b') control('bots', S.flags.bots === false);
    else if (k === 'h') { adminHidden = !adminHidden; render(); }
    else return;
    ev.preventDefault();
  });
}
if (window.speechSynthesis) speechSynthesis.getVoices();   // starts the browser loading its voice list
setInterval(render, 1000);                                 // banners and "checking…" chips expire on the clock
setInterval(() => {                                        // the running clock on chips: no re-render, no restart of animations
  for (const el of document.querySelectorAll('[data-since]')) {
    el.textContent = `${Math.max(0, Date.now() / 1000 - +el.dataset.since).toFixed(1)} s`;
  }
}, 100);
reset(); render(); connect();
