"""A phone's browser as a debater's microphone, no app needed. Owner: B. Included by app/face/routes.py.

GET /mic?ch=A|B     the phone page (static/mic.html + mic.js + mic-worklet.js): getUserMedia -> AudioWorklet ->
                    16 kHz mono s16le frames over a WebSocket. ?agc=0 turns the phone's auto gain off, ?token= is
                    MIC_TOKEN when the server sets one.
WS  /mic/ws?ch=A    binary frames of raw PCM (s16le, 16 kHz, mono, ~80 ms each) -> a pacat (or pw-cat) per connection
                    -> the PulseAudio/PipeWire null sink "whack_phone_A". The ears then record its monitor like any
                    other source: MIC1=whack_phone_A.monitor MIC2=... scripts/ears-two-mics.sh

Off unless PHONE_MICS is set (e.g. PHONE_MICS=A,B or PHONE_MICS=A): only then are the sinks created at start-up
(pactl load-module module-null-sink) and unloaded at shutdown, by module id. A sink that already exists is reused and
left alone. One phone per channel: a second one is refused (close code 4409). MIC_TOKEN, when set, must match ?token=
(4401). A channel that isn't enabled is refused with 4404; the page stops retrying on all three. The same page coming
back (its random ?sid= matches the connection holding the channel) takes the channel over instead: after a Wi-Fi blip
the server may not notice the old socket is dead for ~40 s (ping 20 s + timeout 20 s), so the stale one is closed with
4410 and its writer released. Frames that are empty, odd-length or over MAX_FRAME_BYTES are dropped. A disconnect closes
pacat (then kills it after 2 s) and reaps it.
The connection state shows as a status dot per channel, "phone_A" / "phone_B".

Sinks this server creates carry whack.pid=<its pid>. A sink left behind by a server that died without unloading it
(SIGKILL, crash) is adopted on the next start when that pid is gone, and unloaded at shutdown like a new one.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, WebSocket
from fastapi.responses import FileResponse

from app.schemas import ModuleStatus

log = logging.getLogger("face.phone")

CHANNELS = {"A": "Proposition", "B": "Opposition"}
RATE = 16000
MAX_FRAME_BYTES = 16000          # 0.5 s of s16le mono at 16 kHz; the page sends 80 ms (2560 bytes)
DRAIN_TIMEOUT_S = 2.0            # pacat not taking audio for this long = stuck: end the connection
CLOSE_GRACE_S = 2.0              # after closing pacat's stdin, wait this long before killing it
STATIC = Path(__file__).parent / "static"

REPLACE_TIMEOUT_S = 5.0          # closing a replaced (stale) socket: give up after this; the channel moved on already
SID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

CLOSE_TOKEN, CLOSE_OFF, CLOSE_BUSY, CLOSE_REPLACED, CLOSE_WRITER = 4401, 4404, 4409, 4410, 1011


def sink_name(ch: str) -> str:
    return f"whack_phone_{ch}"


def enabled_channels(env: str | None = None) -> list[str]:
    """PHONE_MICS=A,B -> ['A', 'B']. Anything else in the list is ignored (with a warning)."""
    raw = os.environ.get("PHONE_MICS", "") if env is None else env
    out = []
    for part in raw.replace(" ", ",").split(","):
        ch = part.strip().upper()
        if not ch:
            continue
        if ch in CHANNELS and ch not in out:
            out.append(ch)
        elif ch not in CHANNELS:
            log.warning("PHONE_MICS: ignoring %r (use A, B or A,B)", part)
    return out


# ---------------------------------------------------------------- the two seams tests replace
def run_cmd(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a short command (pactl). Returns (returncode, stdout); a missing binary or a timeout is rc 127 / 124."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip() if r.returncode == 0 else (r.stderr or r.stdout).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out"


def writer_cmd(sink: str) -> list[str] | None:
    """pacat if it exists, else pw-cat. dont-fallback: a missing sink is an error, never the laptop's speakers;
    dont-reconnect: a sink unloaded mid-run ends the writer instead of moving it to the speakers."""
    if shutil.which("pacat"):
        return ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
                f"--device={sink}", "--latency-msec=200", "--client-name=whack-phone-mic",
                "--property=node.dont-reconnect=true", "--property=node.dont-fallback=true"]
    if shutil.which("pw-cat"):
        return ["pw-cat", "--playback", "--raw", "--format=s16", f"--rate={RATE}", "--channels=1",
                f"--target={sink}", "--latency=200ms",
                "-P", "{ node.dont-reconnect = true node.dont-fallback = true }", "-"]
    return None


async def spawn_writer(cmd: list[str]):
    """The per-connection audio writer: a subprocess with stdin = the PCM."""
    return await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)


async def close_writer(proc) -> None:
    """Close stdin (pacat drains and exits), kill it if it hangs, and reap it: no zombies."""
    try:
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), CLOSE_GRACE_S)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
    except asyncio.CancelledError:                   # asyncio's child watcher still reaps it once it is dead
        _kill(proc)
        raise


def _kill(proc) -> None:
    try:
        if proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------- sinks
def sink_names() -> set[str] | None:
    rc, out = run_cmd(["pactl", "list", "short", "sinks"])
    if rc != 0:
        return None
    return {line.split("\t")[1] for line in out.splitlines() if line.count("\t") >= 1}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                          # someone else's process: alive
        return True
    return True


def orphan_module(name: str) -> int | None:
    """The module id of sink `name` when a server of ours made it (whack.pid=N) and that server is gone, else None:
    a sink someone made by hand, or one a server still running owns, is left alone."""
    rc, out = run_cmd(["pactl", "list", "short", "modules"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[1] != "module-null-sink" or not parts[0].strip().isdigit():
            continue
        if f"sink_name={name}" not in parts[2].split():
            continue
        m = re.search(r"whack\.pid=(\d+)", parts[2])
        if m and int(m.group(1)) != os.getpid() and not pid_alive(int(m.group(1))):
            return int(parts[0])
    return None


class Conn:
    """One phone holding a channel: its socket, the page's session id, and its writer (None once released)."""
    __slots__ = ("sock", "sid", "proc")

    def __init__(self, sock, sid: str):
        self.sock, self.sid, self.proc = sock, sid, None


class PhoneMics:
    """Owns the sinks this server created (by module id) and the live writer per channel."""

    def __init__(self, channels: list[str], bus=None):
        self.channels, self.bus = channels, bus
        self.modules: dict[str, int] = {}        # ch -> module id we loaded (and must unload)
        self.ready: dict[str, str] = {}          # ch -> sink name, when the sink exists
        self.active: dict[str, Conn] = {}        # ch -> the connection holding it
        self.procs: set = set()                  # every writer still running, for shutdown
        self.closing: set = set()                # the tasks closing writers of connections that ended

    def status(self, ch: str, state: str, detail: str) -> None:
        if self.bus is not None:
            self.bus.publish(ModuleStatus(module=f"phone_{ch}", state=state, detail=detail), src="face")

    def setup(self) -> None:
        if not self.channels:
            return
        existing = sink_names()
        if existing is None:
            for ch in self.channels:
                log.error("phone mic %s: pactl isn't working, so there is no sink to play the phone into", ch)
                self.status(ch, "down", "pactl not available: no sink for the phone")
            return
        rc, default_before = run_cmd(["pactl", "get-default-sink"])
        for ch in self.channels:
            name = sink_name(ch)
            if name in existing and (orphan := orphan_module(name)) is not None:
                log.warning("phone mic %s: sink %s (module %d) was left by a server that is gone; adopting it "
                            "(unloaded at shutdown)", ch, name, orphan)
                self.modules[ch] = orphan
                self.ready[ch] = name
            elif name in existing:
                log.warning("phone mic %s: sink %s already exists, reusing it (and leaving it at shutdown)", ch, name)
                self.ready[ch] = name
            else:
                rc2, out = run_cmd(["pactl", "load-module", "module-null-sink", f"sink_name={name}",
                                    f'sink_properties="device.description=Phone_mic_{ch}_{CHANNELS[ch]} '
                                    f'whack.pid={os.getpid()}"'])
                if rc2 != 0 or not out.strip().isdigit():
                    log.error("phone mic %s: could not create sink %s: %s", ch, name, out[:200])
                    self.status(ch, "down", f"could not create sink {name}")
                    continue
                self.modules[ch] = int(out.strip())
                self.ready[ch] = name
                log.info("phone mic %s: sink %s (module %s); ears source %s.monitor", ch, name, out.strip(), name)
            self.status(ch, "degraded", f"waiting for a phone: open /mic?ch={ch} (ears: {name}.monitor)")
        # A new sink must never become where the laptop plays: its monitor would then hear the referee and music.
        if rc == 0 and default_before and default_before not in self.ready.values():
            rc3, now = run_cmd(["pactl", "get-default-sink"])
            if rc3 == 0 and now in self.ready.values():
                run_cmd(["pactl", "set-default-sink", default_before])
                log.warning("phone mic: %s had become the default sink; set it back to %s", now, default_before)

    def release(self, proc) -> None:
        """Close a writer in its own task: a connection handler cancelled at shutdown (or by a test client) can't
        leave the writer half closed."""
        async def close():
            try:
                await close_writer(proc)
            finally:
                self.procs.discard(proc)
        self._track(close())

    def _track(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)

    def take_over(self, ch: str, conn: Conn) -> None:
        """The same page reconnected while its old socket still holds the channel (a half-open drop the server hasn't
        noticed yet): release the old writer now, close the old socket in the background, hand the channel over."""
        old = self.active.get(ch)
        if old is not None:
            if old.proc is not None:
                self.release(old.proc)
                old.proc = None
            self._track(asyncio.wait_for(_refuse(old.sock, CLOSE_REPLACED, "replaced by a newer connection from "
                                                 "the same page"), REPLACE_TIMEOUT_S))
        self.active[ch] = conn

    async def teardown(self) -> None:
        await asyncio.gather(*self.closing, *(close_writer(p) for p in list(self.procs)), return_exceptions=True)
        self.procs.clear()
        for ch, mod in self.modules.items():
            rc, out = run_cmd(["pactl", "unload-module", str(mod)])
            if rc != 0:
                log.warning("phone mic %s: unload-module %s failed: %s", ch, mod, out[:200])
        self.modules.clear()
        self.ready.clear()


@asynccontextmanager
async def lifespan(app):
    """Merged into the app's lifespan by include_router: sinks up at start, down at shutdown (only with PHONE_MICS)."""
    mics = PhoneMics(enabled_channels(), getattr(app.state, "bus", None))
    app.state.phone_mics = mics
    mics.setup()                                     # a few quick pactl calls, once; publishes on the loop
    try:
        yield
    finally:
        await mics.teardown()


router = APIRouter(lifespan=lifespan)


@router.get("/mic")
async def mic_page():
    return FileResponse(STATIC / "mic.html")


async def _refuse(sock: WebSocket, code: int, msg: str) -> None:
    """Say why in a JSON frame (the page shows it), then close with an app code the page can act on."""
    try:
        await sock.send_json({"error": msg, "code": code})
        await sock.close(code=code, reason=msg[:120])
    except Exception:                                # the phone already left
        pass


@router.websocket("/mic/ws")
async def mic_ws(sock: WebSocket):
    await sock.accept()
    mics: PhoneMics | None = getattr(sock.app.state, "phone_mics", None)
    ch = (sock.query_params.get("ch") or "").strip().upper()
    want = os.environ.get("MIC_TOKEN", "")
    if want and not hmac.compare_digest(sock.query_params.get("token", "").encode(), want.encode()):
        return await _refuse(sock, CLOSE_TOKEN, "wrong or missing token: open the page with ?token=<MIC_TOKEN>")
    if ch not in CHANNELS:
        return await _refuse(sock, CLOSE_OFF, "unknown channel: use ?ch=A (Proposition) or ?ch=B (Opposition)")
    if mics is None or ch not in mics.ready:
        why = "is not enabled" if mics is None or ch not in mics.channels else "has no sink (see the server log)"
        return await _refuse(sock, CLOSE_OFF, f"phone mic {ch} {why}: start the server with PHONE_MICS=A,B")
    sid = sock.query_params.get("sid", "")
    sid = sid if SID_RE.fullmatch(sid) else ""
    conn = Conn(sock, sid)
    held = mics.active.get(ch)
    if held is not None and not (sid and held.sid == sid):
        return await _refuse(sock, CLOSE_BUSY, f"another phone is already microphone {ch}: stop it first")
    if held is not None:
        log.warning("phone mic %s: the same page reconnected; replacing its stale connection", ch)
    mics.take_over(ch, conn)                         # claimed before the first await: no race with a second phone
    proc, who = None, f"{sock.client.host if sock.client else '?'}"
    try:
        sink = mics.ready[ch]
        names = await asyncio.to_thread(sink_names)
        cmd = writer_cmd(sink)
        if names is not None and sink not in names:
            return await _refuse(sock, CLOSE_WRITER, f"sink {sink} is gone (unloaded?): restart the server")
        if cmd is None:
            return await _refuse(sock, CLOSE_WRITER, "neither pacat nor pw-cat is installed on the server")
        try:
            proc = await spawn_writer(cmd)
        except Exception as e:
            log.exception("phone mic %s: could not start %s", ch, cmd[0])
            return await _refuse(sock, CLOSE_WRITER, f"could not start {cmd[0]}: {e}"[:120])
        mics.procs.add(proc)
        conn.proc = proc
        if mics.active.get(ch) is not conn:          # replaced while starting: take_over couldn't see this writer
            return
        log.info("phone mic %s: connected from %s -> %s (%s)", ch, who, sink, cmd[0])
        mics.status(ch, "up", f"phone connected ({who}) -> {sink}.monitor")
        await sock.send_json({"ok": True, "ch": ch, "side": CHANNELS[ch], "sink": sink, "rate": RATE,
                              "max_frame": MAX_FRAME_BYTES})
        dropped = 0
        while True:
            msg = await sock.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is None:                         # text frames carry nothing the server needs
                continue
            if not data or len(data) > MAX_FRAME_BYTES or len(data) % 2:
                dropped += 1
                if dropped in (1, 100) or dropped % 1000 == 0:
                    log.warning("phone mic %s: dropped %d bad frame(s) (last %d bytes)", ch, dropped, len(data))
                continue
            if mics.active.get(ch) is not conn:      # replaced by the same page's new socket
                break
            if proc.returncode is not None:
                return await _refuse(sock, CLOSE_WRITER, f"{cmd[0]} stopped (rc {proc.returncode}): reconnecting")
            try:
                proc.stdin.write(data)
                await asyncio.wait_for(proc.stdin.drain(), DRAIN_TIMEOUT_S)
            except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError) as e:
                log.warning("phone mic %s: %s not taking audio (%r)", ch, cmd[0], e)
                return await _refuse(sock, CLOSE_WRITER, f"{cmd[0]} stopped taking audio: reconnecting")
    except Exception as e:                           # the phone vanished mid-send, etc.
        if type(e).__name__ not in ("WebSocketDisconnect", "ClientDisconnected"):
            log.warning("phone mic %s: connection ended: %r", ch, e)
    finally:
        owner = mics.active.get(ch) is conn
        if owner:
            del mics.active[ch]
        if conn.proc is not None:                    # no await in here: a cancelled handler still cleans up
            mics.release(conn.proc)
            conn.proc = None
        if proc is not None:
            log.info("phone mic %s: disconnected%s", ch, "" if owner else " (replaced)")
            if owner and ch in mics.ready:
                mics.status(ch, "degraded", f"phone disconnected: waiting for a phone on /mic?ch={ch}")
