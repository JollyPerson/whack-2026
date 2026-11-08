"""B's brain, wired onto the bus. Owner: B.

Consumes: SessionStart, Utterance, Pause, Claim, Verdict, Control, TTSState
Produces: Claim, ArgMapSnapshot, Interjection, DirectorDecision, DirectorStatus, ModuleStatus(brain stats),
          Pause (synthetic, only while A's VAD isn't sending Pauses; src="brain:pause_synth")

Flexible on purpose:
- Claims published by someone else (src not "brain") are folded into the argument map too, so claim
  extraction can be swapped out without touching the Director.
- Missing timestamps (t_start/t_end/t == 0) fall back to the envelope ts; stream-relative or other-clock
  times (far from ts) are rebased onto ts, keeping the utterance's duration.
- Pause synthesis covers for a late/missing VAD; it switches itself off once a real Pause arrives.
- Finals-only ears (no interims seen yet): a final is no proof the speaker stopped, so synthetic Pauses and
  the Director's floor timeout wait longer (>= 6 s) before the floor counts as open.
- Ingress guards: our own TTS (echo: only words matching what we're saying, so a human talking over us still
  counts; expires if TTSState speaking:false never comes), the other speaker's voice leaking into a mic (bleed),
  and interims arriving after their final never count as speech.
- A crash-restart rebuilds the argument map + Director state (incl. lines already spoken, vetoes, mute,
  aggressiveness) from the bus history, silently.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import contextmanager
from typing import Iterable, Iterator

from app import config
from app.brain.argmap import ArgMap
from app.brain.claims import ClaimExtractor
from app.brain.director import Director, truthy
from app.schemas import Claim, Event, Interjection, ModuleStatus, Pause, Utterance

log = logging.getLogger("brain")
TICK_S = 0.25
STATS_EVERY_S = 10.0
TOK = re.compile(r"[a-z0-9]+")
SKEW_S = 120.0          # a producer time this far from the envelope ts is stream-relative or another clock
STOP = frozenset("a an the of to in on at is are was were be been it its s t that this and or for by as with i you "
                 "we they he she so but do does".split())


def _toks(text: str) -> set:
    return set(TOK.findall(text.lower()))


class PauseSynth:
    """Synthesise Pause(speaker) when a speaker's final utterance isn't followed by more speech
    within `gap` seconds. Mode auto: active until the first real Pause of the session.
    Until an interim has been seen (finals-only ears), the next final may be a whole sentence/chunk away:
    wait max(finals_gap, the final's length + gap), capped at max_gap, so we never talk over a monologue."""

    def __init__(self, mode: str = "auto", gap: float = 1.2, finals_gap: float = 6.0, max_gap: float = 15.0):
        self.mode, self.gap, self.finals_gap, self.max_gap = mode, gap, finals_gap, max_gap
        self.real_seen = self.interims_seen = False
        self.deadline: dict[str, float] = {}

    def reset(self) -> None:
        self.real_seen, self.interims_seen, self.deadline = False, False, {}

    @property
    def active(self) -> bool:
        return self.mode == "on" or (self.mode == "auto" and not self.real_seen)

    def on_utterance(self, u: Utterance, now: float) -> None:
        self.interims_seen |= not u.final
        if not self.active:
            return
        if u.final or u.speaker in self.deadline:     # final arms it; more speech pushes it out
            gap = self.gap if self.interims_seen else \
                min(max(self.finals_gap, u.t_end - u.t_start + self.gap), self.max_gap)
            self.deadline[u.speaker] = now + gap

    def on_pause(self, p: Pause) -> None:
        if not p.src.startswith("brain"):
            self.real_seen = True
        self.deadline.pop(p.speaker, None)

    def due(self, now: float) -> list[Pause]:
        if not self.active:
            self.deadline.clear()
            return []
        out = [Pause(speaker=s, t=now) for s, d in self.deadline.items() if now >= d]
        for p in out:
            self.deadline.pop(p.speaker, None)
        return out


class EchoGuard:
    """Keep the moderator's own voice (picked up by a mic) out of the brain. A speaking:true whose
    speaking:false never comes (audio tab refreshed/closed, onend lost) expires after cap_s.
    While we speak (and tail_s after) an utterance is echo only if its words plausibly come from what we said
    (live_overlap of its content words, below the post-hoc `overlap`: a speaker through a mic garbles STT);
    anything else is a human talking over us (barge-in) and must reach the Director and extraction."""

    def __init__(self, tail_s: float = 0.5, overlap: float = 0.6, window_s: float = 10.0, cap_s: float = 15.0,
                 live_overlap: float = 0.4):
        self.tail_s, self.overlap, self.window_s, self.cap_s = tail_s, overlap, window_s, cap_s
        self.live_overlap = live_overlap
        self.speaking_since: float | None = None
        self.windows: list[tuple[float, float]] = []
        self.said: list[tuple[float, set]] = []

    def reset(self) -> None:
        self.speaking_since, self.windows, self.said = None, [], []

    def speaking(self, on: bool, now: float) -> None:
        if on and self.speaking_since is None:
            self.speaking_since = now
        elif not on and self.speaking_since is not None:
            self.windows = (self.windows + [(self.speaking_since, now + self.tail_s)])[-20:]
            self.speaking_since = None

    def spoke(self, text: str, now: float) -> None:
        self.said = [(t, s) for t, s in self.said if now - t < 60] + [(now, _toks(text))]

    def is_echo(self, u: Utterance, now: float) -> bool:
        return self.classify(u, now) == "echo"

    def _stopped(self, ts: float, now: float) -> float:
        """When our TTS of the line published at `ts` stopped: the end of the first speaking window that began
        after it, `now` while it is still playing, else `ts` (D never reported playing it)."""
        end = next((b for a, b in self.windows if a >= ts), None)
        if end is None and self.speaking_since is not None and self.speaking_since >= ts:
            end = now
        return ts if end is None else end

    def classify(self, u: Utterance, now: float) -> str:
        """Returns "echo" (drop), "floor" (Director/pauses only, never extracted) or "speech"."""
        if self.speaking_since is not None and now - self.speaking_since > self.cap_s:
            self.speaking(False, self.speaking_since + self.cap_s)   # lost speaking:false
        t, words = u.t_start or u.ts, _toks(u.text)
        if (self.speaking_since is not None and t >= self.speaking_since) or any(a <= t <= b for a, b in self.windows):
            said = [s for ts, s in self.said if 0 <= now - ts < 60]
            if not said or not words:
                return "echo"           # no idea what the TTS is saying (or nothing heard): old drop-all
            content = words - STOP      # function words are shared by any two sentences
            if len(content) >= 2:
                hit = max(len(content & s) / len(content) for s in said)
                return "echo" if hit >= self.live_overlap else "speech"
            # Under 2 content words ("hold on", or the first interim of our own echo): echo if every word is ours,
            # else a human claiming the floor. Too short to be a claim: floor tracking only, no extraction/argmap.
            return "echo" if any(words <= s for s in said) else "floor"
        if len(words) >= 3:     # after the fact: TTS start-up, the line's audio and STT latency all come first
            late_ok = len(words - STOP) >= 2   # past publish + window_s: "it is the" is too little to call echo
            for ts, said in self.said:
                recent = now - ts <= self.window_s or (late_ok and now - self._stopped(ts, now) <= self.window_s)
                if recent and len(words & said) / len(words) >= self.overlap:
                    return "echo"
        return "speech"


class BleedGuard:
    """Two mics in one room: speaker B's mic also transcribes speaker A. Drop a final utterance whose
    words mostly match another speaker's utterance overlapping in time (keeps the first one heard)."""

    def __init__(self, overlap: float = 0.7, slack_s: float = 1.5):
        self.overlap, self.slack_s = overlap, slack_s
        self.recent: list[tuple[str, float, float, set]] = []   # (speaker, t_start, t_end, tokens)

    def reset(self) -> None:
        self.recent = []

    def is_bleed(self, u: Utterance) -> bool:
        words = _toks(u.text)
        self.recent = [r for r in self.recent if u.ts - r[2] < 30][-40:]
        for spk, a, b, toks in self.recent:
            if spk == u.speaker or len(words) < 4:
                continue
            if u.t_start <= b + self.slack_s and a <= u.t_end + self.slack_s:
                if len(words & toks) / len(words) >= self.overlap:
                    return True
        if u.final:
            self.recent.append((u.speaker, u.t_start, u.t_end, words))
        return False


class LateInterims:
    """An interim that arrives after its final (out-of-order STT callbacks) is stale: never new speech."""

    def __init__(self, keep: int = 512):
        self.keep, self.finals = keep, {}

    def reset(self) -> None:
        self.finals = {}

    def is_late(self, u: Utterance) -> bool:
        if u.final:
            self.finals[u.id] = None
            if len(self.finals) > self.keep:
                self.finals.pop(next(iter(self.finals)))
            return False
        return u.id in self.finals


def _fill_times(ev: Event, now: float | None = None) -> Event:
    """Producer times onto the server clock. 0 means 'unknown' in the contract: fall back to the envelope ts.
    A time far from ts (STT stream-relative seconds, another machine's clock) is rebased onto ts, keeping
    the utterance's duration. `now` (arrival) also replaces an envelope ts the producer set on another clock."""
    ts, upd = ev.ts, {}
    if now is not None and abs(ts - now) > SKEW_S:
        ts = upd["ts"] = now
    ok = lambda t: bool(t) and abs(t - ts) <= SKEW_S          # noqa: E731
    if isinstance(ev, Utterance) and not (ok(ev.t_start) and ok(ev.t_end)):
        a, b = ev.t_start, ev.t_end
        if ok(b):
            a = b                                   # start unknown: zero length at the known end
        elif ok(a):
            b = ts                                  # end unknown: still talking when it arrived
        else:                                       # both unknown/other clock: end at ts, keep the duration
            d = b - a if a and b and 0 <= b - a <= SKEW_S else 0.0
            a, b = ts - d, ts
        upd.update(t_start=a, t_end=b)
    elif isinstance(ev, Pause) and not ok(ev.t):
        upd["t"] = ts
    elif isinstance(ev, Claim) and ev.t_end and not ok(ev.t_end):
        upd["t_end"] = ts
    return ev.model_copy(update=upd) if upd else ev


async def run(bus) -> None:
    from app.brain.llm import make_llm

    amap = ArgMap(k_turns=config.ARGMAP_K_TURNS)
    director = Director(aggr=config.DIRECTOR_AGGR)
    pauses = PauseSynth(config.PAUSE_SYNTH, config.PAUSE_SYNTH_GAP_S, float(getattr(config, "FINALS_ONLY_WAIT_S", 6.0)))
    echo, bleed, late = EchoGuard(), BleedGuard(), LateInterims()
    # Subscribe and snapshot the history in one synchronous step: every event is restored or handled live, once.
    sub = bus.subscribe("brain", {"SessionStart", "Utterance", "Pause", "Claim", "Verdict", "Control", "TTSState"},
                        maxsize=512)
    history = list(bus.history)
    try:
        llm = await asyncio.to_thread(make_llm)        # SDK import (~1 s) off the shared event loop
    except asyncio.CancelledError:
        sub.close()
        raise
    except Exception:
        log.exception("LLM client unavailable; heuristic claim extraction only")
        llm = None

    def publish(ev: Event) -> Event:
        if isinstance(ev, Interjection):
            ev.marks.setdefault("interject", time.time())
            echo.spoke(ev.text, time.time())
        return bus.publish(ev, src="brain")

    def emit(evs: Iterable[Event]) -> None:
        for e in evs:
            publish(e)

    extractor = ClaimExtractor(amap, publish, llm)
    inflight: set[asyncio.Task] = set()               # extraction + warmup: cancelled on SessionStart and on exit
    log.info("brain up: llm=%s", getattr(llm, "name", None) or "heuristic")

    def drain(now: float) -> None:
        for n in amap.drain_notices():
            emit(director.notice(n, now))

    async def _extract(u: Utterance) -> None:
        try:
            await extractor.extract(u)
            drain(time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("claim extraction failed for %s", u.id)

    def spawn(coro) -> None:
        t = asyncio.create_task(coro)
        inflight.add(t)                                # also keeps it from being garbage-collected mid-flight
        t.add_done_callback(inflight.discard)

    def start_extract(units: Iterable[Utterance]) -> None:
        for unit in units:
            spawn(_extract(unit))

    def on_tts(ev: Event, now: float) -> None:
        if ev.event == "TTSState":
            echo.speaking(truthy(ev.speaking), now)
        elif ev.flag == "agent_speaking":
            echo.speaking(truthy(ev.value), now)

    def restore_from_history(history: list[Event]) -> None:
        """After a crash-restart: rebuild state silently (no re-publishing, no re-extraction). Our own spoken
        Interjections and the operator's Controls are replayed too: nothing is said twice, the cooldown still
        counts, vetoes/mute/aggr stick. The Director never fires here: no speech is replayed into it."""
        n = 0
        for ev in history:
            try:
                ev = _fill_times(ev)
                if ev.event == "SessionStart":
                    amap.reset(ev)
                    director.reset(ev)
                elif ev.event == "Utterance" and ev.final:   # the live ingress guards, in the live order
                    heard = echo.classify(ev, ev.ts)         # (bleed also remembers a floor-only final)
                    if heard != "echo" and not bleed.is_bleed(ev) and heard == "speech":
                        amap.on_utterance(ev)
                elif ev.event == "Claim":
                    amap.add_claim(ev)
                    director.handle(ev, ev.ts)
                elif ev.event == "Verdict":
                    amap.on_verdict(ev)
                    director.handle(ev, ev.ts)
                elif ev.event == "Interjection" and ev.src.startswith("brain"):
                    director.restore_fired(ev)
                    echo.spoke(ev.text, ev.ts)
                elif ev.event in ("Control", "TTSState"):
                    on_tts(ev, ev.ts)
                    director.handle(ev, ev.ts)
                else:
                    continue
                n += 1
            except Exception:
                log.exception("restore skipped %s", ev.event)
        amap.drain_notices()
        if n:
            log.info("brain restored %d events from history", n)

    @contextmanager
    def tick_step(what: str) -> Iterator[None]:
        """Each ticker step on its own: one that keeps failing never stops the heartbeat or the others."""
        try:
            yield
        except Exception:
            log.exception("brain tick failed: %s", what)

    async def ticker() -> None:
        last_ver, last_snap, last_status, last_stats = -1, 0.0, 0.0, time.time()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.time()
            with tick_step("pauses"):
                for p in pauses.due(now):
                    bus.publish(p, src="brain:pause_synth")   # comes back through the subscription
            with tick_step("flush"):
                start_extract(extractor.flush_due(now))
            with tick_step("director"):
                emit(director.tick(now))
            with tick_step("snapshot"):
                hz = config.ARGMAP_SNAPSHOT_HZ                # <= 0: snapshots off
                if hz > 0 and amap.version != last_ver and now - last_snap >= 1.0 / hz:
                    last_ver, last_snap = amap.version, now
                    publish(amap.snapshot())
            with tick_step("status"):
                if now - last_status >= 1.0:
                    last_status = now
                    publish(director.status(now))
            with tick_step("stats"):
                if now - last_stats >= STATS_EVERY_S:
                    last_stats = now
                    s = getattr(extractor, "stats", {}) or {}
                    calls, fb = s.get("calls", 0), s.get("fallbacks", 0)
                    failed = fb - (s.get("by_cause") or {}).get("no_llm", 0)   # heuristic-by-choice isn't a failure
                    degraded = calls >= 5 and failed / max(calls, 1) > 0.3
                    publish(ModuleStatus(module="brain", state="degraded" if degraded else "up",
                                         detail=f"llm={s.get('llm', '?')} p50={s.get('p50_ms') or '-'}ms "
                                                f"fallback {fb}/{calls} {s.get('by_cause', {})}"))

    restore_from_history(history)
    tick_task = asyncio.create_task(ticker())
    try:
        async for ev in sub:
            now = time.time()
            try:
                ev = _fill_times(ev, now)
                name = ev.event
                if name == "SessionStart":
                    for t in list(inflight):
                        t.cancel()
                    for m in (amap, director):
                        m.reset(ev)
                    for m in (extractor, pauses, echo, bleed, late):
                        m.reset()
                    spawn(extractor.warmup(ev.motion))
                elif name == "Utterance":
                    if late.is_late(ev) or (heard := echo.classify(ev, now)) == "echo" or \
                            (ev.final and bleed.is_bleed(ev)):
                        continue
                    pauses.on_utterance(ev, now)
                    if heard != "floor":                       # a 1-2 word barge-in over our TTS: floor only
                        if ev.final:
                            amap.on_utterance(ev)
                        start_extract(extractor.assemble(ev, now))     # an interim only keeps a fragment open
                    emit(director.handle(ev, now))             # last: a Director error never costs map/extraction
                elif name == "Pause":
                    pauses.on_pause(ev)
                    start_extract(extractor.on_pause(ev.speaker))
                    amap.on_pause(ev)
                    emit(director.handle(ev, now))
                elif name == "Claim":
                    if not ev.src.startswith("brain"):         # claims from a swapped-in extractor
                        amap.add_claim(ev)
                    emit(director.handle(ev, now))
                elif name == "Verdict":
                    amap.on_verdict(ev)
                    emit(director.handle(ev, now))
                elif name in ("Control", "TTSState"):
                    on_tts(ev, now)
                    emit(director.handle(ev, now))
                drain(now)
            except Exception:
                log.exception("brain failed on %s", ev.event)
    finally:
        tick_task.cancel()
        for t in list(inflight):
            t.cancel()
        sub.close()
