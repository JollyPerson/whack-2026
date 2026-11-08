"""brain/run.py: ingress guards (echo, bleed, late interims), pause synthesis, time rebasing and crash-restore.

Units are tested directly; the wiring runs the real brain on a real Bus with a fake wall clock that only moves
when a test says so (brain, bus and Director all read time.time()). asyncio sleeps stay real but short.
Claims/verdicts under test come from a stand-in extractor/evidence (src="ext"), so these tests don't depend
on claim-extraction details.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app import config
from app.brain import run as brain_run
from app.brain.run import BleedGuard, EchoGuard, LateInterims, PauseSynth, _fill_times
from app.bus import Bus
from app.schemas import Claim, Control, Evidence, Interjection, Pause, SessionStart, TTSState, Utterance, Verdict

T0 = 1_762_532_400.0
EVID = [Evidence(url="https://www.ons.gov.uk/x", title="ONS", quote="q")]


def U(spk, text, a, b, final=True, uid=None, ts=None):
    kw = {"id": uid} if uid else {}
    return Utterance(speaker=spk, text=text, t_start=a, t_end=b, final=final, ts=b if ts is None else ts, **kw)


# ----------------------------------------------------------------------------- units
def test_fill_times_rebases_unknown_stream_relative_and_other_clock_times():
    T = T0
    u = _fill_times(Utterance(speaker="A", text="x", t_start=0.4, t_end=2.8, ts=T))      # Deepgram start/end
    assert (u.t_start, u.t_end) == pytest.approx((T - 2.4, T))
    u = _fill_times(Utterance(speaker="A", text="x", t_start=T - 3604, t_end=T - 3600, ts=T))   # a clock an hour off
    assert (u.t_start, u.t_end) == (T - 4, T)
    assert _fill_times(Utterance(speaker="A", text="x", ts=T)).model_dump(include={"t_start", "t_end"}) == \
        {"t_start": T, "t_end": T}
    u = _fill_times(Utterance(speaker="A", text="x", t_end=T - 1, ts=T))                 # start unknown
    assert (u.t_start, u.t_end) == (T - 1, T - 1)
    u = _fill_times(Utterance(speaker="A", text="x", t_start=T - 3, ts=T))               # end unknown
    assert (u.t_start, u.t_end) == (T - 3, T)
    good = Utterance(speaker="A", text="x", t_start=T - 4, t_end=T - .2, ts=T)
    assert _fill_times(good) is good and _fill_times(good, now=T + 1) is good
    assert _fill_times(Pause(speaker="A", t=3.6, ts=T)).t == T and _fill_times(Pause(speaker="A", ts=T)).t == T
    assert _fill_times(Claim(id="c", speaker="A", text="x", t_end=2.8, ts=T)).t_end == T
    assert _fill_times(Claim(id="c", speaker="A", text="x", ts=T)).t_end == 0             # 0 stays "unknown"
    u = _fill_times(Utterance(speaker="A", text="x", t_start=1.0, t_end=3.0, ts=5.0), now=T)   # producer-set ts
    assert (u.ts, u.t_start, u.t_end) == (T, T - 2, T)


def test_echo_guard_drops_our_voice_and_expires_a_lost_speaking_false():
    e = EchoGuard()
    e.speaking(True, T0)
    assert e.is_echo(U("A", "hello there everyone", T0 + 1, T0 + 2), T0 + 2)
    assert e.is_echo(U("A", "hello there everyone", T0 + 14, T0 + 14.5), T0 + 14.5)
    assert not e.is_echo(U("A", "a human is talking now", T0 + 16, T0 + 18), T0 + 18)   # speaking:false never came
    assert e.speaking_since is None and e.windows[-1] == (T0, T0 + 15.5)
    assert e.is_echo(U("A", "words words words", T0 + 10, T0 + 12), T0 + 19)            # late final from inside it
    e.speaking(True, T0 + 30)
    e.speaking(False, T0 + 33)
    assert e.is_echo(U("A", "x", T0 + 33.2, T0 + 34), T0 + 34) and not e.is_echo(U("A", "x", T0 + 34, T0 + 35), T0 + 35)
    e.spoke("Point of fact: the ONS says it is 4.1 percent.", T0 + 40)
    assert e.is_echo(U("B", "the ONS says it is 4.1 percent", T0 + 41, T0 + 42), T0 + 42)
    assert not e.is_echo(U("B", "that is a modelled estimate though", T0 + 41, T0 + 42), T0 + 42)
    e.reset()
    assert e.speaking_since is None and e.windows == [] and e.said == []


def test_echo_guard_while_speaking_drops_only_our_words():
    """Barge-in: during our TTS (and its tail) only words matching what we're saying are echo; a human talking
    over the referee passes, a 1-2 word interruption is floor-only."""
    e = EchoGuard()
    e.spoke("Point of fact: the ONS says it is 4.1 percent, not 40.", T0 - .5)
    e.speaking(True, T0)
    c = lambda text, a, b: e.classify(U("A", text, a, b), b)            # noqa: E731
    assert c("point of fact the owns says it is for point one percent", T0 + .2, T0 + 3) == "echo"   # garbled STT
    assert c("point of", T0 + .2, T0 + .8) == "echo"                     # first interim of our echo
    assert c("it is", T0 + .2, T0 + .8) == "echo"
    assert c("no the figure is forty percent and falling", T0 + 1, T0 + 3) == "speech"
    assert c("hold on", T0 + 1, T0 + 1.5) == "floor"
    assert c("wait that is wrong", T0 + 1, T0 + 2) == "speech"
    e.speaking(False, T0 + 4)
    assert c("the owns says it is 4.1 percent", T0 + 4.2, T0 + 5) == "echo"           # tail window
    assert c("road deaths fell by thirty percent", T0 + 4.2, T0 + 6) == "speech"
    assert c("hang on", T0 + 4.3, T0 + 5) == "floor"
    assert c("road deaths fell by thirty percent", T0 + 4.6, T0 + 6) == "speech"      # after the tail
    assert c("the ONS says it is 4.1 percent", T0 + 5, T0 + 6) == "echo"              # post-hoc overlap still on
    assert c("the ONS says", T0 + 5, T0 + 6) == "echo" and c("ONS says", T0 + 5, T0 + 6) == "speech"   # >= 3 words
    assert c("the ONS says it is 4.1 percent", T0 + 15, T0 + 16) == "speech"         # outside window_s
    e.speaking(True, T0 + 100)                                           # 100 s on: nothing we said is recent
    assert c("an unrelated human sentence here", T0 + 101, T0 + 102) == "echo"      # unknown TTS text: drop all
    assert c("an unrelated human sentence here", T0 + 116, T0 + 117) == "speech"    # lost speaking:false expires
    assert e.speaking_since is None and e.windows[-1] == (T0 + 100, T0 + 115.5)
    e.spoke("Speaker A, that claim is still unanswered.", T0 + 120)
    e.speaking(True, T0 + 120.5)
    assert c("speaker a that claim is still on answered", T0 + 121, T0 + 123) == "echo"
    assert c("cars are not the problem here", T0 + 121, T0 + 123) == "speech"
    assert c("cars are not the problem here", T0 + 136, T0 + 137) == "speech"
    assert e.speaking_since is None                                      # the 15 s cap still applies


def test_echo_guard_after_the_fact_window_counts_from_when_our_tts_stopped():
    """The 10 s after-the-fact window ran from publish(): TTS start-up, the line's audio and STT latency used it
    up, so our own words came back as a debater's speech (into the map, extraction and the floor model)."""
    e = EchoGuard()
    e.spoke("Point of fact: the ONS contradicts that. Details on screen.", T0)
    e.speaking(True, T0 + 3)
    e.speaking(False, T0 + 8)
    c = lambda text, a, b: e.classify(U("A", text, a, b, ts=b + .1), b + .1)          # noqa: E731
    assert c("the ONS contradicts that details on screen", T0 + 8.7, T0 + 10.5) == "echo"
    assert c("Details on screen.", T0 + 10.5, T0 + 11) == "echo"
    assert c("on that point", T0 + 9, T0 + 9.5) == "echo"                             # within 10 s of publish
    assert c("on that point", T0 + 11, T0 + 11.5) == "speech"         # later: 1 content word is too little to tell
    assert c("the ONS contradicts that", T0 + 18, T0 + 18.9) == "speech"              # 10.5 s after it stopped
    e.spoke("For the record, that checks out. Source: the ONS.", T0 + 30)             # D never reported playing it
    assert c("that checks out source the ONS", T0 + 39, T0 + 39.9) == "echo"
    assert c("that checks out source the ONS", T0 + 40, T0 + 41) == "speech"


def test_pause_synth_waits_longer_until_interims_are_seen():
    ps = PauseSynth("auto", gap=1.2)
    ps.on_utterance(U("A", "x", T0, T0 + 3), T0 + 3.3)              # finals only: max(6, 3 + 1.2)
    assert ps.due(T0 + 9.2) == [] and [p.speaker for p in ps.due(T0 + 9.4)] == ["A"]
    ps.on_utterance(U("A", "x", T0 + 10, T0 + 20), T0 + 20.3)       # a 10 s final: the next may be 10 s away
    assert ps.due(T0 + 31.4) == [] and len(ps.due(T0 + 31.6)) == 1
    ps.on_utterance(U("A", "x", T0 + 40, T0 + 100), T0 + 100)       # capped at 15 s
    assert ps.due(T0 + 114.9) == [] and len(ps.due(T0 + 115.1)) == 1
    ps.on_utterance(U("A", "x", T0 + 120, T0 + 121, final=False), T0 + 121)   # first interim: the usual 1.2 s
    ps.on_utterance(U("A", "x", T0 + 120, T0 + 122), T0 + 122)
    assert ps.due(T0 + 123.1) == [] and len(ps.due(T0 + 123.3)) == 1
    ps.on_utterance(U("A", "x", T0 + 124, T0 + 125), T0 + 125)
    ps.on_utterance(U("A", "more", T0 + 125, T0 + 126, final=False), T0 + 126)  # speech resumes: pushed out
    assert ps.due(T0 + 126.5) == [] and len(ps.due(T0 + 127.3)) == 1
    ps.on_pause(Pause(speaker="B", src="brain:pause_synth"))
    assert ps.active
    ps.on_pause(Pause(speaker="B", src="ears"))                     # a real VAD Pause: synthesis off
    ps.on_utterance(U("A", "x", T0 + 130, T0 + 131), T0 + 131)
    assert not ps.active and ps.due(T0 + 200) == []
    ps.reset()
    assert ps.active and not ps.interims_seen


def test_bleed_guard_drops_the_other_mic_hearing_a_speaker():
    b = BleedGuard()
    assert not b.is_bleed(U("A", "Air pollution causes 40,000 early deaths a year", T0, T0 + 4))
    assert b.is_bleed(U("B", "air pollution causes 40,000 early deaths a year", T0 + .3, T0 + 4.2))
    assert not b.is_bleed(U("B", "That figure is a modelled estimate, not a count", T0 + 1, T0 + 4))
    assert not b.is_bleed(U("A", "Air pollution causes 40,000 early deaths a year", T0 + 5, T0 + 9))   # same speaker
    assert not b.is_bleed(U("B", "air pollution causes 40,000 early deaths a year", T0 + 20, T0 + 24))  # no overlap
    assert not b.is_bleed(U("B", "yes yes", T0 + 5, T0 + 6))                                         # too short
    b.reset()
    assert b.recent == []


def test_late_interims_after_their_final_are_ignored():
    g = LateInterims(keep=2)
    assert not g.is_late(U("A", "air", T0, T0 + 1, final=False, uid="u1"))
    assert not g.is_late(U("A", "air pollution kills", T0, T0 + 3, uid="u1"))
    assert g.is_late(U("A", "air", T0, T0 + 1, final=False, uid="u1"))
    assert not g.is_late(U("A", "air pollution kills people", T0, T0 + 3, uid="u1"))   # a re-sent final is fine
    for i in (2, 3):
        g.is_late(U("A", "x", T0, T0 + 1, uid=f"u{i}"))
    assert not g.is_late(U("A", "air", T0, T0 + 1, final=False, uid="u1"))             # bounded memory
    g.reset()
    assert not g.is_late(U("A", "x", T0, T0 + 1, final=False, uid="u3"))


# ----------------------------------------------------------------------------- the wiring, on a real Bus
class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Brain:
    """Real Bus + real brain/run.py. pub() publishes, step(dt) moves the clock and lets the brain run."""

    def __init__(self, clock: Clock):
        self.clock, self.bus, self.evs, self.task = clock, Bus(), [], None
        self.bus.subscribe("rec", where=lambda ev: self.evs.append(ev), maxsize=1)   # records synchronously

    def now(self) -> float:
        return self.clock.t

    async def start(self) -> None:
        n = len(self.of("DirectorStatus"))
        self.task = asyncio.create_task(brain_run.run(self.bus))
        for _ in range(400):                       # ready once its ticker publishes the first status
            await asyncio.sleep(.005)
            if len(self.of("DirectorStatus")) > n:
                return
        raise AssertionError("brain did not start")

    async def crash(self) -> None:                 # supervise() restarts run(bus) on the same bus
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.start()

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def pub(self, ev, src: str = "test"):
        return self.bus.publish(ev, src=src)

    async def step(self, dt: float = 0.0) -> None:
        for _ in range(5):
            await asyncio.sleep(0)                 # the brain handles what was just published, at the current time
        self.clock.t += dt
        await asyncio.sleep(.03)                   # then ticks a few times at the new time (TICK_S is shrunk)

    def of(self, name: str) -> list:
        return [e for e in self.evs if e.event == name]

    def said(self) -> list:
        return [(i.claim_id, i.kind) for i in self.of("Interjection")]

    def session(self) -> None:
        self.pub(SessionStart(motion="This house would ban cars from city centres",
                              speakers={"A": {"side": "prop"}, "B": {"side": "opp"}}))

    def claim(self, cid: str, spk: str, t_end: float) -> None:      # a swapped-in extractor
        self.pub(Claim(id=cid, speaker=spk, text=f"claim {cid} about 40 percent", type="statistic", checkable=True,
                       importance=1.0, topic="road traffic", t_end=t_end), src="ext")

    def verdict(self, cid: str) -> None:
        self.pub(Verdict(claim_id=cid, label="contradicted", confidence=.95, evidence=EVID,
                         short_fact="it is 4.1 percent"), src="ext")


@pytest.fixture
async def brain(monkeypatch, caplog):
    clock = Clock(time.time())
    monkeypatch.setattr(time, "time", clock)
    monkeypatch.setattr(config, "LLM_PROVIDER", "heuristic")         # offline, whatever the shell exports
    monkeypatch.setattr(config, "FLAGS", {"director": True})
    monkeypatch.setattr(config, "PAUSE_SYNTH", "auto")
    monkeypatch.setattr(config, "DIRECTOR_AGGR", 3)
    monkeypatch.setattr(brain_run, "TICK_S", .005)
    caplog.set_level(logging.WARNING)
    b = Brain(clock)
    yield b
    await b.stop()
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("variant", ["lost_false", "string_false"])
async def test_tts_state_never_leaves_the_brain_deaf(brain, variant):
    """Review #1: a lost TTSState{speaking:false}, or agent_speaking "false" as a string, made every later
    utterance 'echo': the floor model stopped hearing B and the referee spoke over them."""
    h = brain
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    h.claim("a1", "A", t - .2)
    h.claim("a2", "A", t - .2)
    h.verdict("a1")
    h.pub(Pause(speaker="A", t=t))
    await h.step(.5)
    assert h.said() == [("a1", "correction")]
    if variant == "lost_false":
        h.pub(TTSState(speaking=True))                         # the audio tab dies: speaking:false never comes
        await h.step(16)
    else:
        raw = lambda v: Control.model_construct(flag="agent_speaking", value=v)   # noqa: E731 - unvalidated strings
        h.pub(raw("true"))
        await h.step(3)
        h.pub(raw("false"))                                    # bool("false") is True: the guard must parse it
        await h.step(1)
    h.pub(Control(flag="force_next"))                          # skip cooldowns: only the floor decides
    a = h.now()
    words = "Cars are not the main source of pollution in our city centres at all".split()
    for k in (1, 2, 3):
        await h.step(1)
        h.pub(U("B", " ".join(words[:3 * k]), a, h.now(), final=False, uid="u-B-1"))
    h.pub(U("B", "Cars are not the main source of pollution.", a, a + 3, uid="u-B-1"))
    h.pub(Pause(speaker="B", t=a + 3.5))                       # VAD: a breath, then B goes on
    for k in (4, 5, 6, 7):
        await h.step(1)
        h.pub(U("B", " ".join(words[:2 * (k - 3)]), a + 3.8, h.now(), final=False, uid="u-B-2"))
        if k == 5:
            h.verdict("a2")                                    # C's slow verdict lands while B is talking
    await h.step(.3)
    assert h.said() == [("a1", "correction")]                  # never over B
    h.pub(U("B", "Diesel buses produce more nitrogen dioxide than cars.", a + 3.8, h.now() - .1, uid="u-B-2"))
    await h.step(.6)
    assert h.said() == [("a1", "correction")]
    h.pub(Pause(speaker="B", t=h.now()))
    await h.step()
    assert h.said() == [("a1", "correction"), ("a2", "correction")]


@pytest.mark.parametrize("vad", [False, True])
async def test_finals_only_ears_never_talk_over_a_monologue(brain, vad):
    """Review #2: with no interims, a synthetic Pause 1.2 s after each final (or the 2.5 s floor timeout with
    VAD on) opened the floor mid-monologue."""
    h = brain
    await h.start()
    h.session()
    if vad:                                                    # a real VAD Pause switches pause synthesis off
        t = h.now()
        h.pub(U("B", "Over to you then.", t - 2, t - .5))
        h.pub(Pause(speaker="B", t=t))
        await h.step(.5)
    for i in range(7):                                         # 21 s of non-stop speech, a final every 3 s
        await h.step(3)
        h.pub(U("A", f"Sentence number {i} of a long speech about road traffic.", h.now() - 3.3, h.now() - .3,
                uid=f"u-A-{i}"))
        if i == 0:
            h.claim("a1", "A", h.now() - .3)
            h.verdict("a1")
    last = h.now()
    await h.step(.3)
    assert h.said() == [] and not [p for p in h.of("Pause") if p.src.startswith("brain")]
    if vad:
        h.pub(Pause(speaker="A", t=last + .6))
        await h.step(.4)
        assert h.said() == [("a1", "correction")]
    else:
        await h.step(5.3)                                      # 5.6 s after the last final: still nothing
        assert h.said() == []
        await h.step(.7)
        assert h.said() == [("a1", "correction")] and h.of("Interjection")[0].ts >= last + 6


async def test_stream_relative_times_still_interject(brain):
    """Review #3 / flexibility #5: STT stream-relative t_start/t_end/t made every claim 'stale'."""
    h = brain
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", .4, 2.8, uid="u-A-1", ts=0))
    await h.step(.4)
    h.pub(Claim(id="x1", speaker="A", text="Air pollution causes 40,000 early deaths a year.", type="statistic",
                checkable=True, importance=1.0, t_end=2.8), src="ext")
    h.verdict("x1")
    h.pub(Pause(speaker="A", t=3.6))
    await h.step(.3)
    assert ("x1", "correction") in h.said()
    assert not [d for d in h.of("DirectorDecision") if d.reason == "stale_ttl"]
    own = [c for c in h.of("Claim") if c.src == "brain"]
    assert own and all(abs(c.t_end - t) < 5 for c in own)     # the brain's own claims are on the server clock


async def test_crash_restart_never_respeaks_and_keeps_vetoes_cooldown_and_aggr(brain):
    """Review #4 / demo-robustness #7: restore replays our Interjections and the operator's Controls."""
    h = brain
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    for cid in ("a1", "a2", "a3"):
        h.claim(cid, "A", t - .2)
    h.verdict("a1")
    h.pub(Pause(speaker="A", t=t))
    await h.step(.3)
    assert h.said() == [("a1", "correction")]
    h.pub(TTSState(speaking=True))
    await h.step(2)
    h.pub(TTSState(speaking=False))
    await h.step(1)
    h.verdict("a2")
    h.pub(Control(flag="veto", value="a2"))
    h.pub(Control(flag="aggr", value=4))
    h.verdict("a3")
    await h.step(.3)
    await h.crash()
    n = len(h.of("DirectorDecision"))
    t2 = h.now()
    h.pub(U("B", "Well, okay then, fine.", t2 - .5, t2, uid="u-B-1"))   # after the (restored) TTS echo window
    h.pub(Pause(speaker="B", t=t2 + .2))
    await h.step(1.2)
    assert h.said() == [("a1", "correction")]                  # not re-spoken, a2 still vetoed, a3 in cooldown
    assert ("correction:a3", "held", "cooldown") in [(d.key, d.action, d.reason) for d in h.of("DirectorDecision")[n:]]
    st = h.of("DirectorStatus")[-1]
    assert st.aggr == 4 and st.cooldown_left_s > 0 and st.pressure.get("A") == 1
    h.pub(Control(flag="force_next"))                          # the floor is open: force skips the cooldown only
    await h.step(.1)
    assert h.said() == [("a1", "correction"), ("a3", "correction")]


async def test_late_interim_after_its_final_is_not_new_speech(brain):
    """Review #7 (brain side): an out-of-order interim must not push the synthetic Pause out."""
    h = brain
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution", t - 2, t - 1, final=False, uid="u-A-1"))
    await h.step(.3)
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year.", t - 2, t + .2, uid="u-A-1"))
    await h.step(1.0)
    h.pub(U("A", "Air pollution causes", t - 2, t - .5, final=False, uid="u-A-1"))   # the late callback
    await h.step(.3)                                           # 1.3 s after the final
    assert [p.speaker for p in h.of("Pause") if p.src.startswith("brain")] == ["A"]


async def test_barge_in_over_our_tts_is_heard_and_closes_the_floor(brain):
    """Barge-in: EchoGuard dropped every utterance starting while our TTS spoke, so a human talking over the
    referee was invisible: the floor looked open (a Pause from A let a2 fire over B) and B's claim was lost."""
    h = brain
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    h.claim("a1", "A", t - .2)
    h.claim("a2", "A", t - .2)
    h.verdict("a1")
    h.pub(Pause(speaker="A", t=t))
    await h.step(.5)
    assert h.said() == [("a1", "correction")]
    line = h.of("Interjection")[0].text
    await h.step(1)                                            # A's ears were still running the first unit
    own_a = [c.id for c in h.of("Claim") if c.src == "brain" and c.speaker == "A"]
    t1 = h.now()
    h.pub(TTSState(speaking=True))
    await h.step(.5)
    garbled = " ".join(w for i, w in enumerate(line.lower().replace(".", "").replace(",", "").split()) if i % 5 != 2)
    h.pub(U("A", garbled, t1 + .2, h.now(), uid="u-A-echo"))  # A's mic hears the referee
    words = "No road deaths in London fell by 30 percent last year and that is the real story".split()
    h.pub(U("B", "No road", t1 + 1, t1 + 1.4, final=False, uid="u-B-1"))   # B talks over the referee
    await h.step(1)
    h.pub(TTSState(speaking=False))                            # D cuts the TTS
    h.pub(Control(flag="force_next"))                          # skip cooldowns: only the floor decides
    h.verdict("a2")
    for k in range(2, 6):
        await h.step(1)
        h.pub(U("B", " ".join(words[:3 * k]), t1 + 1, h.now(), final=False, uid="u-B-1"))
        if k == 3:
            h.pub(Pause(speaker="A", t=h.now()))               # A's VAD: A is quiet, but B is not
    await h.step(.3)
    assert h.said() == [("a1", "correction")]                  # never over B
    h.pub(U("B", "No, road deaths in London fell by 30 percent last year.", t1 + 1, h.now(), uid="u-B-1"))
    await h.step(.5)
    assert h.said() == [("a1", "correction")]
    h.pub(Pause(speaker="B", t=h.now()))
    await h.step(.3)
    assert h.said() == [("a1", "correction"), ("a2", "correction")]
    own = [c for c in h.of("Claim") if c.src == "brain"]
    assert any(c.speaker == "B" and "30 percent" in c.text for c in own)          # B's barge-in was extracted
    assert [c.id for c in own if c.speaker == "A"] == own_a                       # the echo never was


async def test_a_failing_tick_step_never_stops_the_heartbeat_or_the_map(brain, monkeypatch, caplog):
    """One try wrapped every ticker step: a step that kept raising (e.g. the Director's) stopped the DirectorStatus
    heartbeat, the map snapshots and the brain ModuleStatus for good."""
    h = brain
    boom, real = {"on": True}, brain_run.Director.tick

    def tick(self, now):
        if boom["on"]:
            raise RuntimeError("boom")
        return real(self, now)

    monkeypatch.setattr(brain_run.Director, "tick", tick)
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    n = len(h.of("DirectorStatus"))
    for _ in range(3):
        await h.step(1.1)
    assert len(h.of("DirectorStatus")) >= n + 2 and h.of("ArgMapSnapshot")[-1].turns
    boom["on"] = False
    await h.step()
    assert any("boom" in (r.exc_text or "") or r.exc_info for r in caplog.records)
    caplog.clear()


async def test_snapshot_hz_zero_turns_snapshots_off(brain, monkeypatch):
    """1.0 / ARGMAP_SNAPSHOT_HZ raised ZeroDivisionError on every tick once the map changed."""
    monkeypatch.setattr(config, "ARGMAP_SNAPSHOT_HZ", 0.0)
    h = brain
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    n = len(h.of("DirectorStatus"))
    for _ in range(3):
        await h.step(1.1)
    assert len(h.of("DirectorStatus")) >= n + 2 and not h.of("ArgMapSnapshot")


async def test_a_director_error_on_an_utterance_never_drops_it_from_the_map_or_extraction(brain, monkeypatch, caplog):
    """The Director ran first in the Utterance branch: when it raised, the utterance never reached the argument
    map or claim extraction."""
    h = brain
    real = brain_run.Director.handle

    def handle(self, ev, now):
        if getattr(ev, "event", "") == "Utterance":
            raise RuntimeError("boom")
        return real(self, ev, now)

    monkeypatch.setattr(brain_run.Director, "handle", handle)
    await h.start()
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    await h.step(.5)
    await h.step(.5)
    assert [c for c in h.of("Claim") if c.src == "brain" and c.speaker == "A"]
    assert [x["speaker"] for x in h.of("ArgMapSnapshot")[-1].turns] == ["A"]
    caplog.clear()


async def test_warmup_task_is_cancelled_with_the_brain(brain, monkeypatch):
    """asyncio.create_task(extractor.warmup(...)) was fire-and-forget: never cancelled on shutdown or on the next
    SessionStart (back-to-back sessions stacked warmup LLM calls against old extractors)."""
    from app.brain import llm as llm_mod

    class HangLLM:
        name = "hang"

        async def complete_json(self, *a, **kw):
            await asyncio.Event().wait()

    monkeypatch.setattr(llm_mod, "make_llm", HangLLM)
    h = brain
    await h.start()
    warm = lambda: [t for t in asyncio.all_tasks()                                  # noqa: E731
                    if getattr(t.get_coro(), "__qualname__", "") == "ClaimExtractor.warmup" and not t.done()]
    h.session()
    await h.step(.1)
    assert len(warm()) == 1
    h.session()
    await h.step(.1)
    assert len(warm()) == 1                                    # the old session's warmup was cancelled
    await h.stop()
    for _ in range(3):
        await asyncio.sleep(0)
    assert warm() == []


async def test_crash_restore_applies_the_echo_and_bleed_guards(brain, monkeypatch):
    """restore_from_history fed every final to the argument map: after a restart our own echoed lines and the
    other mic's bleed copies became debate turns."""
    from app.brain.argmap import ArgMap
    seen, real = [], ArgMap.on_utterance
    monkeypatch.setattr(ArgMap, "on_utterance", lambda self, u: (seen.append(u.id), real(self, u))[1])
    h = brain
    h.session()
    t = h.now()
    h.pub(U("A", "Air pollution causes 40,000 early deaths a year in the UK.", t - 4, t - .2, uid="u-A-1"))
    h.pub(U("B", "air pollution causes 40,000 early deaths a year in the UK", t - 3.8, t - .1, uid="u-B-bleed"))
    h.pub(Interjection(id="i-1", text="Point of fact: the ONS says it is 4.1 percent.", kind="correction",
                       claim_id="a1", target="A"), src="brain")
    h.clock.t += 1
    h.pub(TTSState(speaking=True))
    h.clock.t += 3
    h.pub(TTSState(speaking=False))
    h.clock.t += 1
    t = h.now()
    h.pub(U("A", "the ONS says it is 4.1 percent", t - 1.5, t - .5, uid="u-A-echo"))
    h.pub(U("B", "Diesel buses produce more nitrogen dioxide than cars do.", t - .4, t, uid="u-B-1"))
    await h.start()
    assert seen == ["u-A-1", "u-B-1"]


