# Side bots and verdict dances: runbook

Two Mate Engine avatars stand beside the projected dashboard:
- **For** is the Proposition's second, on the left.
- **Against** is the Opposition's second, on the right.

Each listens to the **opponent** (two microphones → ears → claims) and heckles them in a text speech bubble. A bubble
can be:
- a sourced fact check;
- a confident AI-knowledge challenge, always tagged "(unsourced)";
- a "True, but…" for a misrepresented figure;
- a grudging concession of a surprising truth;
- a U-turn quoted back verbatim;
- a nudge about an unanswered point;
- a pointed follow-up question;
- now and then, dry banter between the two bots.

On the **Final screen** click, the judge writes a summary file, names the victor (a narrow margin is a draw) and makes
both bots dance. The winner dances with music, the loser in silence.

How it works and what each event means: `README.md` ("Side bots", "Debate summary and verdict dances"),
`docs/CONTRACTS.md` (BotLine, BotTalk, DebateResult) and `docs/INTEGRATION.md` (Mate Engine channels).

## What you need
- **A Linux laptop.** Tested on Arch with KDE Plasma (Wayland with XWayland), an i5-10310U and an Intel iGPU. Each
  avatar takes about 1 GB of RAM.
- **Two microphones**, one per debater, kept away from the speakers.
- **An Anthropic API key** with credit, in `.env`. It drives claim extraction, the bots' own-knowledge checks
  (Opus), follow-ups and banter (Haiku), and the judge (Opus).
- **Optional:** `EXA_API_KEY`, or saved evidence indexes, for **sourced** fact checks. Without them, verdicts are
  "unverifiable" and the bots fall back to AI knowledge, tagged as such.
- **[uv](https://docs.astral.sh/uv/)** and `parec` for the ears.
- **Our Mate Engine changes**, kept in [`mate-engine/`](../mate-engine/README.md) as patches for
  [Marksonthegamer/Mate-Engine-Linux-Port](https://github.com/Marksonthegamer/Mate-Engine-Linux-Port). Apply them
  with `git am` and build with Unity 6000.2.6f2: `./build-referee.sh <out-dir>` takes about 2 minutes after the first
  project import.
- **For the verdict dances,** the Mate Engine community CustomDances collection. It is not included here; see
  Credits.

## Run it
For the live microphone setup (one phone via MicYou + one USB-C mic: placement, levels, sound check, what the
ears terminal's warnings mean, recovery mid-demo, checklist) see [`docs/LIVE-MICS.md`](LIVE-MICS.md).

1. **Server.** Bind it to localhost, because `/face/tts` has no login:
   ```bash
   HOST=127.0.0.1 EARS=external BOTS=real JUDGE=real ./run.sh live
   ```
2. **Ears.** Find the source names with `pactl list short sources`, then:
   ```bash
   MIC1=<prop mic> MIC2=<opp mic> scripts/ears-two-mics.sh
   ```
3. **Avatars,** from the patched Mate Engine checkout:
   ```bash
   PAYLOAD=<out-dir>/Payload DANCE_LIBRARY=<CustomDances folder> tools/mate-bots.sh start
   ```
   - Each avatar gets its own config, channel, window and model.
   - The launcher also copies the three verdict dances into the build.
   - `place` re-positions the windows for another screen size (`FOR_GEOM`, `AGAINST_GEOM`); `stop` ends both.
4. **Dashboard.** Open `http://127.0.0.1:8000/?admin=1` full screen behind the avatars. Start the debate from the
   motion picker, which declares A = Prop and B = Opp.
5. **During the debate.**
   - Bubbles appear as soon as a line is ready: each bot at most once every `BOTS_MIN_GAP_S` (10 s).
   - In admin, **B** toggles the bots and **M** mutes everything.
   - Every own-knowledge check is logged ("bots: check …"), so the bars can be tuned at rehearsal.
6. **End.** Click **Final screen**.
   - The bots stop.
   - The judge writes `logs/summaries/<run>-summary.md` (and `.json`), and the verdict card appears.
   - The winner's bot dances `MMD-Conqueror` with music, and the loser's bot dances `MMD-Lost One_s Weeping` in
     silence. A draw makes both dance `MMD-皆大欢喜-卜逻desu`.
   - The dances stop after 30 s, or at the next debate.

## Settings worth knowing
| Setting | Default | What it does |
|---|---|---|
| `BOTS_MIN_GAP_S` | 10 | Seconds between one bot's bubbles |
| `BOTS_CHECK_MIN_CONF` | 0.80 | Confidence needed to call a claim false, or to concede a surprising truth, from Claude's own knowledge |
| `BOTS_MISLEADING_MIN_CONF` | 0.60 | Confidence needed for a "True, but…" challenge |
| `BOTS_BANTER`, `BOTS_BANTER_GAP_S` | on, 20 | Bot-to-bot banter. It is always the lowest priority and only when nothing important is pending |
| `JUDGE_DANCE_WIN` / `_LOSE` / `_DRAW` | see above | Dance titles (Mate Engine matches file names, ignoring case) |
| `JUDGE_MUSIC` | `winner` | `winner`, `none` or `both` |
| `JUDGE_DANCE_S` | 30 | How long the dances last |

## Troubleshooting
- **No avatar.** On a fresh config, Mate Engine's first-run tutorial hides the avatar. The launcher seeds
  `tutorialDone`; for a manual setup, finish the tutorial once.
- **An avatar says nothing.** Check the dashboard's `mate_for` / `mate_against` dots, and look for "poll failed" in
  the avatar's `player.log` (under the launcher's `STATE` dir).
- **Low frame rate.** Keep the windows away from the bottom panel and no bigger than about 900x820; the launcher caps
  the FPS at 30.
- **Wrong side.** Always start the debate from the picker. Without declared sides, whoever speaks first becomes Prop.
- **Music picked up by the mics after the verdict.** Stop the ears at the Final screen, or set `JUDGE_MUSIC=none`.

## Credits
- **[Mate Engine](https://github.com/shinyflvre/Mate-Engine)** by shinyflvre (Shiny / Johnson Jason) and
  contributors, under the MateEngine Pro License v2.0: non-commercial, source public, attribution kept.
  - The **Linux port** is [Mate-Engine-Linux-Port](https://github.com/Marksonthegamer/Mate-Engine-Linux-Port) by
    Marksonthegamer.
  - Our changes (`mate-engine/` patches) are under the same licence. It requires their source to be public, so
    make this repository (or that folder) public before distributing a build.
  - Neither project is affiliated with or endorses whack-2026.
  - The speech bubble in our changes mirrors `LLMUnitySamples.Bubble` from [LLMUnity](https://github.com/undreamai/LLMUnity)
    by Undream AI (MIT).
- **Verdict dances.** They come from the Mate Engine community's CustomDances collection: "MMD-Conqueror",
  "MMD-Lost One_s Weeping" and "MMD-皆大欢喜-卜逻desu". The motions and songs belong to their original MMD motion and
  music creators and to the collection's contributors. They are not redistributed here.
  - A later upgrade could use [Mixamo](https://www.mixamo.com) animations ("Victory", "Defeated"), which are
    royalty-free with an Adobe account.
- **Speech to text.** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) by SYSTRAN (MIT), running OpenAI's
  [Whisper](https://github.com/openai/whisper) models (MIT).
- **Language models.** [Claude](https://www.anthropic.com/claude) by Anthropic, through the Anthropic API.
- **Voice** (the referee's optional spoken lines). [ElevenLabs](https://elevenlabs.io). Its free plan asks for
  attribution and non-commercial use.
