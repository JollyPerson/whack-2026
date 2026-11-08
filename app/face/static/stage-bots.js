'use strict';
// The side bots on the stage: each bot's latest line (BotLine) as a speech card on its TEAM's half, the For bot
// (Prop's second) on the Proposition half and the Against bot (Opp's second) on the Opposition half. Drawn by BotCard
// (botcard.js), so names, colours, the "AI knowledge" tag and the source card match the dashboard's strip. A card
// stays hidden until its bot has spoken: with the bots off the stage is unchanged. Loaded after botcard.js and before
// stage.js, which calls Bots.reset/apply/draw. Snapshot lines show at once; only a live line animates in.
const Bots = (() => {
  let lines = {};                                // persona -> {e, live}

  function reset() { lines = {}; }
  function apply(e, live) {
    if (e.event === 'BotLine' && Object.hasOwn(BotCard.NAMES, e.persona)) lines[e.persona] = {e, live};
  }
  function draw() {
    for (const [persona, side] of Object.entries(BotCard.SIDE)) {
      const box = document.querySelector(`#${side} .botcard`);
      if (!box) continue;
      const b = lines[persona], key = b ? b.e.id : '';
      box.hidden = !b;
      if (box.dataset.key === key) continue;    // unchanged: no rebuild, no animation
      box.dataset.key = key;
      box.replaceChildren();
      if (!b) continue;
      const card = document.createElement('div');
      card.className = `botsay ${persona}${b.live ? ' enter' : ''}`;
      BotCard.fill(card, b.e, persona);
      box.appendChild(card);
    }
  }
  return {reset, apply, draw};
})();
