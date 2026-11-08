'use strict';
// The cast of the stage view: one character per verdict label. Each has a look (inline SVG, no assets),
// a sign, and a voice (openers picked deterministically per claim, so a refreshed tab says the same thing).
// SVGs are drawn facing right; the opposition side mirrors them so they face the centre.
// Every SVG holds its sign stick in the hand at x≈132; the sign itself is HTML, so its text never mirrors.

const CAST = {
  supported: {
    name: 'Sunny', role: 'the cheerleader', sign: 'CHECKS OUT', icon: '✓',
    openers: [
      'Ooh, ooh, this one checks out!',
      'Gold star! The sources agree.',
      'Receipts? Got \'em. It\'s legit!',
      'I love it when the numbers behave!',
      'Yep yep yep, that\'s backed up!',
      'Confirmed! Somebody did their homework.',
    ],
    svg: `
<svg viewBox="0 0 160 190" class="art" aria-hidden="true">
  <ellipse cx="78" cy="183" rx="48" ry="6" class="shadow"/>
  <line x1="132" y1="0" x2="132" y2="150" class="stick"/>
  <g class="body">
    <path d="M78 64 C70 44 80 30 96 26 C94 44 88 56 78 64Z" fill="#2f9e44"/>
    <path d="M78 64 C74 50 64 44 54 44 C58 56 66 62 78 64Z" fill="#40c057"/>
    <ellipse cx="58" cy="176" rx="15" ry="8" fill="#2f7d32"/>
    <ellipse cx="100" cy="176" rx="15" ry="8" fill="#2f7d32"/>
    <circle cx="78" cy="118" r="56" fill="#8ce99a" stroke="#2b8a3e" stroke-width="4"/>
    <circle cx="46" cy="132" r="9" fill="#ff8fa3" opacity=".65"/>
    <circle cx="110" cy="132" r="9" fill="#ff8fa3" opacity=".65"/>
    <g class="eyes">
      <ellipse cx="60" cy="106" rx="8" ry="11" fill="#1b1b1b"/><circle cx="63" cy="101" r="3.5" fill="#fff"/>
      <ellipse cx="96" cy="106" rx="8" ry="11" fill="#1b1b1b"/><circle cx="99" cy="101" r="3.5" fill="#fff"/>
    </g>
    <path d="M56 128 Q78 160 100 128 Z" fill="#6b1d1d"/>
    <path d="M68 140 Q78 152 90 140 Q80 136 68 140Z" fill="#ff8787"/>
    <path d="M128 116 Q142 104 134 90" class="limb" stroke="#2b8a3e"/>
    <circle cx="133" cy="88" r="8" fill="#8ce99a" stroke="#2b8a3e" stroke-width="3"/>
    <g class="wave"><path d="M26 112 Q8 100 12 78" class="limb" stroke="#2b8a3e"/>
      <circle cx="12" cy="76" r="8" fill="#8ce99a" stroke="#2b8a3e" stroke-width="3"/></g>
  </g>
</svg>`,
  },

  contradicted: {
    name: 'Grumble', role: 'the stickler', sign: 'NOPE', icon: '✗',
    openers: [
      'Hold it right there.',
      'Ahem. That\'s not what the source says.',
      'Nope. I read the footnotes.',
      'Wrong-o. Allow me to fix that.',
      'Bzzt. Try again.',
      'I\'m not angry. I\'m just… disappointed.',
    ],
    svg: `
<svg viewBox="0 0 160 190" class="art" aria-hidden="true">
  <ellipse cx="78" cy="183" rx="48" ry="6" class="shadow"/>
  <line x1="132" y1="0" x2="132" y2="150" class="stick"/>
  <g class="body">
    <rect x="50" y="166" width="16" height="16" rx="4" fill="#7a1c1c"/>
    <rect x="90" y="166" width="16" height="16" rx="4" fill="#7a1c1c"/>
    <rect x="22" y="62" width="112" height="112" rx="28" fill="#ff8787" stroke="#a61e1e" stroke-width="4"/>
    <path d="M44 62 Q46 30 78 30 Q110 30 112 62 Z" fill="#212529"/>
    <ellipse cx="78" cy="62" rx="50" ry="8" fill="#212529"/>
    <rect x="46" y="50" width="64" height="6" fill="#c92a2a"/>
    <g class="eyes">
      <ellipse cx="60" cy="104" rx="7" ry="6" fill="#1b1b1b"/>
      <ellipse cx="96" cy="104" rx="7" ry="6" fill="#1b1b1b"/>
    </g>
    <path d="M42 86 L72 96" stroke="#4a1010" stroke-width="8" stroke-linecap="round"/>
    <path d="M114 86 L84 96" stroke="#4a1010" stroke-width="8" stroke-linecap="round"/>
    <path d="M60 140 Q78 126 96 140" fill="none" stroke="#4a1010" stroke-width="5" stroke-linecap="round"/>
    <g class="vein" stroke="#c92a2a" stroke-width="3" stroke-linecap="round">
      <path d="M118 74 l8 -4 M122 66 l4 8 M130 78 l-2 -8"/>
    </g>
    <path d="M132 118 Q146 108 136 92" class="limb" stroke="#a61e1e"/>
    <circle cx="133" cy="90" r="8" fill="#ff8787" stroke="#a61e1e" stroke-width="3"/>
    <path d="M24 112 Q6 126 26 142" class="limb" stroke="#a61e1e"/>
  </g>
</svg>`,
  },

  missing_context: {
    name: 'Professor Hoot', role: 'the pedant', sign: 'IT\'S COMPLICATED', icon: '~',
    openers: [
      'Well, actually…',
      'Hmm. True-ish. There\'s a footnote.',
      'Ah, but the full picture is this.',
      'Partly right. Allow me to elaborate.',
      'Context, my dear debater, context!',
      'One moment. I have notes.',
    ],
    svg: `
<svg viewBox="0 0 160 190" class="art" aria-hidden="true">
  <ellipse cx="78" cy="183" rx="48" ry="6" class="shadow"/>
  <line x1="132" y1="0" x2="132" y2="150" class="stick"/>
  <g class="body">
    <path d="M60 172 l-6 10 M66 172 v10 M72 172 l6 10 M88 172 l-6 10 M94 172 v10 M100 172 l6 10"
          stroke="#c77700" stroke-width="4" stroke-linecap="round"/>
    <path d="M34 70 L30 40 L54 60 Z" fill="#c77700"/>
    <path d="M122 70 L126 40 L102 60 Z" fill="#c77700"/>
    <ellipse cx="78" cy="118" rx="54" ry="58" fill="#ffc35a" stroke="#a35f00" stroke-width="4"/>
    <ellipse cx="78" cy="138" rx="32" ry="34" fill="#fff0cc"/>
    <path d="M62 136 q6 6 12 0 M82 136 q6 6 12 0 M70 152 q6 6 12 0" fill="none" stroke="#e0a440" stroke-width="2.5"/>
    <path d="M50 44 L78 32 L106 44 L78 56 Z" fill="#212529"/>
    <rect x="62" y="46" width="32" height="12" fill="#212529"/>
    <path d="M106 44 v18" stroke="#f59f00" stroke-width="3"/><circle cx="106" cy="64" r="3.5" fill="#f59f00"/>
    <circle cx="58" cy="100" r="17" fill="#fff" stroke="#a35f00" stroke-width="3"/>
    <circle cx="98" cy="100" r="17" fill="#fff" stroke="#a35f00" stroke-width="3"/>
    <g class="eyes">
      <circle cx="61" cy="102" r="7" fill="#1b1b1b"/><circle cx="63" cy="99" r="2.5" fill="#fff"/>
      <circle cx="101" cy="102" r="7" fill="#1b1b1b"/><circle cx="103" cy="99" r="2.5" fill="#fff"/>
    </g>
    <circle cx="98" cy="100" r="20" fill="none" stroke="#f59f00" stroke-width="3.5"/>
    <path d="M116 108 Q122 130 112 150" fill="none" stroke="#f59f00" stroke-width="1.5"/>
    <path d="M42 80 Q56 72 72 80" fill="none" stroke="#5c3300" stroke-width="5" stroke-linecap="round"/>
    <path d="M70 116 L86 116 L78 130 Z" fill="#f76707"/>
    <path d="M128 118 Q144 106 134 92" class="limb" stroke="#a35f00"/>
    <circle cx="133" cy="90" r="8" fill="#ffc35a" stroke="#a35f00" stroke-width="3"/>
    <g class="glass">
      <path d="M28 124 Q14 130 18 148" class="limb" stroke="#a35f00"/>
      <line x1="18" y1="150" x2="8" y2="172" stroke="#5c3300" stroke-width="6" stroke-linecap="round"/>
      <circle cx="22" cy="140" r="14" fill="rgba(165,216,255,.35)" stroke="#495057" stroke-width="4"/>
    </g>
  </g>
</svg>`,
  },

  unverifiable: {
    name: 'Shrug', role: 'the honest ghost', sign: 'NO IDEA', icon: '?',
    openers: [
      'I looked everywhere. Couldn\'t pin this one down.',
      'No solid source either way, sorry!',
      'Spooky: the evidence has vanished.',
    ],
    svg: `
<svg viewBox="0 0 160 190" class="art" aria-hidden="true">
  <ellipse cx="78" cy="183" rx="40" ry="5" class="shadow"/>
  <line x1="132" y1="0" x2="132" y2="150" class="stick"/>
  <g class="body">
    <path d="M24 170 L24 104 Q24 52 78 52 Q132 52 132 104 L132 170 L114 158 L96 172 L78 158 L60 172 L42 158 Z"
          fill="#e9ecef" stroke="#868e96" stroke-width="4"/>
    <g class="eyes">
      <ellipse cx="62" cy="102" rx="6" ry="9" fill="#343a40"/><ellipse cx="94" cy="102" rx="6" ry="9" fill="#343a40"/>
    </g>
    <path d="M66 130 Q78 122 90 130" fill="none" stroke="#343a40" stroke-width="4" stroke-linecap="round"/>
    <path d="M130 116 Q144 104 134 92" class="limb" stroke="#868e96"/>
    <circle cx="133" cy="90" r="8" fill="#e9ecef" stroke="#868e96" stroke-width="3"/>
    <path d="M26 112 Q8 104 14 88" class="limb" stroke="#868e96"/>
  </g>
</svg>`,
  },
};

function hashStr(s) {
  let h = 2166136261;
  for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return h >>> 0;
}
const castOf = label => CAST[label] || CAST.unverifiable;
const openerOf = (label, key) => { const o = castOf(label).openers; return o[hashStr(key) % o.length]; };
