"""Integration: HTTP endpoints, websocket protocol (snapshot-then-live, ops), role overrides, mock profile E2E."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import latency
from app import main as mainmod
from app.bus import Bus, load_jsonl
from app.config import FLAGS
from app.main import build_app, loop_lag, resolve, supervise
from app.schemas import Claim, Control, Utterance, Verdict, parse_event
from app.tools import tail

ROOT = Path(__file__).resolve().parent.parent
ROLES = ("EARS", "BRAIN", "EVIDENCE", "FACE")
_POOL = ThreadPoolExecutor(max_workers=8)


def recv(ws, timeout: float = 5.0) -> dict:
    """receive_json() that fails the test instead of hanging it."""
    return _POOL.submit(ws.receive_json).result(timeout)


def recv_until(ws, pred, timeout: float = 5.0) -> dict:
    """Skip unrelated traffic (e.g. ModuleStatus 'loop lag') until a message matches."""
    deadline = time.monotonic() + timeout
    while True:
        m = recv(ws, max(0.05, deadline - time.monotonic()))
        if pred(m):
            return m


def wait_for(cond, timeout: float = 5.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not met in time")


async def fake_brain(bus):
    """Loaded via BRAIN=tests.test_main:fake_brain (the pkg.module:function override)."""
    bus.publish({"event": "BrainHello", "n": 1}, src="fake_brain")


SYNC_CALLS: list[int] = []


def sync_run(bus):
    """A plain `def run(bus)` that starts an SDK thread and returns (the docs allow it)."""
    SYNC_CALLS.append(1)
    bus.publish({"event": "SyncHello"}, src="sync")


def statuses(c) -> dict:
    return {e["module"]: e for e in c.get("/debug/history").json() if e["event"] == "ModuleStatus"}


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """make_client(profile, quiet=True, **env): quiet switches every role off unless env names it."""
    saved, clients = dict(FLAGS), []
    for k in ROLES + ("PRINT", "LOOP", "SPEED", "MOCK_EVIDENCE_DELAY", "FIXTURE"):
        monkeypatch.delenv(k, raising=False)

    def make(profile: str = "mock", quiet: bool = True, file=None, speed: float = 1.0, **env) -> TestClient:
        for k, v in ({r: "off" for r in ROLES} if quiet else {}).items():
            monkeypatch.setenv(k, v)
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        c = TestClient(build_app(profile, file=file, speed=speed, log_dir=tmp_path))
        c.__enter__()                                    # runs the lifespan (modules, flags watcher)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    FLAGS.clear()
    FLAGS.update(saved)


def test_health_and_debug_endpoints(make_client):
    c = make_client()
    h = c.get("/health").json()
    assert h["ok"] and h["schema"] and h["modules"] == {"ears": "off", "brain": "off", "evidence": "off", "face": "off"}
    assert "director" in h["flags"] and h["log"].endswith(".jsonl")
    assert c.get("/debug/subs").status_code == 200 and isinstance(c.get("/debug/history").json(), list)
    assert c.get("/").status_code == 200


def test_debug_page_served(make_client):
    r = make_client().get("/debug")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    for needle in ("Snapshot", "function apply(e, live)", "TTSState", "audio", "session_start"):
        assert needle in r.text


def test_http_publish_single_list_invalid(make_client):
    c = make_client()
    one = c.post("/publish", json={"event": "Pause", "speaker": "A"}, headers={"x-src": "curl"}).json()
    assert len(one) == 1 and one[0]["seq"] >= 1 and one[0]["src"] == "curl" and one[0]["ts"] > 0
    many = c.post("/publish", json=[{"event": "Claim", "id": "c1", "speaker": "A", "text": "x"},
                                    {"event": "Verdict", "claim_id": "c1", "label": "TRUE", "confidence": 90,
                                     "evidence": [{"url": "https://x.org"}]}]).json()
    assert [e["event"] for e in many] == ["Claim", "Verdict"] and many[1]["seq"] == many[0]["seq"] + 1
    assert many[1]["label"] == "supported" and many[1]["confidence"] == 0.9    # normalised on the way in
    assert c.post("/publish", json={"event": "Claim", "id": "c2", "text": "no speaker"}).status_code == 422
    assert c.post("/publish", json={"speaker": "A"}).status_code == 422
    assert c.post("/publish", content=b"not json").status_code == 422
    log = load_jsonl(c.app.state.log_path)
    assert [e.event for e in log if e.event in ("Pause", "Claim", "Verdict")] == ["Pause", "Claim", "Verdict"]


def test_session_and_control_endpoints(make_client):
    c = make_client()
    s = c.post("/session", json={"motion": "M", "speakers": {"spk1": {"name": "X", "side": "opp"}}}).json()
    assert s["event"] == "SessionStart" and s["speakers"]["spk1"]["side"] == "opp"
    ctl = c.post("/control", json={"flag": "web_fallback", "value": False}).json()
    assert ctl["event"] == "Control" and ctl["value"] is False
    wait_for(lambda: c.get("/health").json()["flags"]["web_fallback"] is False)


def test_ws_snapshot_then_live(make_client):
    c = make_client()
    before = [c.post("/publish", json={"event": "Pause", "speaker": f"p{i}"}).json()[0]["seq"] for i in range(3)]
    with c.websocket_connect("/ws") as ws:
        snap = recv(ws)
        assert snap["event"] == "Snapshot" and snap["last_seq"] >= before[-1]
        assert {"flags", "modules", "events", "schema"} <= set(snap)
        assert set(before) <= {e["seq"] for e in snap["events"]}
        assert max(e["seq"] for e in snap["events"]) <= snap["last_seq"]
        after = [c.post("/publish", json={"event": "Pause", "speaker": f"q{i}"}).json()[0]["seq"] for i in range(3)]
        got = []
        while not set(after) <= {m["seq"] for m in got}:
            got.append(recv(ws))
        seqs = [m["seq"] for m in got]
        assert seqs[0] == snap["last_seq"] + 1 and seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_ws_no_gap_no_dup_while_publishing(make_client):
    """Events keep arriving while the client connects: snapshot + live must cover them exactly once."""
    c = make_client()
    bus = c.app.state.bus
    started, stop = threading.Event(), threading.Event()

    def producer():
        i = 0
        while not stop.is_set():
            bus.publish_threadsafe({"event": "Pause", "speaker": "load", "t": float(i + 1)})
            i += 1
            if i == 20:
                started.set()
            time.sleep(0.001)
        bus.publish_threadsafe({"event": "Pause", "speaker": "end"})

    th = threading.Thread(target=producer, daemon=True)
    th.start()
    assert started.wait(5)
    with c.websocket_connect("/ws") as ws:
        snap = recv(ws)
        time.sleep(0.1)
        stop.set()
        live = []
        while True:
            m = recv(ws)
            if m.get("speaker") == "end":
                break
            live.append(m)
    th.join(5)
    snap_load = [e["seq"] for e in snap["events"] if e.get("speaker") == "load"]
    live_load = [m["seq"] for m in live if m.get("speaker") == "load"]
    published = [e.seq for e in bus.history if getattr(e, "speaker", None) == "load"]
    assert snap_load and live_load
    assert snap_load + live_load == published                          # no gap, no duplicate, in order
    assert all(s <= snap["last_seq"] for s in snap_load) and all(s > snap["last_seq"] for s in live_load)


def test_ws_ops(make_client):
    c = make_client()
    with c.websocket_connect("/ws") as ws:
        assert recv(ws)["event"] == "Snapshot"
        ws.send_json({"op": "session_start", "motion": "Ban cars", "speakers": {"Gov": {"name": "P", "side": "prop"}}})
        s = recv_until(ws, lambda m: m["event"] == "SessionStart")
        assert s["src"] == "dashboard" and s["speakers"]["Gov"]["name"] == "P" and s["motion"] == "Ban cars"
        ws.send_json({"op": "control", "flag": "aggr", "value": 5})
        ctl = recv_until(ws, lambda m: m["event"] == "Control")
        assert ctl["flag"] == "aggr" and ctl["value"] == 5
        ws.send_json({"op": "publish", "src": "dashboard:audio",
                      "event": {"event": "TTSState", "speaking": True, "interjection_id": "i-0001"}})
        tts = recv_until(ws, lambda m: m["event"] == "TTSState")
        assert tts["speaking"] is True and tts["src"] == "dashboard:audio"
        ws.send_json({"op": "control", "value": True})                 # missing flag -> Error reply, socket stays up
        err = recv_until(ws, lambda m: m["event"] == "Error")
        assert err["op"] == "control" and "seq" not in err
        ws.send_json({"op": "subscribe", "events": ["Claim", "Control"]})
        ws.send_json({"op": "control", "flag": "probe", "value": 1})   # processed after subscribe: a barrier
        assert recv_until(ws, lambda m: m["event"] == "Control")["flag"] == "probe"
        c.post("/publish", json=[{"event": "Pause", "speaker": "A"}, {"event": "Claim", "id": "c9", "speaker": "A", "text": "x"}])
        assert recv(ws)["event"] == "Claim"                             # the Pause was filtered out
    with c.websocket_connect("/ws") as ws2:                             # history was reset by the SessionStart
        assert recv(ws2)["events"][0]["event"] == "SessionStart"


def test_unknown_event_from_external_module_reaches_dashboards(make_client):
    c = make_client()
    with c.websocket_connect("/ws") as dash, c.websocket_connect("/ws") as ext:
        recv(dash), recv(ext)
        ext.send_json({"op": "publish", "src": "face", "event": {"event": "FaceReady", "voice": "en-GB", "nested": {"a": [1, 2]}}})
        m = recv_until(dash, lambda m: m["event"] == "FaceReady")
        assert m["voice"] == "en-GB" and m["nested"] == {"a": [1, 2]} and m["src"] == "face" and m["seq"] > 0
        c.post("/publish", json={"event": "EvidenceHeartbeat", "qps": 3}, headers={"x-src": "evidence-node"})
        m = recv_until(dash, lambda m: m["event"] == "EvidenceHeartbeat")
        assert m["qps"] == 3 and m["src"] == "evidence-node"
        m = recv_until(ext, lambda m: m["event"] == "EvidenceHeartbeat")    # the external module hears it too
        assert m["qps"] == 3


def test_role_overrides_from_env(make_client):
    c = make_client("mock", quiet=False, EARS="off", BRAIN="tests.test_main:fake_brain", EVIDENCE="external")
    assert c.get("/health").json()["modules"] == {"ears": "off", "brain": "tests.test_main:fake_brain",
                                                  "evidence": "external", "face": "off"}
    hist = wait_for(lambda: (h := c.get("/debug/history").json()) and any(e["event"] == "BrainHello" for e in h) and h)
    status = {e["module"]: e for e in hist if e["event"] == "ModuleStatus"}
    assert status["evidence"]["state"] == "up" and "external" in status["evidence"]["detail"]
    assert "ears" not in status and "face" not in status


def test_mock_profile_end_to_end(make_client, capsys):
    c = make_client("mock", quiet=False, SPEED=40, MOCK_EVIDENCE_DELAY=0.05,
                    FIXTURE=ROOT / "fixtures" / "fake_debate.jsonl")
    path = c.app.state.log_path
    with c.websocket_connect("/ws") as ws:
        recv(ws)
        live = recv_until(ws, lambda m: m["event"] == "Interjection", timeout=10)
    assert live["kind"] and live["text"] and live["evidence"] and live["target"]
    evs = wait_for(lambda: (e := load_jsonl(path)) and any(x.event == "ArgMapSnapshot" for x in e) and e)
    names = {e.event for e in evs}
    assert {"SessionStart", "Utterance", "Claim", "Verdict", "SourceCard", "Interjection"} <= names
    assert any(e.event == "Utterance" and not e.final for e in evs)
    subs = {s["name"] for s in c.get("/debug/subs").json()}
    assert {"mock_brain", "mock_evidence", "flags"} <= subs

    s = latency.summarize(evs)                                          # the latency meter on a real log
    assert s["claims"] > 0 and s["verdicts"] > 0 and s["interjections_by_kind"].get("correction", 0) >= 1
    for stage in ("stt", "extract", "time_to_chip", "verdict_e2e", "interject_after_pause"):
        assert s["stages"][stage]["n"] > 0, stage
    assert latency.main([str(path)]) == 0
    assert tail.main([str(path), "--no-color", "--only", "Interjection", "Verdict"]) == 0
    out = capsys.readouterr().out
    assert "time_to_chip" in out and "verdicts by via" in out
    assert "Interjection" in out and "Verdict" in out


# ----------------------------------------------------------------------------- review regressions: /ws, HTTP
def test_ws_bare_events_and_bad_frames_keep_the_socket_working(make_client):
    """Review: a bare event was ignored; one array / non-JSON frame killed the client's publish path."""
    c = make_client()
    with c.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_json({"event": "Utterance", "speaker": "A", "text": "bare"})       # no "op": same as POST /publish
        assert recv_until(ws, lambda m: m.get("text") == "bare")["src"] == "ws"
        ws.send_json([{"event": "Pause", "speaker": "A1"}, {"event": "Pause", "speaker": "A2"}])
        assert recv_until(ws, lambda m: m.get("speaker") == "A2")["seq"] > 0
        for bad, op in (("ping", None), ("5", None), ('{"op": "nope"}', "nope"),
                        ('[{"event": "Pause", "speaker": "C"}, {"event": "Claim", "id": "c1"}]', "publish")):
            ws.send_text(bad)
            err = recv_until(ws, lambda m: m["event"] == "Error")
            assert err["op"] == op and err["detail"] and "seq" not in err
        ws.send_bytes(b'{"event": "Pause", "speaker": "D"}')
        assert recv_until(ws, lambda m: m.get("speaker") == "D")["event"] == "Pause"
        ws.send_json({"op": "publish", "event": {"event": "Utterance", "speaker": "A", "text": "still alive"}})
        assert recv_until(ws, lambda m: m.get("text") == "still alive")
    assert not [e for e in c.app.state.bus.history if getattr(e, "speaker", None) == "C"]   # bad batch: none of it


def test_ws_session_start_and_control_keep_extra_fields(make_client):
    c = make_client()
    with c.websocket_connect("/ws") as ws:
        recv(ws)
        ws.send_json({"op": "session_start", "motion": "M", "speakers": {"A": {"side": "prop"}}, "time_limit_s": 240})
        s = recv_until(ws, lambda m: m["event"] == "SessionStart")
        assert s["time_limit_s"] == 240 and s["src"] == "dashboard" and s["mode"] == "live"
        ws.send_json({"op": "control", "flag": "mute", "value": "false", "by": "admin"})
        ctl = recv_until(ws, lambda m: m["event"] == "Control")
        assert ctl["value"] is False and ctl["by"] == "admin"


def test_publish_batch_is_all_or_nothing_and_bad_bodies_are_422(make_client):
    c = make_client()
    r = c.post("/publish", json=[{"event": "Pause", "speaker": "first"}, {"event": "Claim", "id": "c1"}])
    assert r.status_code == 422
    assert not [e for e in c.app.state.bus.history if getattr(e, "speaker", None) == "first"]
    assert c.post("/session", json={"speakers": "nope"}).status_code == 422
    assert c.post("/control", json={"value": 1}).status_code == 422
    assert c.post("/control", content=b"nope").status_code == 422


def test_publish_stream_relative_times_become_unknown(make_client):
    """Review: Deepgram-style seconds-since-stream-start made every claim 'stale' and opened the floor."""
    c = make_client()
    now = time.time()
    u = c.post("/publish", json={"event": "Utterance", "speaker": "A", "text": "x", "t_start": 2.8, "t_end": 3.4,
                                 "ts": 3.5}).json()[0]
    assert (u["t_start"], u["t_end"]) == (0, 0) and abs(u["ts"] - now) < 5     # 0 = unknown -> consumers use ts
    p = c.post("/publish", json={"event": "Pause", "speaker": "A", "t": now * 1000}).json()[0]
    assert p["t"] == pytest.approx(now, abs=1)                                  # JS Date.now() is fine


# ----------------------------------------------------------------------------- review regressions: replay
def _write_log(path: Path, rows: list[dict]) -> Path:
    import json
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_replay_latest_symlink_replays_the_recorded_run_and_stays_silent_on_refresh(make_client, tmp_path):
    """Review: `--file logs/latest.jsonl` replayed the replay's own empty log (symlink re-pointed first)."""
    rec = _write_log(tmp_path / "20261107-120000-drive.jsonl", [
        {"event": "ModuleStatus", "module": "ears", "state": "down", "detail": "finished", "ts": 990.0},
        {"event": "SessionStart", "motion": "ban cars", "ts": 1000.0},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "Bus use fell by a third.", "ts": 1000.1},
        {"event": "Interjection", "id": "i-0001", "text": "Point of fact.", "kind": "correction", "ts": 1001.0},
    ])
    (tmp_path / "latest.jsonl").symlink_to(rec.name)
    c = make_client("replay", quiet=False, file=tmp_path / "latest.jsonl", speed=4)
    assert os.readlink(tmp_path / "latest.jsonl") == rec.name                  # a replay never re-points latest
    with c.websocket_connect("/ws") as ws:
        snap = recv(ws)
        live = recv_until(ws, lambda m: m["event"] == "Interjection")
        assert live["seq"] > snap["last_seq"]                                   # live: the audio tab speaks it
    st = wait_for(lambda: (s := statuses(c)) and "finished" in s.get("replay", {}).get("detail", "") and s)
    assert "ears" not in st                                                     # recorded module dots aren't replayed
    with c.websocket_connect("/ws") as ws2:                                     # a refresh after the replay
        snap = recv(ws2)
        (ij,) = [e for e in snap["events"] if e["event"] == "Interjection"]
        assert ij["seq"] <= snap["last_seq"]                                    # history: never spoken again
    page = (ROOT / "app" / "tools" / "ws_debug.html").read_text()
    assert "if (live && e.seq > snapSeq) speak(e)" in page


def test_replay_missing_file_fails_loudly(tmp_path, monkeypatch, capsys):
    with pytest.raises(FileNotFoundError):
        build_app("replay", file=str(tmp_path / "golden_run.jsonl"), log_dir=tmp_path)
    for argv, needle in ((["replay", "--file", str(tmp_path / "golden_run.jsonl")], "golden_run.jsonl"),
                         (["replay"], "replay needs --file")):
        monkeypatch.setattr(sys, "argv", ["app.main", *argv])
        with pytest.raises(SystemExit) as e:
            mainmod.main()
        assert e.value.code == 2 and needle in capsys.readouterr().err


def test_replay_missing_file_says_how_to_create_it(tmp_path, monkeypatch, capsys):
    golden = tmp_path / "golden_run.jsonl"
    with pytest.raises(FileNotFoundError, match="cp logs/latest.jsonl"):
        build_app("replay", file=str(golden), log_dir=tmp_path)
    monkeypatch.setattr(sys, "argv", ["app.main", "replay", "--file", str(golden)])
    with pytest.raises(SystemExit):
        mainmod.main()
    err = capsys.readouterr().err
    assert "No such file" in err and f"cp logs/latest.jsonl {golden}" in err
    with pytest.raises(FileNotFoundError, match="No run recorded yet"):   # cp latest -> latest would be nonsense
        build_app("replay", file=str(tmp_path / "latest.jsonl"), log_dir=tmp_path)


# ----------------------------------------------------------------------------- review regressions: flags
async def test_flags_watcher_persists_state_flags_only():
    """force_next/veto/time are one-shot commands: they reach the bus untouched but never stick in FLAGS."""
    saved, b = dict(FLAGS), Bus()
    seen = b.subscribe("brain", {"Control"})
    t = asyncio.create_task(mainmod.flags_watcher(b))
    await asyncio.sleep(0)
    try:
        sent = [("force_next", True), ("veto", "c3:correction"), ("time", "A"), ("mute", "on"), ("aggr", "4"),
                ("tts", False), ("director", False), ("web_fallback", "off"), ("interims", 0), ("agent_speaking", 1),
                ("screen", "final")]   # a flag another module adds (D's final screen) is state too
        for flag, value in sent:
            b.publish(Control(flag=flag, value=value), src="test")
        for _ in range(20):
            await asyncio.sleep(0)
        assert not {"force_next", "veto", "time"} & set(FLAGS)
        assert {k: FLAGS[k] for k in ("mute", "aggr", "tts", "director", "web_fallback", "interims", "agent_speaking",
                                      "screen")} \
            == {"mute": True, "aggr": 4, "tts": False, "director": False, "web_fallback": False, "interims": False,
                "agent_speaking": True, "screen": "final"}
        got = [seen.q.get_nowait() for _ in range(seen.q.qsize())]
        assert [(e.flag, e.value) for e in got[:3]] == sent[:3]
    finally:
        t.cancel()
        FLAGS.clear()
        FLAGS.update(saved)


def test_one_shot_controls_not_in_health_or_snapshot_flags(make_client):
    c = make_client()
    for flag, value in (("force_next", True), ("veto", "k1"), ("time", "A"), ("aggr", 5)):
        assert c.post("/control", json={"flag": flag, "value": value}).status_code == 200
    wait_for(lambda: c.get("/health").json()["flags"].get("aggr") == 5)
    assert not {"force_next", "veto", "time"} & set(c.get("/health").json()["flags"])
    with c.websocket_connect("/ws") as ws:
        snap = recv(ws)
        assert not {"force_next", "veto", "time"} & set(snap["flags"]) and snap["flags"]["aggr"] == 5
        assert {"force_next", "veto", "time"} <= {e["flag"] for e in snap["events"] if e["event"] == "Control"}


# ----------------------------------------------------------------------------- review regressions: debug page
def test_director_decisions_survive_a_refresh(make_client):
    """The bus history keeps DirectorDecision, so a refreshed /debug tab rebuilds its Decisions panel."""
    c = make_client()
    c.post("/publish", json=[{"event": "DirectorDecision", "key": f"k{i}", "kind": "correction", "action": "held"}
                             for i in range(3)])
    with c.websocket_connect("/ws") as ws:
        assert [e["key"] for e in recv(ws)["events"] if e["event"] == "DirectorDecision"] == ["k0", "k1", "k2"]
    page = (ROOT / "app" / "tools" / "ws_debug.html").read_text()
    assert "live only" not in page and "since this tab connected" not in page
    assert "S.decisions.splice(0, S.decisions.length - DECISIONS_MAX)" in page and "DECISIONS_MAX = 50" in page


@pytest.mark.skipif(not shutil.which("deno"), reason="runs the page's reducer in deno when it is installed")
def test_debug_reducer_snapshot_decisions_capped_and_silent(tmp_path):
    """Runs the real reducer block of ws_debug.html: snapshot decisions are applied (last 50 kept), a snapshot
    Interjection is never spoken, a live one is, and one-shot Controls never land in S.flags."""
    page = (ROOT / "app" / "tools" / "ws_debug.html").read_text()
    rule = "// " + "-" * 66
    consts = page[page.index("const LABEL"):page.index(rule + " state")]
    block = page[page.index("let S = null"):page.index(rule + " transport")]
    js = consts + block + """
const spoken = []; function speak(e) { spoken.push(e.id); } function onControl() {}
S = null; reset();
const hist = [];
for (let i = 0; i < 60; i++) hist.push({event: 'DirectorDecision', seq: i + 1, key: 'k' + i, action: 'held'});
hist.push({event: 'Interjection', seq: 61, id: 'i-old', text: 'x'});
hist.push({event: 'Control', seq: 62, flag: 'force_next', value: true});
hist.push({event: 'Control', seq: 63, flag: 'mute', value: true});
for (const h of hist) apply(h, false);
snapSeq = 63;
const snap = {n: S.decisions.length, first: S.decisions[0].key, spoken: spoken.length, flags: Object.keys(S.flags)};
apply({event: 'Interjection', seq: 64, id: 'i-new', text: 'y'}, true);
apply({event: 'Control', seq: 65, flag: 'veto', value: 'k1'}, true);
console.log(JSON.stringify({...snap, live: spoken, flags2: Object.keys(S.flags)}));
"""
    env = {**os.environ, "DENO_DIR": str(tmp_path), "NO_COLOR": "1"}
    out = subprocess.run(["deno", "eval", js], capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert (r["n"], r["first"], r["spoken"], r["live"]) == (50, "k10", 0, ["i-new"])
    assert r["flags"] == ["mute"] and r["flags2"] == ["mute"]


# ----------------------------------------------------------------------------- review regressions: supervisor
async def test_supervisor_accepts_sync_and_returning_modules():
    b = Bus()
    s = b.subscribe("m", {"ModuleStatus"})
    SYNC_CALLS.clear()
    await asyncio.wait_for(supervise(b, "ears", "tests.test_main:sync_run"), 1)   # returns: never re-run
    await asyncio.wait_for(supervise(b, "brain", "tests.test_main:fake_brain"), 1)
    st = [(e.module, e.state) for e in [s.q.get_nowait() for _ in range(s.q.qsize())]]
    assert SYNC_CALLS == [1] and st == [("ears", "up"), ("ears", "up"), ("brain", "up"), ("brain", "up")]


async def test_supervisor_missing_run_py_and_missing_dependency(tmp_path, monkeypatch):
    b = Bus()
    s = b.subscribe("m", {"ModuleStatus"})
    await supervise(b, "ears", "app.ears_unwritten.run:run", served="serving routes.py")
    await supervise(b, "face", "app.face_unwritten.run:run")
    (tmp_path / "dep_mod.py").write_text("import surely_missing_pkg_7f3\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    await asyncio.wait_for(supervise(b, "evidence", "dep_mod:run"), 1)          # no retry loop
    ears, face, ev = [s.q.get_nowait() for _ in range(3)]
    assert (ears.state, face.state, ev.state) == ("up", "down", "down")
    assert "routes.py" in ears.detail and "FACE=external|off" in face.detail    # no face mock: not suggested
    assert "missing dependency 'surely_missing_pkg_7f3'" in ev.detail


def test_routes_or_static_only_roles_are_not_red(make_client, monkeypatch, tmp_path):
    """Review: A with only routes.py and D with only static/index.html showed red dots in live/drive."""
    from fastapi import APIRouter
    r = APIRouter()
    r.add_api_route("/mic", lambda: {"mic": True})
    monkeypatch.setitem(sys.modules, "app.ears.routes", types.SimpleNamespace(router=r))
    static = tmp_path / "root" / "app" / "face" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<h1>D's dashboard</h1>")
    monkeypatch.setattr(mainmod, "ROOT", tmp_path / "root")
    c = make_client("live", quiet=False, BRAIN="off", EVIDENCE="off")
    assert c.get("/mic").json() == {"mic": True} and "D's dashboard" in c.get("/").text
    st = wait_for(lambda: (s := statuses(c)) and {"ears", "face"} <= set(s) and s)
    assert (st["ears"]["state"], st["face"]["state"]) == ("up", "up"), st


def test_role_choice_typos_are_loud(make_client):
    assert resolve("ears", " MOCK ") == "app.mocks.mock_ears:run"
    assert resolve("evidence", "app.mocks.mock_evidence") == "app.mocks.mock_evidence:run"   # ':run' implied
    with pytest.raises(ValueError, match="FACE='mock'"):
        resolve("face", "mock")
    c = make_client("mock", quiet=False, EARS="off", BRAIN="off", EVIDENCE="External", FACE="Externl")
    assert c.get("/health").json()["modules"]["evidence"] == "external"
    st = wait_for(lambda: (s := statuses(c)) and {"evidence", "face"} <= set(s) and s)
    assert st["evidence"]["state"] == "up" and st["face"]["state"] == "down" and "Externl" in st["face"]["detail"]


async def test_loop_lag_recovers_after_a_stall():
    """Review: one 1 s startup stall left the loop dot yellow for the whole demo."""
    b = Bus()
    s = b.subscribe("m", {"ModuleStatus"})
    t = asyncio.create_task(loop_lag(b, tick=0.01, warn=0.04, recover=5))
    await asyncio.sleep(0.03)
    time.sleep(0.15)                                                            # a blocking call on the loop
    for _ in range(100):
        await asyncio.sleep(0.01)
        if any(e.state == "up" for e in list(s.q._queue)):
            break
    t.cancel()
    states = [e.state for e in list(s.q._queue)]
    assert states[0] == "degraded" and states[-1] == "up"


# ----------------------------------------------------------------------------- review regressions: run.sh
def _fake_repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    (d / ".venv" / "bin").mkdir(parents=True)
    (d / "requirements").mkdir()
    shutil.copy(ROOT / "run.sh", d / "run.sh")
    (d / "requirements.txt").write_text("-r requirements/base.txt\n-r requirements/ears.txt\n")
    (d / "requirements" / "base.txt").write_text("fastapi\n")
    (d / "requirements" / "ears.txt").write_text("# A\n")
    py, pip = d / ".venv" / "bin" / "python", d / ".venv" / "bin" / "pip"
    py.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit ${FAKE_VENV_BROKEN:-0}; fi\necho "PYTHON $*"\n')
    pip.write_text('#!/bin/sh\necho pip >> "$(dirname "$0")/../pip.log"\nexit ${FAKE_PIP_EXIT:-0}\n')
    py.chmod(0o755), pip.chmod(0o755)
    return d


def test_run_sh_reinstalls_on_role_requirements_and_survives_pip_failure(tmp_path):
    """Review: requirements/<role>.txt changes never installed; any pip failure blocked offline replay."""
    d = _fake_repo(tmp_path)

    def run(**env):
        r = subprocess.run(["bash", str(d / "run.sh"), "mock"], capture_output=True, text=True, timeout=20,
                           env={**os.environ, "SKIP_PIP": "", **env})
        log = d / ".venv" / "pip.log"
        n = len(log.read_text().splitlines()) if log.exists() else 0
        return r, n

    r, n = run()
    assert r.returncode == 0 and "PYTHON -m app.main mock" in r.stdout and n == 1
    assert run()[1] == 1                                                        # unchanged: no pip
    (d / "requirements" / "ears.txt").write_text("# A\ndeepgram-sdk\n")
    assert run()[1] == 2                                                        # a role file changed: pip
    (d / "requirements" / "ears.txt").write_text("# A\ndeepgram-sdk\nnumpy\n")
    r, n = run(FAKE_PIP_EXIT="1")
    assert n == 3 and r.returncode == 0 and "WARN" in r.stderr and "PYTHON -m app.main" in r.stdout
    r, n = run(FAKE_PIP_EXIT="1", FAKE_VENV_BROKEN="1")
    assert r.returncode != 0 and "ERROR" in r.stderr and "PYTHON" not in r.stdout
    assert run(FAKE_PIP_EXIT="1", SKIP_PIP="1")[0].returncode == 0             # offline venue: skip pip


# ----------------------------------------------------------------------------- latency meter (app/latency.py)
def test_latency_summary_exact_on_synthetic_log():
    T, ev = 1000.0, [{"url": "https://x.org"}]
    evs = [parse_event(d) for d in [
        {"event": "SessionStart", "ts": T - 10},                       # a rehearsal, dropped by --last
        {"event": "Claim", "id": "c0", "speaker": "A", "text": "old", "ts": T - 9},
        {"event": "SessionStart", "ts": T},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "x", "t_end": T + 1, "ts": T + 1.5, "final": False},
        {"event": "Utterance", "id": "u1", "speaker": "A", "text": "x", "t_end": T + 1, "ts": T + 2},
        {"event": "Claim", "id": "c1", "speaker": "A", "text": "x", "ts": T + 2.5,
         "marks": {"speech_end": T + 1, "stt_final": T + 2, "claim": T + 2.4}},
        {"event": "Claim", "id": "c1", "speaker": "A", "text": "x (revised)", "ts": T + 9},   # timed once
        {"event": "Verdict", "claim_id": "c1", "label": "supported", "evidence": ev, "via": "index", "latency_ms": 3000,
         "marks": {"claim": T + 2.4, "retrieved": T + 2.5, "verdict": T + 4.0}},
        {"event": "Verdict", "claim_id": "c1", "label": "contradicted", "evidence": ev, "via": "web", "ts": T + 8},
        {"event": "Utterance", "id": "u2", "speaker": "B", "text": "y", "ts": T + 61},
        {"event": "Pause", "speaker": "A", "t": T + 5, "ts": T + 5},
        {"event": "Interjection", "id": "i-1", "text": "t", "kind": "correction", "ts": T + 5.4},
        {"event": "Interjection", "id": "i-2", "text": "t", "kind": "prompt", "ts": T + 90},          # no Pause near it
    ]]
    s = latency.summarize(latency.last_session(evs))
    st = {k: v["p50"] for k, v in s["stages"].items()}
    assert st["stt"] == pytest.approx(1.0) and s["stages"]["stt"]["n"] == 1     # finals only
    assert st["extract"] == pytest.approx(0.4) and st["time_to_chip"] == pytest.approx(1.4)
    assert st["retrieve"] == pytest.approx(0.1) and st["judge"] == pytest.approx(1.5)
    assert st["verdict_e2e"] == 3.0 and s["verdict_e2e_by_via"]["index"]["n"] == 1
    assert st["interject_after_pause"] == pytest.approx(0.4) and s["stages"]["interject_after_pause"]["n"] == 1
    assert (s["claims"], s["verdicts"], s["verdicted_claims"], s["sessions"]) == (1, 2, 1, 1)
    assert s["verdicts_by_via"] == {"index": 1, "web": 1} and s["interjections_by_kind"] == {"correction": 1, "prompt": 1}
    assert s["claims_per_min"] == pytest.approx(1 / ((61 - 2) / 60), abs=0.05)
    assert latency.summarize(evs)["claims"] == 2
    assert "verdict_e2e" in latency.report(s) and "retrieve" in latency.report(s)
    assert latency.pct([1, 2, 3, 4], 0.5) == 2.5 and latency.pct([5], 0.9) == 5 and latency.pct([], 0.5) is None


def test_latency_e2e_fallback_without_latency_ms():
    evs = [parse_event(d) for d in [
        {"event": "Claim", "id": "c1", "speaker": "A", "text": "x", "t_end": 100.0, "ts": 101.0},
        {"event": "Verdict", "claim_id": "c1", "ts": 103.5},                                   # joins the claim's t_end
    ]]
    assert latency.summarize(evs)["stages"]["verdict_e2e"]["p50"] == pytest.approx(3.5)


def test_stamp_and_finish_helpers():
    u = latency.stamp(Utterance(speaker="A", text="x"), "heard", 5.0)
    assert u.marks == {"heard": 5.0} and parse_event(u.model_dump_json()).marks == {"heard": 5.0}
    assert latency.stamp({"event": "Pause", "marks": {"a": 1.0}}, "b", 2.0)["marks"] == {"a": 1.0, "b": 2.0}
    claim = Claim(id="c1", speaker="A", text="x", t_end=100.0, marks={"speech_end": 100.0, "claim": 101.0})
    v = latency.finish({"claim_id": "c1", "label": "supported", "evidence": [{"url": "https://x.org"}]}, claim, now=103.25)
    assert isinstance(v, Verdict) and v.latency_ms == 3250 and v.marks["verdict"] == 103.25
    latency.stamp(v, "published", 104.0)
    assert "verdict" not in claim.marks and "published" not in claim.marks          # never aliases the claim's marks
    v2 = latency.finish(Verdict(claim_id="c2"), {"t_end": 1_762_500_000_000, "marks": {}}, now=1_762_500_001.0)
    assert v2.latency_ms == 1000                                                       # ms t_end from a JS producer
    assert latency.finish(Verdict(claim_id="c3"), None, now=5.0).latency_ms == 0       # unknown speech end: no number
