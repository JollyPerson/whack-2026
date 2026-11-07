"""Claim extractor: LLM mapping, fallbacks, generation guard, assembler, dedupe, heuristic on the fixture.
Deterministic and offline: stub LLM, injected clock, fake argument map (plus the real ArgMap when built)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app import config
from app.brain import claims as C
from app.brain.claims import ClaimExtractor
from app.brain.llm import LLMRefusal
from app.brain.prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT
from app.bus import load_jsonl
from app.schemas import Claim, Pause, Utterance

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_debate.jsonl"
T = 1000.0


class FakeMap:
    """Records call order; side_of/context_for/find_duplicate configurable."""

    def __init__(self, sides=None, motion="This house would ban cars from city centres"):
        self.sides, self.motion = dict(sides or {}), motion
        self.claims: list[Claim] = []
        self.log: list[tuple] = []
        self.dups: dict[str, str] = {}
        self.merged: list[tuple] = []

    def side_of(self, speaker):
        return self.sides.get(speaker, "")          # "" = unknown -> extractor's own first-seen rule

    def add_claim(self, c):
        self.log.append(("add", c.id))
        self.claims.append(c)

    def context_for(self, speaker, limit=15):
        return self.claims[-limit:]

    def turn_text(self, speaker, exclude_utterance_id=None):
        return []

    def find_duplicate(self, speaker, text):
        return self.dups.get(text)

    def merge_repeat(self, claim_id, links):
        self.merged.append((claim_id, [(l.claim_id, l.relation) for l in links]))


class StubLLM:
    name = "stub"

    def __init__(self, reply=None, delay=0.0, gate: asyncio.Event | None = None):
        self.reply, self.delay, self.gate = reply, delay, gate
        self.calls: list[dict] = []
        self.active = self.peak = 0

    async def complete_json(self, system, user, schema, timeout, max_tokens=400):
        self.calls.append(dict(system=system, user=user, schema=schema, timeout=timeout, max_tokens=max_tokens))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            r = self.reply(user) if callable(self.reply) else self.reply
            if isinstance(r, BaseException):
                raise r
            return r
        finally:
            self.active -= 1


def utt(text, speaker="A", uid="u-A-0001", t_end=T, final=True, t_start=None):
    return Utterance(id=uid, speaker=speaker, text=text, t_start=t_end - 3 if t_start is None else t_start,
                     t_end=t_end, final=final, ts=t_end + 0.8)


def make(llm=None, sides=None, clock=None, **kw):
    amap, pub = FakeMap(sides if sides is not None else {"A": "prop", "B": "opp"}, **kw), []

    def publish(ev):
        amap.log.append(("publish", ev.id))
        pub.append(ev)
        return ev

    ex = ClaimExtractor(amap, publish, llm, clock=clock or (lambda: T + 0.5))
    return ex, amap, pub


def seed(amap, cid, speaker, side, text, importance=0.67):
    amap.claims.append(Claim(id=cid, speaker=speaker, side=side, text=text, importance=importance))


def one(text="x", type="statistic", checkable=True, importance=2, topic="t", links=()):
    return {"text": text, "type": type, "checkable": checkable, "importance": importance, "topic": topic,
            "links": [{"to": to, "rel": rel} for to, rel in links]}


# ----------------------------------------------------------------------------- LLM mapping
async def test_llm_output_maps_to_claim_fields():
    llm = StubLLM({"claims": [one("Unemployment rose to 6 percent in 2024.", importance=3,
                                  topic="UK unemployment rate since covid", links=[("c900", "attacks"),
                                                                                    ("c999", "attacks"),
                                                                                    ("c900", "supports")])]})
    ex, amap, pub = make(llm)
    seed(amap, "c900", "B", "opp", "Unemployment is falling.")
    out = await ex.extract(utt("Unemployment has gone up to six percent this year.", uid="u-A-0007"))
    assert out == pub and len(pub) == 1
    c = pub[0]
    assert re.fullmatch(r"c\d+", c.id) and c.id != "c900"
    assert (c.utterance_id, c.speaker, c.side, c.source) == ("u-A-0007", "A", "prop", "llm")
    assert c.text == "Unemployment rose to 6 percent in 2024." and c.type == "statistic" and c.checkable
    assert c.importance == 1.0 and c.topic == "UK unemployment rate since"
    assert [(l.claim_id, l.relation) for l in c.links] == [("c900", "attacks")]   # unknown id dropped, deduped
    assert c.t_end == T and c.marks == {"speech_end": T, "stt_final": T + 0.8, "claim": T + 0.5}
    assert amap.log[-2:] == [("add", c.id), ("publish", c.id)]                  # map first, then bus
    call = llm.calls[0]
    assert call["system"] == SYSTEM_PROMPT and call["schema"] == OUTPUT_SCHEMA and call["max_tokens"] == 400
    assert "c900 [OPP] Unemployment is falling." in call["user"]
    assert "SENTENCE [PROP]: Unemployment has gone up" in call["user"]
    assert "MOTION: This house would ban cars from city centres" in call["user"]
    assert ex.stats["calls"] == 1 and ex.stats["fallbacks"] == 0 and ex.stats["p50_ms"] is not None


async def test_value_and_prediction_forced_uncheckable_and_importance_mapping():
    llm = StubLLM({"claims": [one("Cars should be banned.", "value", True, 1),
                              one("Shops will close.", "prediction", True, 2),
                              one("Bus use fell by a third.", "statistic", True, 3),
                              one("Fourth claim is dropped.", "fact", True, 2)]})
    ex, _, pub = make(llm)
    await ex.extract(utt("Long sentence with several claims in it, apparently."))
    assert [(c.type, c.checkable, c.importance) for c in pub] == [
        ("value", False, 0.33), ("prediction", False, 0.67), ("statistic", True, 1.0)]


async def test_unknown_type_becomes_other_and_empty_list_means_no_claims():
    ex, _, pub = make(StubLLM({"claims": [one("Something odd.", "vibes")]}))
    await ex.extract(utt("Something odd happened here today."))
    assert pub[0].type == "other"
    ex, _, pub = make(StubLLM({"claims": []}))
    assert await ex.extract(utt("Air pollution kills 40,000 people a year.")) == []
    assert ex.stats["fallbacks"] == 0                                          # LLM said "no claim": respected


@pytest.mark.parametrize("v,expect", [
    (True, True), ("true", True), ("True", True), ("yes", True), ("Yes.", True), ("y", True), (1, True), (1.0, True),
    ("1", True), (0.9, True),
    (False, False), ("false", False), ("no", False), (0, False), ("0", False), (None, False), ("maybe", False),
    ([], False),
])
def test_checkable_non_boolean_from_json_mode(v, expect):
    """llm-crosscompat: OpenAI JSON-mode (non-strict) answered checkable "yes"/1 and it became False."""
    assert C.drafts_from_llm({"claims": [one("Crime rose 9%.", checkable=v)]}, set())[0].checkable is expect


def test_json_mode_shape_variants():
    d, = C.drafts_from_llm({"claims": one("Crime rose 9%.", importance="high")}, set())   # one object, not a list
    assert (d.text, d.importance) == ("Crime rose 9%.", 1.0)
    assert [C.map_importance(x) for x in ("low", "Medium", "HIGH", "2", "huge")] == [0.33, 0.67, 1.0, 0.67, 0.5]
    out = {"claims": [{"text": "That figure is wrong.", "type": "fact", "checkable": True, "importance": 2,
                       "links": [{"to": "c3", "rel": "attack"}, {"to": "c4", "rel": "Support"}]}]}
    assert C.drafts_from_llm(out, {"c3", "c4"})[0].links == [("c3", "attacks"), ("c4", "supports")]


# ----------------------------------------------------------------------------- fallbacks
async def test_timeout_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setattr(config, "CLAIM_TIMEOUT_S", 0.05)
    ex, _, pub = make(StubLLM({"claims": []}, delay=1.0))
    await ex.extract(utt("Nearly 70 percent of shoppers arrive by car."))
    assert len(pub) == 1 and pub[0].source == "heuristic" and pub[0].type == "statistic" and pub[0].checkable
    assert ex.stats["by_cause"]["timeout"] == 1 and ex.stats["fallbacks"] == 1
    assert ex.stats["p50_ms"] >= 40


@pytest.mark.parametrize("reply,cause", [
    ({"nope": 1}, "invalid"),
    ("{not json", "invalid"),
    ({"claims": "lots"}, "invalid"),
    (LLMRefusal("declined"), "refusal"),
    (RuntimeError("socket closed"), "error"),
])
async def test_bad_llm_output_falls_back_with_cause(reply, cause):
    ex, _, pub = make(StubLLM(reply))
    await ex.extract(utt("Bus use outside London has fallen by a third since 2010."))
    assert [c.source for c in pub] == ["heuristic"]
    assert ex.stats["by_cause"][cause] == 1
    assert ex.stats["fallbacks"] == sum(ex.stats["by_cause"].values()) == 1


async def test_stale_speech_skips_llm():
    llm = StubLLM({"claims": [one("x")]})
    ex, _, pub = make(llm, clock=lambda: T + config.CLAIM_STALE_S + 5)
    await ex.extract(utt("Air pollution causes 40,000 early deaths a year."))
    assert llm.calls == [] and pub[0].source == "heuristic" and ex.stats["by_cause"]["stale"] == 1


async def test_no_llm_is_heuristic():
    ex, _, pub = make(None)
    await ex.extract(utt("Air pollution causes 40,000 early deaths a year."))
    assert ex.stats["llm"] == "heuristic" and ex.stats["by_cause"]["no_llm"] == 1 and pub[0].source == "heuristic"


async def test_concurrency_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "CLAIM_CONCURRENCY", 2)
    llm = StubLLM({"claims": []}, delay=0.02)
    ex, _, _ = make(llm)
    text = " ".join(f"Sentence number {i} has enough words." for i in range(5))
    await ex.extract(utt(text))
    assert len(llm.calls) == 5 and llm.peak == 2


# ----------------------------------------------------------------------------- generation guard / dedupe
async def test_reset_during_inflight_extract_publishes_nothing():
    gate = asyncio.Event()
    ex, amap, pub = make(StubLLM({"claims": [one("Late claim.")]}, gate=gate))
    task = asyncio.create_task(ex.extract(utt("This will come back after the reset happens.")))
    await asyncio.sleep(0.01)
    ex.reset()
    gate.set()
    assert await task == []
    assert pub == [] and amap.claims == []


async def test_duplicate_is_merged_not_published():
    ex, amap, pub = make(StubLLM({"claims": [one("Repeat claim.", links=[("c901", "attacks")])]}))
    seed(amap, "c901", "B", "opp", "Opponent point.")
    amap.dups["Repeat claim."] = "c777"
    assert await ex.extract(utt("Saying the same thing yet again now.")) == []
    assert pub == [] and amap.merged == [("c777", [("c901", "attacks")])]


# ----------------------------------------------------------------------------- sentence assembler
def test_fragment_then_continuation():
    ex, _, _ = make()
    assert ex.assemble(utt("Unemployment has risen to", uid="u1", t_end=10.0, t_start=8.0), 10.0) == []
    units = ex.assemble(utt("6 percent since 2019.", uid="u2", t_end=12.0), 12.0)
    assert len(units) == 1
    u = units[0]
    assert (u.text, u.id, u.t_start, u.t_end) == ("Unemployment has risen to 6 percent since 2019.", "u2", 8.0, 12.0)


def test_complete_sentence_passes_through_untouched():
    ex, _, _ = make()
    u = utt("Inflation is 4 percent.")
    assert ex.assemble(u, T) == [u] and ex.assemble(utt("ignored", final=False), T) == []


def test_complete_head_emitted_tail_buffered_until_pause():
    ex, _, _ = make()
    units = ex.assemble(utt("Inflation is 4 percent. And the rate of", uid="u1"), T)
    assert [x.text for x in units] == ["Inflation is 4 percent."]
    assert ex.on_pause("B", T + 5) == []
    assert ex.on_pause("A", T + 0.5) == []                                   # never at once
    assert ex.flush_due(T + 0.4 + C.FRAG_PAUSE_FINALS_S) == []
    assert [x.text for x in ex.flush_due(T + 0.5 + C.FRAG_PAUSE_FINALS_S)] == ["And the rate of"]
    assert ex.on_pause("A", T + 9) == [] and ex.flush() == []


def test_quiet_flush_and_max_age():
    ex, _, _ = make()
    ex.assemble(utt("so the thing about buses", uid="u1"), 100.0)
    assert ex.flush_due(100.0 + C.FRAG_QUIET_FINALS_S - 0.1) == []
    assert [x.text for x in ex.flush_due(100.0 + C.FRAG_QUIET_FINALS_S)] == ["so the thing about buses"]
    for i in range(5):                                               # steady speech, never punctuated
        t = 200.0 + 1.5 * i
        ex.assemble(utt(f"words part {i}", uid=f"v{i}"), t)
        assert ex.flush_due(t + 1.0) == []
    assert ex.flush_due(200.0 + C.FRAG_MAX_AGE_S - 0.1) == []
    assert [x.text for x in ex.flush_due(200.0 + C.FRAG_MAX_AGE_S)] == [" ".join(f"words part {i}" for i in range(5))]


def drive(ex, events, until, tick=0.25):
    """Feed (t, Utterance | Pause) in order with flush_due() ticks in between, like brain/run.py (which calls
    assemble() for finals and, once wired, interims). Returns [(t, unit)] of released units."""
    out, t = [], events[0][0]
    for at, ev in events + [(until, None)]:
        while t + tick <= at:
            t += tick
            out += [(t, u) for u in ex.flush_due(t)]
        if ev is None:
            return out
        res = ex.on_pause(ev.speaker, at) if isinstance(ev, Pause) else ex.assemble(ev, at)
        out += [(at, u) for u in res]


def interims(text, uid, t0, t1, every=0.4, speaker="A"):
    words = text.split()
    n = max(1, int((t1 - t0) / every))
    return [(t0 + every * (k + 1), utt(" ".join(words[:max(1, len(words) * (k + 1) // (n + 1))]) + f" {k}",
                                       speaker=speaker, uid=uid, final=False, t_end=t0 + every * (k + 1)))
            for k in range(n) if t0 + every * (k + 1) < t1]


@pytest.mark.parametrize("gap", [2.0, 2.9])
def test_split_final_rejoined_at_normal_cadence_finals_only(gap):
    """Review brain-correctness #5 (s8b): finals > 1.5 s apart were flushed in halves. Finals-only ears (or
    run.py not passing interims yet) get the longer timers."""
    ex, _, _ = make()
    out = drive(ex, [(100.0, utt("Air pollution causes around 40,000", uid="u1", t_end=100.0)),
                     (100.0 + gap, utt("early deaths every year in the UK.", uid="u2", t_end=100.0 + gap))], 110.0)
    assert [u.text for _, u in out] == ["Air pollution causes around 40,000 early deaths every year in the UK."]


@pytest.mark.parametrize("every", [0.4, 1.4])
def test_interims_keep_fragment_open_through_a_long_segment(every):
    """Same split with a 5 s second segment: interims show the speaker is still talking. Sparse interims (1.4 s)
    also get run.py's PauseSynth firing 1.2 s after each one: that Pause must not cut the sentence (s8b GAP=5)."""
    ex, _, _ = make()
    seg = "early deaths every year in the UK from road traffic fumes alone."
    evs = [(100.0, utt("Air pollution causes around 40,000", uid="u1", t_end=100.0))]
    evs += interims(seg, "u2", 100.0, 105.0, every=every)
    if every > 1.2:                                                    # PauseSynth: 1.2 s after any speech
        evs = sorted(evs + [(t + 1.2, Pause(speaker="A")) for t, _ in evs if t + 1.2 < 105.0],
                     key=lambda e: e[0])
    evs += [(105.0, utt(seg, uid="u2", t_end=105.0))]
    out = drive(ex, evs, 112.0)
    assert [u.text for _, u in out] == [f"Air pollution causes around 40,000 {seg}"]


def test_unchanged_or_empty_interims_do_not_hold_a_fragment():
    ex, _, _ = make()
    evs = [(100.0, utt("so the point about buses", uid="u1", t_end=100.0))]
    evs += [(100.0 + 0.5 * k, utt("and", uid="u2", final=False)) for k in range(1, 20)]   # STT repeating itself
    assert drive(ex, evs, 112.0)[0][0] == 100.5 + C.FRAG_QUIET_S                         # first one counted only
    ex, _, _ = make()
    evs = [(100.0, utt("so the point about buses", uid="u1", t_end=100.0))]
    evs += [(100.0 + 0.5 * k, utt("", uid="u2", final=False)) for k in range(1, 20)]      # Deepgram silence
    assert drive(ex, evs, 112.0)[0][0] == 100.0 + C.FRAG_QUIET_FINALS_S


@pytest.mark.parametrize("with_interims", [False, True])
def test_mid_sentence_pause_storm_does_not_cut(with_interims):
    """Review brain-correctness #5 (s8): unpunctuated finals 1 s apart + a VAD Pause (x5) at every breath
    produced 'Early deaths a year in the UK and road traffic.' / 'Is the biggest single source ...' claims."""
    chunks = ["air pollution causes around 40,000", "early deaths a year in the UK and road traffic",
              "is the biggest single source of nitrogen dioxide", "in our cities according to the ONS"]
    ex, _, _ = make()
    evs = []
    for i, ch in enumerate(chunks):
        t = 100.0 + i
        if with_interims and i:
            evs += interims(ch, f"u{i}", t - 0.7, t, every=0.2)
        evs += [(t, utt(ch, uid=f"u{i}", t_end=t))] + [(t, Pause(speaker="A"))] * 5
    out = drive(ex, evs, 112.0)
    assert [u.text for _, u in out] == [" ".join(chunks)]
    assert out[0][0] == 103.0 + (C.FRAG_PAUSE_S if with_interims else C.FRAG_PAUSE_FINALS_S)   # end of turn: grace


def test_pause_shortens_the_wait_and_resumed_speech_cancels_it():
    ex, _, _ = make()
    ex.assemble(utt("so the point about buses", uid="u1"), 100.0)
    assert ex.on_pause("A", 100.2) == [] and ex.on_pause("A", 100.9) == []      # a storm: the first one counts
    assert ex.flush_due(100.1 + C.FRAG_PAUSE_FINALS_S) == []
    assert [x.text for x in ex.flush_due(100.2 + C.FRAG_PAUSE_FINALS_S)] == ["so the point about buses"]
    ex.assemble(utt("so the point about buses", uid="u1"), 150.0)              # caller that never ticks:
    assert [x.text for x in ex.on_pause("A", 155.0)] == ["so the point about buses"]   # overdue goes now
    ex.assemble(utt("so the point about buses", uid="u2"), 200.0)
    ex.on_pause("A", 200.2)
    ex.assemble(utt("is that nobody rides them", uid="u3"), 201.5)              # speech resumed: Pause void
    assert ex.flush_due(201.5 + C.FRAG_PAUSE_FINALS_S) == []
    assert [x.text for x in ex.flush_due(201.5 + C.FRAG_QUIET_FINALS_S)] == [
        "so the point about buses is that nobody rides them"]


def test_interims_cannot_hold_a_fragment_past_max_age():
    ex, _, _ = make()
    evs = [(100.0, utt("and so the argument goes", uid="u1", t_end=100.0))]
    evs += interims("on and on " * 20, "u2", 100.0, 120.0)
    assert drive(ex, evs, 120.0)[0][0] == 100.0 + C.FRAG_MAX_AGE_S


async def test_stale_check_ignores_assembler_hold():
    """A fragment held open by interims still gets the LLM: only STT lag + queueing count as staleness."""
    now = [100.8]
    llm = StubLLM({"claims": [one("Air pollution causes 40,000 early deaths.")]})
    ex, _, pub = make(llm, clock=lambda: now[0])
    ex.assemble(utt("Air pollution causes 40,000 early deaths", uid="u1", t_end=100.0), 100.8)
    out = drive(ex, interims("and more words keep coming " * 5, "u2", 100.8, 110.0), 110.0)
    assert len(out) == 1 and out[0][0] - 100.0 > config.CLAIM_STALE_S             # released by max age
    now[0] = out[0][0]
    await ex.extract(out[0][1])
    assert len(llm.calls) == 1 and [c.source for c in pub] == ["llm"] and ex.stats["by_cause"]["stale"] == 0


@pytest.mark.parametrize("t_end", [41.2, T + 0.8 - 3600, T + 0.8 + 120])
async def test_wrong_clock_t_end_falls_back_to_ts(t_end):
    """Stream-relative / skewed t_end must not make every sentence 'stale' (LLM skipped) or stamp claims with it."""
    llm = StubLLM({"claims": [one("Unemployment is 6 percent.")]})
    ex, _, pub = make(llm)
    u = Utterance(id="u1", speaker="A", text="Unemployment is 6 percent.", t_start=t_end - 2, t_end=t_end, ts=T)
    await ex.extract(u)
    assert len(llm.calls) == 1 and pub[0].source == "llm"
    assert pub[0].t_end == T and pub[0].marks["speech_end"] == T


def test_speakers_buffer_independently():
    ex, _, _ = make()
    ex.assemble(utt("our figures show that", speaker="A", uid="a1"), T)
    units = ex.assemble(utt("That is wrong.", speaker="B", uid="b1"), T)
    assert [x.text for x in units] == ["That is wrong."]
    assert [x.text for x in ex.assemble(utt("crime fell.", speaker="A", uid="a2"), T)] == ["our figures show that crime fell."]


async def test_filler_sentences_are_dropped():
    llm = StubLLM({"claims": []})
    ex, _, _ = make(llm)
    await ex.extract(utt("Thank you. Can I just finish my point? Okay. Inflation is 4%."))
    assert len(llm.calls) == 1 and "SENTENCE [PROP]: Inflation is 4%." in llm.calls[0]["user"]


# ----------------------------------------------------------------------------- sides
async def test_opaque_speaker_ids_without_session():
    ex, _, pub = make(None, sides={})
    await ex.extract(utt("Unemployment is 4 percent in Leeds.", speaker="Gov", uid="g1"))
    await ex.extract(utt("Crime fell by 10 percent last year.", speaker="spk7", uid="s1"))
    await ex.extract(utt("Wages rose 3 percent in 2023.", speaker="Gov", uid="g2"))
    assert [(c.speaker, c.side) for c in pub] == [("Gov", "prop"), ("spk7", "opp"), ("Gov", "prop")]


# ----------------------------------------------------------------------------- warmup
async def test_warmup():
    ex, _, _ = make(None)
    await ex.warmup("motion")                                       # no LLM: no-op
    llm = StubLLM({"claims": []})
    ex, _, _ = make(llm)
    await ex.warmup("Ban cars")
    assert len(llm.calls) == 1 and llm.calls[0]["schema"] == OUTPUT_SCHEMA and "MOTION: Ban cars" in llm.calls[0]["user"]
    assert ex.stats["warmup"] == "ok"
    ex, _, _ = make(StubLLM(RuntimeError("down")))
    await ex.warmup("Ban cars")                                     # never raises
    assert ex.stats["warmup"] == "failed:error"


# ----------------------------------------------------------------------------- heuristic
@pytest.mark.parametrize("text,typ,checkable", [
    ("Unemployment is 4.2%.", "statistic", True),
    ("The IFS says the policy will cost three billion pounds.", "attribution", True),
    ("You voted against this in 2019.", "attribution", True),
    ("We must protect the NHS.", "value", False),
    ("I just think people should be free to choose.", "value", False),
    ("Prices will rise by 10% if this passes.", "prediction", False),
    ("Crime has risen every year since the Tories took office.", "comparison", True),
    ("Things are getting worse and everyone knows it.", "comparison", False),
    ("Smoking causes lung cancer.", "causal", True),
    ("No country has ever done this successfully.", "fact", False),
    ("Isn't it true that one in five children lives in poverty?", "statistic", True),
])
def test_heuristic_types(text, typ, checkable):
    d = C.heuristic_drafts(text, "prop")
    assert [(x.type, x.checkable) for x in d] == [(typ, checkable)]


def test_heuristic_cleaning_and_filters():
    assert C.clean_text("Honestly, I just think people should be free.") == ("People should be free.", True)
    assert C.clean_text("So the answer is simply to invest") == ("The answer is to invest.", False)
    assert C.clean_text("It was just 5 percent.")[0] == "It was just 5 percent."   # "just" before a number stays
    assert not C.keep_sentence("Thank you.") and not C.keep_sentence("Can I just finish my point?")
    assert C.keep_sentence("Inflation is 4%.") and not C.keep_sentence("That is fine.")
    assert C.heuristic_drafts("What are you talking about?", "prop") == []
    assert C.heuristic_drafts("And that is exactly the problem.", "prop") == []


def _fixture_run(amap_factory):
    async def go():
        events = load_jsonl(FIXTURE)
        session = events[0]
        amap = amap_factory(session)
        pub: list[Claim] = []
        ex = ClaimExtractor(amap, pub.append, None, clock=lambda: 0.0)
        for ev in events:
            for unit in ex.flush_due(ev.ts):
                await ex.extract(unit)
            if ev.event == "Utterance":
                if hasattr(amap, "on_utterance") and ev.final:
                    amap.on_utterance(ev)
                for unit in ex.assemble(ev, ev.ts):
                    await ex.extract(unit)
            elif ev.event == "Pause":
                for unit in ex.on_pause(ev.speaker, ev.ts):
                    await ex.extract(unit)
                if hasattr(amap, "on_pause"):
                    amap.on_pause(ev)
        for unit in ex.flush():
            await ex.extract(unit)
        return pub
    return go()


def _fake_map(session):
    return FakeMap({k: v.side for k, v in session.speakers.items()}, session.motion)


def _real_map(session):
    from app.brain.argmap import ArgMap
    try:
        m = ArgMap(k_turns=1)
        m.reset(session)
    except NotImplementedError:
        pytest.skip("app.brain.argmap not implemented yet")
    return m


@pytest.mark.parametrize("factory", [_fake_map, _real_map], ids=["fake_map", "real_argmap"])
async def test_heuristic_on_fixture(factory):
    pub = await _fixture_run(factory)
    by_u: dict[str, list[Claim]] = {}
    for c in pub:
        by_u.setdefault(c.utterance_id, []).append(c)
    numeric = ["u-A-0001", "u-A-0002", "u-B-0001", "u-B-0003", "u-A-0004", "u-A-0005"]
    for uid in numeric:
        assert any(c.checkable for c in by_u.get(uid, [])), uid
    assert [(c.type, c.checkable) for c in by_u["u-B-0004"]] == [("value", False)]
    assert by_u["u-B-0004"][0].text == "People should be free to choose how they travel."
    assert [(c.type, c.checkable) for c in by_u["u-B-0006"]] == [("prediction", False)]
    assert [(c.type, c.checkable) for c in by_u["u-A-0006"]] == [("value", False)]
    deaths = next(c for c in by_u["u-A-0001"] if "40,000" in c.text)
    rebut = by_u["u-B-0001"][0]
    assert rebut.text.startswith("That 40,000 figure is a modelled estimate")
    assert [(l.claim_id, l.relation) for l in rebut.links] == [(deaths.id, "attacks")]
    assert all(c.source == "heuristic" and c.side == ("prop" if c.speaker == "A" else "opp") for c in pub)
    assert all(not l or l.relation == "attacks" for c in pub for l in c.links)
    assert all(1 <= len(c.topic.split()) <= 4 for c in pub)
    assert not any("Thank you" in c.text or "finish my point" in c.text for c in pub)
