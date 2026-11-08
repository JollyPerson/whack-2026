'use strict';
// The stage's centre lane and final screen. Loaded before stage.js, which calls Centre.reset/apply/draw/afterSnapshot.
// Centre lane: Consistency findings from the brain. A self-contradiction brings on Flip the chameleon (leaning towards
// the side that U-turned), a concession brings Dove flying in from the side that conceded; agreements and corrections
// land as chips in "Common ground". One character at a time, queued like the side stages; the Snapshot never re-pops.
// Final screen (Control screen=final, from here or the dashboard): legal scales tilted by each side's evidence score,
// a weight per claim that checked out, a cracked weight per U-turn, then the summary's agreed and disputed points.
const Centre = (() => {
  const DWELL_MS = 11000, CHIPS_MAX = 8;
  const C = {queue: [], active: null, findings: new Map(), order: [], summary: null, screen: 'live',
             summaryAsked: false, drawnSummary: null};

  const sideName = s => s === 'prop' ? 'Proposition' : s === 'opp' ? 'Opposition' : 'Speaker';
  const other = s => s === 'prop' ? 'opp' : 'prop';
  const whoOf = k => (k.speaker && S.names[k.speaker]) || sideName(k.side);
  const earlierWho = k => {
    const c = S.claims.get(k.earlier_claim_id);
    return (c && S.names[c.speaker]) || sideName(k.earlier_side || other(k.side));
  };
  const textOf = (id, fallback) => (S.claims.get(id) || {}).text || fallback || '';

  function reset() {
    if (C.active) C.active.el.remove();
    Object.assign(C, {queue: [], active: null, findings: new Map(), order: [], summary: null, summaryAsked: false,
                      drawnSummary: null});
  }

  function apply(e, live) {
    if (e.event === 'Consistency') {
      const fresh = !C.findings.has(e.id);
      C.findings.set(e.id, e);
      if (fresh) C.order.push(e.id);
      if (live && fresh && (e.kind === 'self_contradiction' || e.kind === 'concession')) { C.queue.push(e.id); tick(); }
    } else if (e.event === 'DebateSummary') {
      C.summary = e;
    } else if (e.event === 'Control' && e.flag === 'screen') {
      C.screen = e.value === 'final' ? 'final' : 'live';
    }
  }
  function afterSnapshot(flags) {
    C.screen = flags && flags.screen === 'final' ? 'final' : 'live';
    const last = C.order.filter(id => ['self_contradiction', 'concession'].includes(C.findings.get(id).kind)).pop();
    if (last) show(last, false);               // the latest one back on stage, without the pop
  }

  // ------------------------------------------------------------------ the centre stage
  function actorHtml(k) {
    const who = castOf(k.kind), from = k.side || 'prop';
    const then = clip(k.then || textOf(k.earlier_claim_id), 150), now = clip(k.now || textOf(k.claim_id), 150);
    const head = k.kind === 'concession'
      ? `<b>${esc(whoOf(k))}</b> concedes a point to <b>${esc(earlierWho(k))}</b>`
      : `<b>${esc(whoOf(k))}</b> (${esc(sideName(k.side))}) changes their story`;
    const rows = k.kind === 'concession'
      ? `<div class="then ${esc(k.earlier_side || other(from))}"><small>${esc(earlierWho(k))} said</small>“${esc(then)}”</div>
         <div class="now ${esc(from)}"><small>${esc(whoOf(k))} accepts</small>“${esc(now)}”</div>`
      : `<div class="then ${esc(from)}"><small>Then</small>“${esc(then)}”</div>
         <div class="now ${esc(from)}"><small>Now</small>“${esc(now)}”</div>`;
    return `<div class="cactor ${esc(k.kind)} from-${esc(from)}" data-id="${esc(k.id)}">
      <div class="figure">
        <div class="sign"><span class="icon">${who.icon}</span>${esc(who.sign)}</div>
        <div class="art-wrap">${who.svg}</div>
        <div class="nametag">${esc(who.name)} <small>${esc(who.role)}</small></div>
      </div>
      <div class="cbubble">
        <p class="opener">${esc(openerOf(k.kind, k.id))}</p>
        <p class="head">${head}</p>
        <div class="thennow">${rows}</div>
        ${k.explanation ? `<p class="context">${esc(clip(k.explanation, 180))}</p>` : ''}
        <div class="meta">${Math.round((k.confidence || 0) * 100)}% sure${k.topic ? ` · ${esc(k.topic)}` : ''}</div>
        <div class="timer"></div>
      </div>
    </div>`;
  }
  function show(id, animate = true) {
    const k = C.findings.get(id), stage = document.querySelector('#centre .cstage');
    if (!k || !stage) return;
    if (C.active) leave(C.active);
    const box = document.createElement('div');
    box.innerHTML = actorHtml(k);
    const el = box.firstElementChild;
    if (!animate) el.classList.add('still');
    const dwell = C.queue.length ? DWELL_MS * .7 : DWELL_MS;
    el.style.setProperty('--dwell', `${dwell}ms`);
    stage.appendChild(el);
    C.active = {id, el, until: Date.now() + dwell};
    draw();
  }
  function leave(active) {
    active.el.classList.add('leaving');
    setTimeout(() => active.el.remove(), 450);
    if (C.active === active) C.active = null;
  }
  function tick() {
    if (C.active && Date.now() >= C.active.until) { leave(C.active); draw(); }
    if (!C.active && C.queue.length) show(C.queue.shift());
  }
  setInterval(tick, 250);

  // ------------------------------------------------------------------ chips + the rest of the lane
  function chipHtml(k) {
    const icon = {self_contradiction: '↺', concession: '🕊', agreement: '🤝', correction: '✎'}[k.kind] || '•';
    const label = k.kind === 'agreement' ? clip(k.then || textOf(k.earlier_claim_id), 60)
      : k.kind === 'correction' ? `${whoOf(k)} updated a figure after the referee`
      : k.kind === 'concession' ? `${whoOf(k)} conceded: ${clip(k.then || textOf(k.earlier_claim_id), 40)}`
      : `${whoOf(k)} U-turn: ${clip(k.topic || k.now, 40)}`;
    const on = C.active && C.active.id === k.id ? ' on' : '';
    return `<button class="kchip ${esc(k.kind)} side-${esc(k.side)}${on}" data-kid="${esc(k.id)}" title="${esc(k.explanation)}">`
      + `<span class="i">${icon}</span><span>${esc(label)}</span></button>`;
  }
  function draw() {
    const lane = document.getElementById('centre');
    if (!lane) return;
    const all = C.order.map(id => C.findings.get(id));
    const idle = lane.querySelector('.cidle');
    if (idle) idle.hidden = !!C.active;
    const counts = {prop: 0, opp: 0};
    for (const k of all) if (k.kind === 'self_contradiction') counts[k.side] = (counts[k.side] || 0) + 1;
    setHtml(lane.querySelector('.uturns'), `<span class="prop">↺ ${counts.prop}</span><span class="label">U-turns</span>`
      + `<span class="opp">${counts.opp} ↺</span>`);
    const common = all.filter(k => k.kind === 'agreement' || k.kind === 'concession' || k.kind === 'correction');
    setHtml(lane.querySelector('.common .chips'), common.slice(-CHIPS_MAX).reverse().map(chipHtml).join('')
      || '<p class="none">Nothing agreed yet</p>');
    setHtml(lane.querySelector('.recent'), all.filter(k => k.kind === 'self_contradiction').slice(-4).reverse()
      .map(chipHtml).join(''));
    drawFinal();
  }

  // ------------------------------------------------------------------ the final screen: the scales
  function tally(side) {
    const t = {supported: 0, missing_context: 0, contradicted: 0};
    for (const c of S.claims.values()) {
      if ((c.side || S.sides[c.speaker]) !== side) continue;
      const v = S.verdicts.get(c.id);
      if (v && v.label in t && (v.confidence || 0) >= 0.6) t[v.label]++;
    }
    return t;
  }
  function scoreOf(side, t) {
    const s = C.summary && C.summary.score && C.summary.score[side];
    if (typeof s === 'number') return s;
    return (t.supported + .5 * t.missing_context + 1) / (t.supported + t.contradicted + t.missing_context + 2);
  }
  function weightsSvg(side, t, cracked, x) {
    const items = [...Array(Math.min(t.supported, 6)).fill('ok'), ...Array(Math.min(t.missing_context, 3)).fill('mid'),
                   ...Array(Math.min(cracked, 3)).fill('crack')];
    return items.map((kind, i) => {
      const col = i % 4, row = Math.floor(i / 4), wx = x - 50 + col * 26, wy = -22 - row * 20;
      const crack = kind === 'crack' ? `<path d="M${wx + 8} ${wy} l5 7 l-4 4 l6 7" class="crackline"/>` : '';
      return `<g class="weight ${kind}" style="animation-delay:${.25 + i * .14}s">
        <rect x="${wx}" y="${wy}" width="22" height="18" rx="3"/>${crack}
        <text x="${wx + 11}" y="${wy + 13}">${kind === 'ok' ? '✓' : kind === 'mid' ? '~' : '↺'}</text></g>`;
    }).join('');
  }
  function scalesSvg(tilt, sides) {
    const L = 190, px = 300, py = 78, a = tilt * Math.PI / 180;
    const dyL = -L * Math.sin(a), dyR = L * Math.sin(a);
    const pan = (side, x, dy) => `<g class="pan ${side}" style="--dy:${dy.toFixed(1)}px">
        <line x1="${x}" y1="${py}" x2="${x - 58}" y2="${py + 120}" class="chain"/>
        <line x1="${x}" y1="${py}" x2="${x + 58}" y2="${py + 120}" class="chain"/>
        <g transform="translate(0 ${py + 120})">
          <path d="M${x - 72} 0 Q${x} 46 ${x + 72} 0 Z" class="dish"/>
          ${weightsSvg(side, sides[side].t, sides[side].cracked, x)}
        </g>
        <text x="${x}" y="${py + 192}" class="pan-label">${esc(sides[side].label)}</text>
        <text x="${x}" y="${py + 214}" class="pan-score">${Math.round(sides[side].score * 100)}% on the evidence</text>
      </g>`;
    return `<svg viewBox="0 0 600 330" class="scales" role="img" aria-label="Legal scales">
      <path d="M220 318 Q300 290 380 318 Z" class="base"/>
      <rect x="292" y="${py}" width="16" height="232" rx="4" class="pillar"/>
      <g class="beam" style="--tilt:${tilt.toFixed(2)}deg">
        <rect x="${px - L - 6}" y="${py - 6}" width="${2 * L + 12}" height="12" rx="6" class="bar"/>
        <circle cx="${px - L}" cy="${py}" r="7" class="end"/><circle cx="${px + L}" cy="${py}" r="7" class="end"/>
      </g>
      ${pan('prop', px - L, dyL)}${pan('opp', px + L, dyR)}
      <circle cx="${px}" cy="${py}" r="13" class="pivot"/>
      <path d="M${px - 9} ${py - 22} L${px} ${py - 38} L${px + 9} ${py - 22} Z" class="finial"/>
    </svg>`;
  }
  function finalListsHtml(s) {
    const agreed = (s.agreed || []).map(a => `<li>🤝 ${esc(a.statement)}</li>`).join('')
      || '<li class="none">No common ground found.</li>';
    const disputed = (s.disputed || []).map(d => {
      const lean = d.evidence_leans === 'prop' ? '← evidence leans Proposition'
        : d.evidence_leans === 'opp' ? 'evidence leans Opposition →' : 'evidence is even';
      return `<li><b>${esc(d.issue)}</b>
        <div class="pos"><span class="prop">${esc(clip(d.prop, 110) || '—')}</span><span class="opp">${esc(clip(d.opp, 110) || '—')}</span></div>
        <span class="lean ${esc(d.evidence_leans)}">${lean}</span></li>`;
    }).join('') || '<li class="none">No open disputes.</li>';
    return `<div class="col agreed"><h3>Agreed</h3><ul>${agreed}</ul></div>
            <div class="col disputed"><h3>Still disputed</h3><ul>${disputed}</ul></div>`;
  }
  function drawFinal() {
    const box = document.getElementById('final');
    if (!box) return;
    const on = C.screen === 'final';
    box.hidden = !on;
    document.body.classList.toggle('on-final', on);
    if (!on) { C.drawnSummary = null; return; }
    const s = C.summary;
    const key = s ? `${s.seq || ''}:${s.version}` : 'waiting';
    if (C.drawnSummary === key) return;        // draw (and animate) once per summary
    C.drawnSummary = key;
    const sides = {};
    for (const side of ['prop', 'opp']) {
      const t = tally(side), names = Object.keys(S.sides).filter(k => S.sides[k] === side).map(nameOf);
      const cracked = s && s.self_contradictions ? s.self_contradictions[side] || 0
        : [...C.findings.values()].filter(k => k.kind === 'self_contradiction' && k.side === side).length;
      sides[side] = {t, cracked, score: scoreOf(side, t), label: `${sideName(side)}${names.length ? ` · ${names.join(' & ')}` : ''}`};
    }
    const tilt = s ? Math.max(-12, Math.min(12, (sides.opp.score - sides.prop.score) * 45)) : 0;
    const counts = side => {
      const x = sides[side], conceded = s && s.concessions ? s.concessions[side] || 0 : 0;
      return `<span class="${side}">✓ ${x.t.supported} · ~ ${x.t.missing_context} · ✗ ${x.t.contradicted} · ↺ ${x.cracked}`
        + ` U-turn${x.cracked === 1 ? '' : 's'} · 🕊 ${conceded} conceded</span>`;
    };
    box.innerHTML = `<div class="final-head"><h2>Weighing up the debate</h2>
        <button type="button" id="back-live" class="link">Back to live</button></div>
      <div class="scales-wrap${s ? '' : ' weighing'}">${scalesSvg(tilt, sides)}</div>
      <div class="final-counts">${counts('prop')}${counts('opp')}</div>
      ${s ? `<p class="call">${esc(s.call || '')}</p><div class="final-cols">${finalListsHtml(s)}</div>`
          : '<p class="call weighing-text">Weighing it up…</p>'}`;
    const beam = box.querySelector('.beam');
    const pans = box.querySelectorAll('.pan');
    requestAnimationFrame(() => requestAnimationFrame(() => {   // start level, then tip: the swing is the reveal
      beam && beam.classList.add('tipped');
      pans.forEach(p => p.classList.add('tipped'));
    }));
    document.getElementById('back-live').onclick = () => send({op: 'control', flag: 'screen', value: 'live'});
  }
  function openFinal() { send({op: 'control', flag: 'screen', value: 'final'}); }

  document.addEventListener('click', ev => {
    const b = ev.target.closest && ev.target.closest('button.kchip');
    if (b && ['self_contradiction', 'concession'].includes((C.findings.get(b.dataset.kid) || {}).kind)) show(b.dataset.kid);
  });
  return {reset, apply, afterSnapshot, draw, openFinal};
})();
