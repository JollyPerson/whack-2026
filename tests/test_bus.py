"""Bus: publish envelope, filtering, overflow, history compaction, JSONL log, replay, thread-safety."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from app import bus as busmod
from app.bus import Bus, load_jsonl, rebase, replay
from app.schemas import (ArgMapSnapshot, Claim, DirectorDecision, DirectorStatus, Event, Interjection, ModuleStatus,
                         Pause, ResearchProgress, SessionStart, Utterance, Verdict, parse_event)

EV = [{"url": "https://x.org", "title": "t", "quote": "q"}]


def drain(sub) -> list[Event]:
    out = []
    while not sub.q.empty():
        out.append(sub.q.get_nowait())
    return out


def test_publish_assigns_seq_ts_src():
    b = Bus()
    t = time.time()
    e1 = b.publish(Pause(speaker="A"), src="ears")
    e2 = b.publish(Pause(speaker="B", src="mine", ts=123.0), src="ears")
    assert (e1.seq, e2.seq, b.last_seq) == (1, 2, 2)
    assert t - 1 <= e1.ts <= time.time() + 1
    assert e2.ts == 123.0                                    # producer timestamps are kept
    assert (e1.src, e2.src) == ("ears", "mine")              # src arg never overrides an existing src


def test_publish_accepts_dict_json_and_bytes():
    b = Bus()
    a = b.publish({"event": "Pause", "speaker": "A"})
    c = b.publish('{"event": "Claim", "id": "c1", "speaker": "B", "text": "x"}', src="http")
    d = b.publish(b'{"event": "Whatever", "x": 1}')
    assert isinstance(a, Pause) and isinstance(c, Claim) and c.src == "http"
    assert type(d) is Event and d.x == 1 and d.seq == 3


def test_subscription_filters():
    b = Bus()
    every, claims = b.subscribe("all"), b.subscribe("claims", {"Claim"})
    only_a = b.subscribe("a", {"Utterance"}, where=lambda e: e.speaker == "A")
    broken = b.subscribe("broken", where=lambda e: 1 / 0)    # a broken filter must not break publish()
    b.publish(Utterance(speaker="A", text="x"))
    b.publish(Utterance(speaker="B", text="y"))
    b.publish(Claim(id="c1", speaker="A", text="x"))
    assert [e.event for e in drain(every)] == ["Utterance", "Utterance", "Claim"]
    assert [e.id for e in drain(claims)] == ["c1"]
    assert [e.speaker for e in drain(only_a)] == ["A"]
    assert drain(broken) == []
    only_a.close()
    assert only_a not in b.subs


def test_overflow_drops_oldest_and_counts():
    b = Bus()
    s = b.subscribe("slow", maxsize=3)
    for i in range(5):
        b.publish(Pause(speaker=f"s{i}"))
    assert [e.speaker for e in drain(s)] == ["s2", "s3", "s4"] and s.dropped == 2


async def test_async_iteration_in_order():
    b = Bus()
    s = b.subscribe("it", {"Pause"})
    for i in range(3):
        b.publish(Pause(speaker=str(i)))
    got = []
    async for e in s:
        got.append(e.speaker)
        if len(got) == 3:
            break
    assert got == ["0", "1", "2"]


def test_history_compaction():
    b = Bus()
    b.publish(Utterance(id="u1", speaker="A", text="air pol", final=False))
    b.publish(Pause(speaker="A"))
    b.publish(Utterance(id="u1", speaker="A", text="air pollution kills", final=True))
    b.publish(Claim(id="c1", speaker="A", text="v1"))
    b.publish(Claim(id="c1", speaker="A", text="v2"))
    b.publish(Verdict(claim_id="c1", label="supported", evidence=EV, via="index"))
    b.publish(Verdict(claim_id="c1", label="contradicted", evidence=EV, via="web"))
    b.publish(ArgMapSnapshot(version=1))
    b.publish(ArgMapSnapshot(version=2))
    b.publish(ModuleStatus(module="brain", state="down"))
    b.publish(ModuleStatus(module="brain", state="up"))
    b.publish(ModuleStatus(module="ears"))
    b.publish(DirectorDecision(key="k", action="fired"))
    b.publish(Interjection(id="i-1", text="hi"))
    h = b.history
    assert [e.event for e in h] == ["Utterance", "Pause", "Claim", "Verdict", "ArgMapSnapshot", "ModuleStatus",
                                    "ModuleStatus", "DirectorDecision", "Interjection"]
    assert h[0].final and h[0].text == "air pollution kills"          # same slot, latest revision
    assert h[2].text == "v2" and h[3].via == "web" and h[4].version == 2
    assert [m.module for m in h[5:7]] == ["brain", "ears"] and h[5].state == "up"
    b.publish(SessionStart(motion="new debate"))                      # module dots survive a new session
    assert [e.event for e in b.history] == ["ModuleStatus", "ModuleStatus", "SessionStart"]
    b.publish(ModuleStatus(module="brain", state="degraded"))          # ...and still compact in place
    b.publish(Claim(id="c1", speaker="B", text="fresh"))               # old claim slots are gone after the reset
    assert [e.event for e in b.history] == ["ModuleStatus", "ModuleStatus", "SessionStart", "Claim"]
    assert b.history[0].state == "degraded"


def test_history_cap_keeps_slots_consistent(monkeypatch):
    monkeypatch.setattr(busmod, "HISTORY_MAX", 5)
    b = Bus()
    b.publish(Utterance(id="u1", speaker="A", text="old"))
    for i in range(3):
        b.publish(Pause(speaker=str(i)))
    b.publish(Utterance(id="u2", speaker="A", text="v1"))
    b.publish(Pause(speaker="x"))                                      # 6 > 5: trimmed to 4 in one go, u1 first
    b.publish(Utterance(id="u2", speaker="A", text="v2"))              # must update u2 in place
    assert len(b.history) == 4
    assert [getattr(e, "text", None) for e in b.history if e.event == "Utterance"] == ["v2"]


def test_history_cap_pins_session_start_and_trims_in_chunks(monkeypatch):
    """Review: a long session with id-less interims pushed the SessionStart out of the Snapshot, and every
    publish at the cap rebuilt the whole history."""
    monkeypatch.setattr(busmod, "HISTORY_MAX", 10)
    b = Bus()
    b.publish(ModuleStatus(module="ears"))
    b.publish(SessionStart(motion="ban cars", speakers={"A": {"side": "prop"}}))
    b.publish(Claim(id="c1", speaker="A", text="v1"))
    lens = []
    for i in range(30):                                                # id-less: one row each
        b.publish(Utterance(speaker="A", text=f"w{i}", final=False))
        lens.append(len(b.history))
    drops = [b for a, b in zip(lens, lens[1:]) if b < a]
    assert max(lens) <= 10 and len(drops) >= 5 and set(drops) == {8}   # 11 -> 8 in one go, every 3rd publish
    assert [e.event for e in b.history[:2]] == ["ModuleStatus", "SessionStart"]   # pinned, order kept
    assert b.history[1].motion == "ban cars" and b.history[-1].text == "w29"
    b.publish(ModuleStatus(module="ears", state="down"))               # slots rebuilt: still compacts in place
    assert [e.state for e in b.history if e.event == "ModuleStatus"] == ["down"]


def test_late_interim_never_overwrites_final():
    """Review (brain #7): an out-of-order interim replaced its final in history (refresh + crash-restore)."""
    b = Bus()
    b.publish(Utterance(id="u1", speaker="A", text="Air pollution", final=False))
    b.publish(Utterance(id="u1", speaker="A", text="Air pollution kills 40,000 a year.", final=True))
    late = b.publish(Utterance(id="u1", speaker="A", text="Air pollution kills", final=False))
    assert late.seq == 3                                               # still logged and fanned out
    (u,) = [e for e in b.history if e.event == "Utterance"]
    assert u.final and u.text == "Air pollution kills 40,000 a year."
    b.publish(Utterance(id="u1", speaker="A", text="Air pollution kills 40,000 a year (fixed).", final=True))
    assert [e.text for e in b.history if e.event == "Utterance"] == ["Air pollution kills 40,000 a year (fixed)."]


def test_log_written_before_fanout(tmp_path):
    path = tmp_path / "run.jsonl"
    b = Bus(path)
    seen = []
    b.subscribe("probe", where=lambda e: seen.append((e.seq, path.read_text().count("\n"))) or True)
    for i in range(3):
        b.publish(Pause(speaker=str(i)), src="t")
    assert seen == [(1, 1), (2, 2), (3, 3)]                           # line already on disk at fan-out
    lines = path.read_text().splitlines()
    assert [json.loads(ln)["seq"] for ln in lines] == [1, 2, 3]
    assert lines[0] == b.history[0].model_dump_json()                  # one wire format
    b.close()


def test_load_jsonl_skips_bad_lines(tmp_path, caplog):
    p = tmp_path / "f.jsonl"
    p.write_text("\n".join([
        '{"event": "Pause", "speaker": "A", "ts": 1.0}',
        "this is not json",
        "// a comment",
        "",
        '{"event": "Claim", "id": "c1", "text": "missing speaker"}',
        '{"no_event": true}',
        '{"event": "NewThing", "x": 2, "ts": 2.0}',
    ]))
    evs = load_jsonl(p)
    assert [e.event for e in evs] == ["Pause", "NewThing"] and evs[1].x == 2
    assert caplog.text.count("skipped") == 3


def test_rebase_shifts_known_times_and_marks_but_not_zero():
    e = Claim(id="c1", speaker="A", text="x", ts=10.0, t_end=8.0, marks={"speech_end": 8.0, "claim": 10.0})
    rebase(e, lambda t: t + 100)
    assert (e.ts, e.t_end, e.marks) == (110.0, 108.0, {"speech_end": 108.0, "claim": 110.0})
    u = rebase(Utterance(speaker="A", text="x", ts=5.0), lambda t: t + 100)
    assert (u.ts, u.t_start, u.t_end) == (105.0, 0.0, 0.0)             # 0 = unknown, stays 0


async def test_replay_fast_rebases_and_orders(tmp_path):
    p = tmp_path / "rec.jsonl"
    rows = [  # deliberately out of order; relative seconds like a fixture
        {"event": "Pause", "speaker": "A", "t": 3.0, "ts": 3.0, "src": "ears"},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "one", "t_start": 0.5, "t_end": 1.5, "ts": 2.0},
        {"event": "Claim", "id": "c1", "speaker": "A", "text": "one", "ts": 2.5, "marks": {"speech_end": 1.5}},
        {"event": "Utterance", "id": "u0", "speaker": "B", "text": "zero", "ts": 1.0},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    b = Bus()
    s = b.subscribe("r")
    w0 = time.time()
    await asyncio.wait_for(replay(b, p, speed=1000), 2)
    evs = drain(s)
    assert [e.event + ":" + getattr(e, "id", e.src) for e in evs] == ["Utterance:u0", "Utterance:u1", "Claim:c1",
                                                                      "Pause:replay:ears"]
    assert [e.seq for e in evs] == [1, 2, 3, 4]
    assert all(w0 - 0.5 <= e.ts <= time.time() + 0.5 for e in evs)    # rebased to now
    u1, c1, pause = evs[1:]
    assert u1.t_end - u1.t_start == pytest.approx(1.0 / 1000, abs=1e-6)
    assert c1.marks["speech_end"] == pytest.approx(u1.t_end)
    assert pause.t == pytest.approx(pause.ts) and evs[0].src == "replay"
    assert evs[3].ts - evs[0].ts == pytest.approx(2.0 / 1000, abs=1e-6)


async def test_replay_only_filter(tmp_path):
    p = tmp_path / "rec.jsonl"
    p.write_text('{"event":"Utterance","speaker":"A","text":"x","ts":1}\n{"event":"Claim","id":"c1","speaker":"A","text":"x","ts":2}\n')
    b = Bus()
    s = b.subscribe("r")
    await replay(b, p, speed=1000, only={"Utterance"})
    assert [e.event for e in drain(s)] == ["Utterance"]


async def test_publish_threadsafe_from_thread():
    b = Bus()
    b.bind_loop(asyncio.get_running_loop())
    s = b.subscribe("t", {"Pause"})
    th = threading.Thread(target=b.publish_threadsafe, args=({"event": "Pause", "speaker": "A"}, "stt-callback"))
    th.start()
    th.join()
    ev = await asyncio.wait_for(s.__anext__(), 2)
    assert ev.speaker == "A" and ev.src == "stt-callback" and ev.seq == 1


def test_age():
    b = Bus()
    e = b.publish(Pause(speaker="A", ts=time.time() - 3))
    assert 2.5 < busmod.age(e) < 4
    assert parse_event(e.model_dump_json()).ts == e.ts


def test_gap_offsets():
    assert busmod.gap_offsets([], 3) == []
    assert busmod.gap_offsets([0, 1, 10, 11, 111], 3) == [0, 0, 6, 6, 103]
    assert busmod.gap_offsets([0, 1, 10], 0) == [0, 0, 0]                    # 0 = keep every gap


async def test_replay_last_session_cuts_idle_gaps_and_skips_module_status(tmp_path):
    """Review: a rehearsal log replayed minutes of setup silence and earlier sessions."""
    p = tmp_path / "rehearsal.jsonl"
    rows = [
        {"event": "SessionStart", "motion": "rehearsal", "ts": 1000.0},
        {"event": "Utterance", "id": "u0", "speaker": "A", "text": "old", "ts": 1001.0},
        {"event": "ModuleStatus", "module": "ears", "state": "down", "detail": "finished", "ts": 1002.0},
        {"event": "SessionStart", "motion": "judging", "ts": 1100.0},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "one", "t_start": 1219.0, "t_end": 1220.0, "ts": 1220.0},
        {"event": "Interjection", "id": "i-1", "text": "Point of fact.", "ts": 1221.0, "marks": {"interject": 1221.0}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    b = Bus()
    s = b.subscribe("r")
    t = time.monotonic()
    assert await asyncio.wait_for(replay(b, p, speed=1, max_gap=0.05), 2) == 3
    assert time.monotonic() - t < 1.0                                        # 120 s + 1 s of recorded gaps -> ~0.1 s
    evs = drain(s)
    assert [e.event for e in evs] == ["SessionStart", "Utterance", "Interjection"] and evs[0].motion == "judging"
    u, ij = evs[1], evs[2]
    assert u.t_end - u.t_start == pytest.approx(1.0, abs=0.01)              # times inside an event keep their spacing
    assert ij.ts - u.ts == pytest.approx(0.05, abs=0.02) and ij.marks["interject"] == pytest.approx(ij.ts)
    b2 = Bus()
    s2 = b2.subscribe("r")
    assert await replay(b2, p, speed=1000, last=False, max_gap=0) == 5       # every session, ModuleStatus still skipped
    assert [e.event for e in drain(s2)].count("SessionStart") == 2
    with pytest.raises(FileNotFoundError):
        await replay(Bus(), tmp_path / "missing.jsonl")


# ----------------------------------------------------------------------------- hardening regressions: core
def test_history_cap_keeps_the_latest_singletons(monkeypatch):
    """ArgMapSnapshot/ResearchProgress/DirectorStatus compact into the slot of their FIRST publish, so the cap trimmed
    the freshest state first: a refreshed dashboard lost the research status and the argument map."""
    monkeypatch.setattr(busmod, "HISTORY_MAX", 10)
    b = Bus()
    b.publish(SessionStart(motion="m"))
    b.publish(ResearchProgress(stage="ready", done=20, total=20))
    b.publish(ArgMapSnapshot(version=1))
    b.publish(DirectorStatus())
    for i in range(12):
        b.publish(Pause(speaker=str(i)))
    b.publish(ArgMapSnapshot(version=2))
    h = b.history
    assert {"SessionStart", "ResearchProgress", "DirectorStatus", "ArgMapSnapshot"} <= {e.event for e in h}
    assert len(h) <= 10 and [e.version for e in h if e.event == "ArgMapSnapshot"] == [2]   # still one slot each
