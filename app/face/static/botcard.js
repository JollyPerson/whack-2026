'use strict';
// One side-bot line (BotLine), drawn the same way on the dashboard's For / Against strip (app.js) and on the stage's
// halves (stage-bots.js): the bot's name, the "AI knowledge" tag for an unsourced challenge or concede, the line, and
// for a sourced line a source card (title, domain, the quote), like the stage characters' bubbles. Loaded before
// app.js and stage-bots.js. Model text, so textContent only: never innerHTML.
const BotCard = (() => {
  const NAMES = {for: 'For bot', against: 'Against bot'};   // BotLine.persona: Prop's second, Opp's second
  const SIDE = {for: 'prop', against: 'opp'};                // the team each bot seconds: its colour and its half
  const TAG = 'AI knowledge';
  const host = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return ''; } };
  const clip = (s, n) => { s = String(s || '').replace(/\s+/g, ' ').trim();
    return s.length <= n ? s : s.slice(0, s.lastIndexOf(' ', n - 1) > n * .6 ? s.lastIndexOf(' ', n - 1) : n - 1) + '…'; };

  const tagOf = e => !e.sourced && (e.kind === 'challenge' || e.kind === 'concede') ? TAG : '';
  // The source a sourced line cites: its first evidence item with a title, a domain or a quote; null otherwise.
  function sourceOf(e) {
    if (!e.sourced || !Array.isArray(e.evidence)) return null;
    for (const ev of e.evidence) {
      const s = {title: clip(ev && ev.title, 90), domain: host(ev && ev.url), quote: clip(ev && ev.quote, 160)};
      if (s.title || s.domain || s.quote) return s;
    }
    return null;
  }
  // Appends the line's parts to `box`: b.who, span.tag (AI knowledge), span.text, div.srccard (sourced only).
  function fill(box, e, persona) {
    const part = (parent, tag, cls, text) => {
      const x = parent.appendChild(document.createElement(tag));
      x.className = cls; x.textContent = text;
      return x;
    };
    part(box, 'b', 'who', NAMES[persona]);
    const tag = tagOf(e);
    if (tag) part(box, 'span', 'tag', tag);
    part(box, 'span', 'text', String(e.text ?? ''));
    const s = sourceOf(e);
    if (s) {
      const card = part(box, 'div', 'srccard', '');
      if (s.title) part(card, 'span', 'src-title', s.title);
      if (s.domain) part(card, 'span', 'src-domain', s.domain);
      if (s.quote) part(card, 'q', 'src-quote', s.quote);
    }
    return box;
  }
  return {NAMES, SIDE, TAG, tagOf, sourceOf, fill};
})();
