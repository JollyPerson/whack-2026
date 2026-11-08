"""The phone web-page microphone: /mic, /mic/ws, the PHONE_MICS null sinks, MIC_TOKEN, one phone per channel,
bounded frames, writer cleanup. pactl and pacat are faked; one test runs a real subprocess as the writer."""
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from app.face import phone_mic
from tests.test_main import make_client  # noqa: F401  (fixture)

REAL_SPAWN = phone_mic.spawn_writer
ROOT = Path(__file__).resolve().parent.parent


class FakePactl:
    def __init__(self, sinks=("alsa_output.speakers",), works=True, default="alsa_output.speakers"):
        self.sinks = {name: i for i, name in enumerate(sinks, 1)}
        self.calls, self.works, self.default, self.next_id = [], works, default, 500
        self.steal_default = False

    def __call__(self, cmd, timeout=5.0):
        self.calls.append(cmd)
        if not self.works:
            return 127, "pactl not found"
        args = cmd[1:]
        if args == ["list", "short", "sinks"]:
            return 0, "\n".join(f"{i}\t{n}\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED" for n, i in self.sinks.items())
        if args == ["get-default-sink"]:
            return 0, self.default
        if args[:1] == ["set-default-sink"]:
            self.default = args[1]
            return 0, ""
        if args[:2] == ["load-module", "module-null-sink"]:
            name = args[2].split("=", 1)[1]
            self.next_id += 1
            self.sinks[name] = self.next_id
            if self.steal_default:
                self.default = name
            return 0, str(self.next_id)
        if args[:1] == ["unload-module"]:
            mod = int(args[1])
            self.sinks = {n: i for n, i in self.sinks.items() if i != mod}
            return 0, ""
        return 1, f"unexpected {cmd}"

    def loads(self):
        return [c for c in self.calls if c[1:2] == ["load-module"]]

    def unloads(self):
        return [c[2] for c in self.calls if c[1:2] == ["unload-module"]]


class FakeStdin:
    def __init__(self, proc):
        self.proc, self.closed = proc, False

    def write(self, data):
        if self.proc.broken:
            raise BrokenPipeError()
        self.proc.data.extend(data)

    async def drain(self):
        if self.proc.stuck:
            await asyncio.sleep(10)

    def close(self):
        self.closed = True
        if not self.proc.hang:
            self.proc.returncode = 0

    def is_closing(self):
        return self.closed


class FakeProc:
    def __init__(self, cmd, hang=False):
        self.cmd, self.data, self.returncode, self.killed = cmd, bytearray(), None, False
        self.hang, self.broken, self.stuck, self.waited = hang, False, False, False
        self.stdin = FakeStdin(self)

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        self.waited = True
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9


@pytest.fixture
def fakes(monkeypatch):
    pactl, procs = FakePactl(), []
    opts = {"hang": False}

    async def spawn(cmd):
        p = FakeProc(cmd, hang=opts["hang"])
        procs.append(p)
        return p

    monkeypatch.setattr(phone_mic, "run_cmd", pactl)
    monkeypatch.setattr(phone_mic, "spawn_writer", spawn)
    monkeypatch.setattr(phone_mic, "writer_cmd", lambda sink: ["pacat", f"--device={sink}"])
    monkeypatch.setattr(phone_mic, "CLOSE_GRACE_S", 0.2)
    monkeypatch.delenv("PHONE_MICS", raising=False)
    monkeypatch.delenv("MIC_TOKEN", raising=False)
    return pactl, procs, opts


def until(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def refused(c, url):
    with c.websocket_connect(url) as ws:
        msg = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    return msg, e.value.code


def statuses(c, ch):
    return [e for e in c.get("/debug/history?n=2000").json()
            if e["event"] == "ModuleStatus" and e["module"] == f"phone_{ch}"]


def test_page_and_scripts_are_served(make_client, fakes):
    c = make_client()
    r = c.get("/mic?ch=A")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/static/mic.js" in r.text and "Phone mic" in r.text
    for asset in ("mic.js", "mic-worklet.js"):
        assert c.get(f"/static/{asset}").status_code == 200
    assert "mic-downsampler" in c.get("/static/mic-worklet.js").text


def test_off_without_phone_mics(make_client, fakes):
    """Not configured: no pactl call, no status dot, and the socket refuses with a pointer to PHONE_MICS."""
    pactl, procs, _ = fakes
    c = make_client()
    assert pactl.calls == [] and statuses(c, "A") == []
    msg, code = refused(c, "/mic/ws?ch=A")
    assert code == 4404 and "PHONE_MICS" in msg["error"] and procs == []
    assert "phone_A" not in str(c.get("/health").json())


def test_sinks_created_at_start_and_unloaded_at_shutdown(make_client, fakes):
    pactl, _, _ = fakes
    c = make_client(PHONE_MICS="a, b")
    loads = pactl.loads()
    assert [l[3] for l in loads] == ["sink_name=whack_phone_A", "sink_name=whack_phone_B"]
    assert all(l[2] == "module-null-sink" and l[4].startswith("sink_properties=device.description=") for l in loads)
    assert " " not in loads[0][4]                     # pactl splits module args on spaces
    assert {"whack_phone_A", "whack_phone_B"} <= set(pactl.sinks)
    st = statuses(c, "A")[-1]
    assert st["state"] == "degraded" and "whack_phone_A.monitor" in st["detail"]
    c.__exit__(None, None, None)
    assert pactl.unloads() == ["501", "502"] and "whack_phone_A" not in pactl.sinks


def test_existing_sink_is_reused_and_left_alone(make_client, fakes):
    pactl, procs, _ = fakes
    pactl.sinks["whack_phone_A"] = 77
    c = make_client(PHONE_MICS="A")
    assert pactl.loads() == []
    with c.websocket_connect("/mic/ws?ch=A") as ws:
        assert ws.receive_json()["ok"] is True
    c.__exit__(None, None, None)
    assert pactl.unloads() == [] and "whack_phone_A" in pactl.sinks


def test_a_new_sink_never_stays_the_default(make_client, fakes):
    pactl, _, _ = fakes
    pactl.steal_default = True
    make_client(PHONE_MICS="A")
    assert pactl.default == "alsa_output.speakers"


def test_frames_reach_the_writer_in_order(make_client, fakes):
    _, procs, _ = fakes
    c = make_client(PHONE_MICS="A,B")
    with c.websocket_connect("/mic/ws?ch=B") as ws:
        hello = ws.receive_json()
        assert hello == {"ok": True, "ch": "B", "side": "Opposition", "sink": "whack_phone_B", "rate": 16000,
                         "max_frame": phone_mic.MAX_FRAME_BYTES}
        frames = [bytes([i]) * 2560 for i in range(5)]
        for f in frames:
            ws.send_bytes(f)
        until(lambda: len(procs[0].data) == 5 * 2560)
        assert bytes(procs[0].data) == b"".join(frames)
        assert procs[0].cmd == ["pacat", "--device=whack_phone_B"]
        assert statuses(c, "B")[-1]["state"] == "up"
    until(lambda: procs[0].waited)
    assert procs[0].stdin.closed and not procs[0].killed
    until(lambda: statuses(c, "B")[-1]["state"] == "degraded")


def test_bad_frames_are_dropped(make_client, fakes):
    _, procs, _ = fakes
    c = make_client(PHONE_MICS="A")
    with c.websocket_connect("/mic/ws?ch=A") as ws:
        ws.receive_json()
        ws.send_bytes(b"\x01" * (phone_mic.MAX_FRAME_BYTES + 2))   # oversize
        ws.send_bytes(b"\x01\x02\x03")                             # odd length: not s16
        ws.send_bytes(b"")                                         # empty
        ws.send_text("hello" * 1000)                               # text: ignored
        ws.send_bytes(b"\x05\x06" * 10)                            # good
        until(lambda: len(procs[0].data) == 20)
        assert bytes(procs[0].data) == b"\x05\x06" * 10


def test_token(make_client, fakes):
    _, procs, _ = fakes
    c = make_client(PHONE_MICS="A", MIC_TOKEN="s3cr&t")
    msg, code = refused(c, "/mic/ws?ch=A")
    assert code == 4401 and "token" in msg["error"]
    assert refused(c, "/mic/ws?ch=A&token=wrong")[1] == 4401
    assert procs == []
    with c.websocket_connect("/mic/ws?ch=A&token=s3cr%26t") as ws:
        assert ws.receive_json()["ok"] is True


def test_second_phone_is_refused_until_the_first_leaves(make_client, fakes):
    _, procs, _ = fakes
    c = make_client(PHONE_MICS="A,B")
    with c.websocket_connect("/mic/ws?ch=A") as first:
        first.receive_json()
        msg, code = refused(c, "/mic/ws?ch=A")
        assert code == 4409 and "already microphone A" in msg["error"]
        with c.websocket_connect("/mic/ws?ch=B") as other:              # the other channel is independent
            assert other.receive_json()["ok"] is True
        first.send_bytes(b"\x00\x01" * 4)
        until(lambda: len(procs[0].data) == 8)                           # the first phone still streams
    until(lambda: "A" not in c.app.state.phone_mics.active)
    with c.websocket_connect("/mic/ws?ch=A") as again:
        assert again.receive_json()["ok"] is True
    assert len(procs) == 3


def test_unknown_or_disabled_channel(make_client, fakes):
    c = make_client(PHONE_MICS="A")
    assert refused(c, "/mic/ws?ch=C")[1] == 4404
    assert refused(c, "/mic/ws")[1] == 4404
    msg, code = refused(c, "/mic/ws?ch=B")
    assert code == 4404 and "not enabled" in msg["error"]


def test_a_hanging_writer_is_killed_and_reaped(make_client, fakes):
    _, procs, opts = fakes
    opts["hang"] = True
    c = make_client(PHONE_MICS="A")
    with c.websocket_connect("/mic/ws?ch=A") as ws:
        ws.receive_json()
    until(lambda: procs[0].killed and procs[0].waited)
    until(lambda: c.app.state.phone_mics.procs == set())


def test_writer_dying_mid_stream_ends_the_connection(make_client, fakes):
    _, procs, _ = fakes
    c = make_client(PHONE_MICS="A")
    with c.websocket_connect("/mic/ws?ch=A") as ws:
        ws.receive_json()
        procs[0].broken = True
        ws.send_bytes(b"\x00\x00" * 8)
        msg = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 1011 and "stopped taking audio" in msg["error"]
    until(lambda: "A" not in c.app.state.phone_mics.active)


def test_stuck_writer_times_out(make_client, fakes, monkeypatch):
    _, procs, _ = fakes
    monkeypatch.setattr(phone_mic, "DRAIN_TIMEOUT_S", 0.1)
    c = make_client(PHONE_MICS="A")
    with c.websocket_connect("/mic/ws?ch=A") as ws:
        ws.receive_json()
        procs[0].stuck = True
        ws.send_bytes(b"\x00\x00")
        assert ws.receive_json()["code"] == 1011


def test_sink_gone_at_connect(make_client, fakes):
    pactl, procs, _ = fakes
    c = make_client(PHONE_MICS="A")
    del pactl.sinks["whack_phone_A"]                  # someone unloaded it by hand
    msg, code = refused(c, "/mic/ws?ch=A")
    assert code == 1011 and "gone" in msg["error"] and procs == []   # never pacat to a missing sink: the speakers


def test_no_pactl(make_client, fakes):
    pactl, procs, _ = fakes
    pactl.works = False
    c = make_client(PHONE_MICS="A")
    assert statuses(c, "A")[-1]["state"] == "down"
    msg, code = refused(c, "/mic/ws?ch=A")
    assert code == 4404 and "no sink" in msg["error"] and procs == []


def test_shutdown_with_a_phone_connected_kills_the_writer_and_unloads(fakes):
    pactl, _, _ = fakes

    async def go():
        mics = phone_mic.PhoneMics(["A", "B"])
        mics.setup()
        stuck = FakeProc(["pacat"], hang=True)
        mics.procs.add(stuck)
        await mics.teardown()
        return mics, stuck

    mics, stuck = asyncio.run(go())
    assert stuck.killed and stuck.waited and mics.procs == set()
    assert pactl.unloads() == ["501", "502"] and mics.modules == {}


def test_enabled_channels_parsing():
    assert phone_mic.enabled_channels("A,B") == ["A", "B"]
    assert phone_mic.enabled_channels(" b ") == ["B"]
    assert phone_mic.enabled_channels("A,A,x") == ["A"]
    assert phone_mic.enabled_channels("") == []


def test_writer_cmd_prefers_pacat_and_never_falls_back_to_the_speakers(monkeypatch):
    have = {"pacat", "pw-cat"}
    monkeypatch.setattr(phone_mic.shutil, "which", lambda n: f"/usr/bin/{n}" if n in have else None)
    cmd = phone_mic.writer_cmd("whack_phone_A")
    assert cmd[0] == "pacat" and "--device=whack_phone_A" in cmd and "--format=s16le" in cmd
    assert "--rate=16000" in cmd and "--channels=1" in cmd and "--raw" in cmd
    assert "--property=node.dont-fallback=true" in cmd and "--property=node.dont-reconnect=true" in cmd
    have.discard("pacat")
    cmd = phone_mic.writer_cmd("whack_phone_A")
    assert cmd[0] == "pw-cat" and "--target=whack_phone_A" in cmd and "--raw" in cmd and cmd[-1] == "-"
    assert "node.dont-fallback = true" in " ".join(cmd)
    have.clear()
    assert phone_mic.writer_cmd("whack_phone_A") is None


def test_a_real_subprocess_writer_gets_the_bytes_and_is_reaped(make_client, fakes, monkeypatch, tmp_path):
    """The real spawn_writer/close_writer path, with `cat > file` standing in for pacat."""
    out = tmp_path / "pcm.raw"
    monkeypatch.setattr(phone_mic, "spawn_writer", REAL_SPAWN)
    monkeypatch.setattr(phone_mic, "writer_cmd", lambda sink: [sys.executable, "-c",
                        f"import sys,shutil; shutil.copyfileobj(sys.stdin.buffer, open({str(out)!r}, 'wb'))"])
    c = make_client(PHONE_MICS="A")
    with c.websocket_connect("/mic/ws?ch=A") as ws:
        ws.receive_json()
        for i in range(10):
            ws.send_bytes(bytes([i, 0]) * 1280)
    until(lambda: not c.app.state.phone_mics.procs)
    assert out.read_bytes() == b"".join(bytes([i, 0]) * 1280 for i in range(10))


def test_close_writer_kills_a_process_that_ignores_stdin(monkeypatch):
    monkeypatch.setattr(phone_mic, "CLOSE_GRACE_S", 0.2)

    async def go():
        proc = await REAL_SPAWN([sys.executable, "-c", "import time; time.sleep(30)"])
        t = time.monotonic()
        await phone_mic.close_writer(proc)
        return proc, time.monotonic() - t

    proc, took = asyncio.run(go())
    assert proc.returncode is not None and proc.returncode < 0 and took < 5   # killed and reaped



@pytest.mark.skipif(not shutil.which("deno"), reason="needs deno to run the worklet")
def test_worklet_downsampler_and_int16_packing():
    r = subprocess.run(["deno", "run", "--allow-read", str(ROOT / "tests/js/mic_downsample.js"),
                        str(ROOT / "app/face/static/mic-worklet.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "all passed" in r.stdout, r.stdout + r.stderr


@pytest.mark.skipif(not shutil.which("deno"), reason="needs deno to run mic.js")
def test_mic_page_rules():
    r = subprocess.run(["deno", "run", "--allow-read", str(ROOT / "tests/js/mic_page.js"),
                        str(ROOT / "app/face/static/mic.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "all passed" in r.stdout, r.stdout + r.stderr
