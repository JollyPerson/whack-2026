"""The debate summary files: {JUDGE_DIR}/<run>-summary.md and the same content as JSON beside it. Owner: B.

<run> is the bus log's stem (logs/<run>.jsonl), else a timestamp; a relative JUDGE_DIR is under the repo root (logs/
is gitignored). new_path() never overwrites an earlier session's summary (<run>-2-summary.md, ...); re-judging the
same session rewrites its own file. render() builds the Markdown: motion, date, speakers and sides, the verdict
(winner, margin, headline, how it was decided), the scorecard per side, the per-criterion scores with justifications,
bias, factuality, other observations, the reasoning, the fact checks (sourced and "AI knowledge, unsourced" apart),
the system's balance, the dances, and a line saying it is an automated assessment.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from app import config
from app.brain.argmap import SIDE_LABEL, SIDES
from app.judge import scorecard as sc
from app.judge.scorecard import DECISIVE_FROM, DRAW_BELOW, Record
from app.judge.writer import CRITERION_LABEL, Judgement, weights_text

ROOT = Path(__file__).resolve().parent.parent.parent
PERSONA_LABEL = {"for": "FOR bot (the Proposition's second)", "against": "AGAINST bot (the Opposition's second)"}
LABEL_TEXT = {"supported": "supported", "contradicted": "contradicted", "missing_context": "missing context",
              "unverifiable": "unverifiable"}
_RECORD = "This is an automated assessment: a code scorecard from the live fact-checker's record"
_WRONG = "It can be wrong; every sourced fact check shows its source."
AUTOMATED = {"llm": f"{_RECORD}, weighed with the transcript by an AI judge. {_WRONG}",
             "formula": f"{_RECORD}, decided by its formula. {_WRONG}"}


def summary_dir() -> Path:
    d = Path(str(config.JUDGE_DIR or "logs/summaries")).expanduser()
    return d if d.is_absolute() else ROOT / d


def new_path(run: str) -> Path:
    """The first free <run>-summary.md, <run>-2-summary.md, ... in summary_dir()."""
    d = summary_dir()
    for n in range(1, 1000):
        p = d / (f"{run}-summary.md" if n == 1 else f"{run}-{n}-summary.md")
        if not p.exists() and not p.with_suffix(".json").exists():
            return p
    return d / f"{run}-{time.strftime('%H%M%S')}-summary.md"


def display(path: Path) -> str:
    """The path as DebateResult.report carries it: relative to the repo root when it is inside it."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def write(path: Path, rec: Record, card: dict, j: Judgement, dances: dict, music: dict, dance_s: float,
          when: Optional[float] = None, run: str = "") -> Path:
    """Write the Markdown at `path` and the JSON beside it; returns `path`. Blocking: run it in a thread."""
    when = time.time() if when is None else when
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(rec, card, j, dances, music, dance_s, when, run), encoding="utf-8")
    data = {"run": run, "date": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when)), "motion": rec.motion,
            "speakers": card["speakers"],
            "verdict": {"winner": j.winner, "margin": j.margin, "headline": j.headline, "via": j.via, "note": j.note,
                        "model": j.model},
            "criteria": list(j.criteria), "scores": j.scores, "justifications": j.why, "bias": j.bias,
            "factuality": j.factuality, "observations": j.observations, "reasoning": j.reasoning,
            "scorecard": {k: card[k] for k in ("sides", "system", "formula")}, "fact_checks": sc.fact_checks(rec),
            "dances": dances, "music": music, "dance_s": dance_s, "automated": automated(j)}
    path.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def automated(j: Judgement) -> str:
    return AUTOMATED.get(j.via, AUTOMATED["formula"])


def _flat(v: object) -> str:
    return " ".join(str(v).split())


def _cell(v: object) -> str:
    return _flat(v).replace("|", "\\|")


def _table(head: list[str], rows: list[list[object]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head),
            *("| " + " | ".join(_cell(c) for c in r) + " |" for r in rows)]


def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def verdict_line(j: Judgement) -> str:
    """The verdict, then the headline on its own line (no "A draw: A draw: ..." when the headline says it too)."""
    if j.winner == "draw":
        head = "**A draw**" + ("" if "too close to call" in j.headline.lower() else " (too close to call)")
    else:
        head = f"**The {SIDE_LABEL[j.winner]} wins** ({j.margin} margin)"
    return f"{head}\n\n> {j.headline}" if j.headline else head


def decided_by(j: Judgement) -> str:
    if j.via == "llm":
        return f"Claude ({j.model or 'LLM'}) weighing the code scorecard and the transcript" + \
            (f". {j.note}" if j.note else "")
    return f"the scorecard formula ({j.note})" if j.note else "the scorecard formula"


def render(rec: Record, card: dict, j: Judgement, dances: dict, music: dict, dance_s: float,
           when: float, run: str = "") -> str:
    sides, system = card["sides"], card["system"]
    out = [f"# Debate summary: {rec.motion or '(no motion given)'}", "",
           f"{time.strftime('%A %d %B %Y, %H:%M', time.localtime(when))}" + (f" · run `{run}`" if run else ""), ""]

    out += ["## Verdict", "", verdict_line(j), "",
            f"- Overall: Proposition **{j.scores['prop']['overall']:.1f}**, Opposition "
            f"**{j.scores['opp']['overall']:.1f}** (out of 10)",
            f"- Decided by: {decided_by(j)}",
            f"- Scorecard formula: {_formula_line(card['formula'])}", ""]

    out += ["## Speakers", ""]
    out += _table(["Side", "Speakers", "Speaking time", "Share", "Words"],
                  [[SIDE_LABEL[s], ", ".join(sides[s]["speakers"]) or "unknown", _mmss(sides[s]["speaking_s"]),
                    f"{sides[s]['share']:.0%}", sides[s]["words"]] for s in SIDES])
    other = card.get("unassigned") or {}
    if other.get("speakers"):
        out += ["", f"Also heard, on neither side and not counted: {', '.join(other['speakers'])} "
                    f"({other['words']} words)."]
    out.append("")

    out += ["## Scorecard", "", "Computed in code from the session's record, the same way for both sides.", ""]
    out += _table(["", "Proposition", "Opposition"], [[label, *(fn(sides[s]) for s in SIDES)] for label, fn in _ROWS])
    out += ["", f"Formula: {weights_text(sc.WEIGHTS)}. A difference below {DRAW_BELOW} is a draw, {DECISIVE_FROM} or "
                f"more is decisive. Verdicts below 0.6 confidence count as unverifiable; AI-knowledge results count "
                f"half.", ""]

    out += ["## Scores by criterion", ""]
    out += _table(["Criterion", "Proposition", "Opposition"],
                  [[CRITERION_LABEL.get(k, k), *(f"{j.scores[s][k]:.1f}" for s in SIDES)] for k in j.criteria]
                  + [["**Overall**", *(f"**{j.scores[s]['overall']:.1f}**" for s in SIDES)]])
    out.append("")
    for k in j.criteria:
        out.append(f"- **{CRITERION_LABEL.get(k, k)}.** " + " ".join(
            f"{SIDE_LABEL[s]}: {j.why[s].get(k) or '-'}{'' if (j.why[s].get(k) or '').endswith('.') else '.'}"
            for s in SIDES))
    out.append("")

    out += ["## Bias", "", f"- **Proposition:** {j.bias.get('prop') or '-'}",
            f"- **Opposition:** {j.bias.get('opp') or '-'}",
            f"- **The system's interventions:** {j.bias.get('system') or '-'}", ""]
    out += ["## Factuality", "", j.factuality or "-", ""]
    none = "- None: the scorecard formula decided, and only the AI judge adds observations." if j.via == "formula" \
        else "- None."
    out += ["## Other observations", "", *(f"- {o}" for o in j.observations or []), *([] if j.observations else [none]),
            ""]
    out += ["## Reasoning", "", j.reasoning or "-", ""]

    fc = sc.fact_checks(rec)
    out += ["## Fact checks", "", "### Sourced (a verdict backed by a quoted source)", ""]
    for f in fc["sourced"]:
        conf = f"{f['confidence']:.0%}" + (", counted as unverifiable" if f["counted_as"] != f["label"] else "")
        srcs = ", ".join(f"[{_flat(s['title'] or s['url']).replace('[', '(').replace(']', ')')}]({s['url']})"
                         for s in f["sources"]) or "no source"
        who = f"`{f['claim_id']}` {SIDE_LABEL.get(f['side'] or '', '?')} ({f['speaker']})"
        out.append(f"- {who}: \"{_flat(f['text'])}\" - **{LABEL_TEXT.get(f['label'], f['label'])}** ({conf}). "
                   f"{_flat(f['explanation'])} Source: {srcs}")
    if not fc["sourced"]:
        out.append("- None.")
    out += ["", "### AI knowledge, unsourced (a side bot's own check, not verified against a source)", ""]
    for a in fc["ai"]:
        what = "flagged as likely false" if a["kind"] == "challenge" else "confirmed"
        about = f" about \"{_flat(a['claim'])}\"" if a["claim"] else ""
        out.append(f"- {SIDE_LABEL.get(a['side'] or '', '?')} claim {what} by the {a['persona'].upper()} bot{about}: "
                   f"\"{_flat(a['text'])}\"")
    if not fc["ai"]:
        out.append("- None.")
    out += ["", f"Claims with no verdict (opinions, predictions, or not checked in time): {fc['unchecked']}.", ""]

    out += ["## System balance", "", "Interventions by the referee and the side bots, by the side they addressed.", ""]
    out += _table(["", "Proposition", "Opposition"], [
        ["Referee interjections", *(_kinds(sides[s]["interjections"]) for s in SIDES)],
        ["Side-bot lines", *(_kinds(sides[s]["bot_lines"]) for s in SIDES)],
        ["Pressure (all but confirmations, concessions and time calls)", *(system["pressure"][s] for s in SIDES)],
        ["Confirmations (the referee's confirms, bots conceding)", *(system["confirmations"][s] for s in SIDES)],
        ["Flagged claims (contradicted, missing context, AI-flagged)", *(system["flagged"][s] for s in SIDES)]])
    extra = [f"{system['interjections']['none']} interjections to nobody in particular"] \
        if system["interjections"]["none"] else []
    extra += [f"{system['bot_lines']['banter']} banter lines between the bots"] if system["bot_lines"]["banter"] else []
    out += ["", sc.balance_text(system) + (f" Also {', '.join(extra)}." if extra else ""), ""]

    out += ["## Verdict dances", ""]
    for p in ("for", "against"):
        if p in dances:
            out.append(f"- {PERSONA_LABEL[p]}: **{dances[p]}**" + (" with its music" if music.get(p) else ", silent"))
    out += [f"- Both stop after {dance_s:g} s or at the next debate.", "", "---", "", f"_{automated(j)}_", ""]
    return "\n".join(out)


def _formula_line(f: dict) -> str:
    who = "a draw" if f["winner"] == "draw" else f"{SIDE_LABEL[f['winner']]} ({f['margin']})"
    return f"{who}, Proposition {f['overall']['prop']:.1f} to Opposition {f['overall']['opp']:.1f}"


def _kinds(d: dict) -> str:
    rest = ", ".join(f"{k} {v}" for k, v in sorted(d.items()) if k != "total")
    return f"{d['total']}" + (f" ({rest})" if rest else "")


def _verdicts(st: dict) -> str:
    v = st["verdicts"]
    return " / ".join(str(v[k]) for k in sc.LABELS)


_ROWS = [
    ("Speaking time", lambda st: _mmss(st["speaking_s"])),
    ("Words", lambda st: st["words"]),
    ("Claims (checkable)", lambda st: f"{st['claims']} ({st['checkable']})"),
    ("Sourced verdicts: supported / contradicted / missing context / unverifiable", _verdicts),
    ("Verdicts with a source", lambda st: st["verdicts"]["with_source"]),
    ("AI knowledge (unsourced): flagged / confirmed", lambda st: f"{st['ai_flagged']} / {st['ai_confirmed']}"),
    ("Its points the opponent left unanswered", lambda st: st["unanswered"]),
    ("Its points the opponent conceded", lambda st: st["conceded_to"]),
    ("Opponent's points it answered / conceded / ignored",
     lambda st: f"{st['answered']} / {st['conceded']} / {st['ignored']}"),
    ("Non-responsive turns", lambda st: st["nonresponsive"]),
    *((f"Formula {k}", lambda st, k=k: f"{st['criteria'][k]:.1f}") for k in sc.CRITERIA),
    ("**Formula overall**", lambda st: f"**{st['overall']:.1f}**"),
]
