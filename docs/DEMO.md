# Demo script

Owner: Harkaran. Slot: **3 minutes**, judging about 12:00 Sun 8 Nov. Everything is connected before the slot starts.
Anything marked **(check)** isn't confirmed as built or decided. Clear each one at rehearsal 1.

## Who stands where

| Person | Where | Does |
|---|---|---|
| **Presenter** (picked 09:00, (check) who) | At Tong's laptop, side of the stage, facing the judges | Runs `https://<VULTR_DOMAIN>/?admin=1&audio=1` (add `&theme=light` if needed). Speaks the hook and close. Says nothing during the debate |
| **Debater A, Prop** | Left of the screen | Phone mic page (`/mic?ch=A`, (check) URL), phone about 15 cm from mouth. Holds the Prop fact card |
| **Debater B, Opp** | Right of the screen | Phone mic page (`/mic?ch=B`), same. Holds the Opp fact card |

Fact cards: [`docs/FACT-CARDS.md`](FACT-CARDS.md), printed, one per side per Motion.

## Run of show

### 0:00–0:20: hook and Motion

> **Presenter:** "By the time you'd typed a prompt about that claim, our agent had already checked it.
> It's a live debate referee. It listens, checks every factual claim as it's spoken, and when a fact is wrong it says so out loud at the next pause.
> Pick a Motion from this list."

Point at the picker on the projector. The judge picks. Presenter clicks it and starts the debate (A = Prop, B = Opp, (check) the picker sets sides).

> **Presenter, while it starts:** "Two things upfront. We built the evidence index for each of these Motions in advance. And our debaters' fact cards include some facts that are deliberately wrong. Let's see if it catches them. Prop, go."

### 0:20–2:15: the debate (presenter silent)

| Time | Who | Rule |
|---|---|---|
| 0:20–0:50 | A (Prop) | Say a figure in the first 10 s. Use your **WRONG** fact in this turn. Stop dead for a full second at the end |
| 0:50–1:20 | B (Opp) | Same: figure early, **WRONG** fact in this turn, then stop |
| 1:20–1:45 | A | Answer anything the Moderator said, then a **missing context** fact |
| 1:45–2:15 | B | Answer the Moderator, then a closing fact. Stop at 2:15 |

- One claim per sentence. Say the number and the unit ("29 per cent", "£11.5 billion").
- **Never talk over the Moderator.** When it speaks, stop. When it finishes, the next speaker reacts to it by name: "The referee says it's 65, not 90, so let me take that…". This reaction is the agent's effect on the debate, so make it visible.
- **Dry spell:** at the first Pause after 25 s of debate with no Interjection yet, the Moderator says *"Neither side has offered a figure yet. Care to name one?"* (or *"Either side, a figure I can check?"* once a figure has been said). After that it comes back only after 30 s with no Interjection, and never once the room has been quiet for 10 s. Real corrections always go first. `DIRECTOR_DRY_FIRST_S=0` turns it off. If you hear it, answer with a figure from your card straight away.

### 2:15–3:00: final screen and close

Presenter clicks **Final screen** in the admin bar, so every tab switches.

> **Presenter:** "Here's the debate as the agent saw it: every claim, by side, with its verdict and its source.
> [Point at the corrected claim.] When Prop said ___, the referee corrected it at the pause, and Opp used that in the next turn. The agent didn't describe the debate. It changed it.
> Every verdict quotes its source, and code checks that the quote really is in that source. When it isn't sure, it says unverifiable.
> Live fact-checking isn't new. Full Fact, Duke Reporters' Lab and ClaimBuster got there first. Our angle is speed, about ___ seconds from speech to verdict on that latency meter, the argument map, and an agent that moderates and interjects. Thank you."

Fill both blanks live from the screen. Use the median latency from rehearsal if the meter is busy.

## Fallback ladder

Decide on the clock, not on hope. Shout the trigger, then act.

| # | Trigger | Action | Presenter says |
|---|---|---|---|
| 1 | No transcript text **15 s** after "Prop, go", **or** no verdict chip **40 s** into the debate | Debaters stop. Admin bar: **Replay recorded debate** (`POST /face/replay`). Phones stay silent, because real mics stay open | "The venue mics aren't cooperating, so this is a replay of a debate we recorded earlier, running live through the same pipeline. The checks you're seeing happen now." |
| 2 | Venue internet down (dashboard won't load, or `/health` fails) | On Tong's laptop: `SKIP_PIP=1 ./run.sh replay --file fixtures/golden_run.jsonl`, then open `http://localhost:8000/?admin=1&audio=1` and click **Enable audio** | "The venue internet is down, so this is a saved run of the full pipeline, replayed on this laptop." |
| 3 | Nothing works | Play the backup video full screen. File: ___ on the desktop, (check) Thomas records it at 09:00 | "Here's a recording of it working." |

- The replay's Motion is the recorded debate's Motion, not the judge's pick. Today that's *ban cars from city centres* (`fixtures/fake_debate.jsonl`). It plays `fixtures/test_debate.jsonl` once that exists, **(check)** which Motion that is.
- `fixtures/golden_run.jsonl` **(check): not in the repo at 21:15 Sat.** Save it after the 20:00 run with `cp logs/latest.jsonl fixtures/golden_run.jsonl`, and keep a copy on Tong's laptop.
- The Moderator is spoken by browser speech. **(check)** that it still speaks with Wi-Fi off, for rung 2.
- If the Moderator speaks over someone or won't stop, press **M** or **Esc** to mute. **F** says the next line now.

## Pre-slot checklist (T−15 min)

- [ ] **Freeze deploys:** `gh variable set DEPLOY_ENABLED --body false`, and `gh run list --limit 3` shows nothing deploying. A deploy restarts the app and drops the debate.
- [ ] **`/health` is green:** `curl -s https://<VULTR_DOMAIN>/health` shows every module up. No red dots in the admin view.
- [ ] **Motion indexes built** for all 5 Motions on the server. Each deploy builds them in the background (`preresearch --all --missing`, needs `EXA_API_KEY`). Check that `/srv/cache/evidence` in the app container lists all 5, and that `/srv/logs/preresearch.log` shows no errors (see `docs/DEPLOY.md`, "Evidence indexes"). (check) this is merged and has run since the last deploy
- [ ] **Server profile** `APP_PROFILE=live` for the live demo, (check) it is set in `/opt/debate-ref/.env`. Ears has a red dot until `app/ears` has a `run.py`
- [ ] Tong's laptop shows `/?admin=1&audio=1`, the site password is entered, and **Enable audio** is clicked. **Only one** tab has `audio=1`.
- [ ] **Projector audio:** HDMI or the venue speaker is selected and the volume is tested with a spoken line. No silent-demo warning in admin.
- [ ] **Light theme** (`&theme=light`) if the projector washes out the dark screen.
- [ ] **Phones connected:** both are on the team hotspot (venue Wi-Fi as backup, (check)), mic permission is granted, and the screen lock is off. Each debater says one test line, it shows under the right side, then click **New debate** to clear it.
- [ ] Talkativeness is set to the rehearsal level: key ___ (1–5).
- [ ] Rung 1 tested today. Rung 2 has `.venv` and `golden_run.jsonl` on Tong's laptop. Rung 3 has the video open and paused.
- [ ] Laptop is on the charger, notifications are off, and it won't sleep (`caffeinate -d`). Phones are on Do Not Disturb.
- [ ] Fact cards are printed for all 5 Motions, both sides.

## Rehearsals (09:00–11:30, at least 3)

Use different Motions from the list. Time each one with a phone stopwatch from "Prop, go".

| # | Time | Motion | Debaters | Live or replay | First text (s) | First verdict (s) | Verdicts | Interjection spoken at a pause? | WRONG fact caught? | Median latency (s) | Run time | Fixes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 09:00 | | | | | | | | | | | |
| 2 | 09:45 | | | | | | | | | | | |
| 3 | 10:30 | | | | | | | | | | | |
| 4 | 11:00 | (judge test) | stand-in | | | | | | | | | |
| 5 | 11:30 | | | | | | | | | | | |

Rehearse the fallback ladder at least once: run rung 1 from a mid-debate failure, and run rung 2 with Wi-Fi off.

## 11:00 decision: judges or teammates?

**Default: teammates debate.** Judges debate instead only if a stand-in from **outside the team** passes this test at rehearsal 4:

1. The stand-in gets a fact card and the phone, and a Motion is picked.
2. **Pass** if the debate starts within **15 s** of the pick and it produces **at least 2 verdicts**.

Decision: ______ (Tong, as integration lead). If judges debate, the presenter hands over the phones and cards at 0:15 and says the cards include deliberately wrong facts.
