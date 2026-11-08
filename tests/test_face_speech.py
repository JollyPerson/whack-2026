"""Face voice + Mate Engine bridge: TTS cache/providers, /face/tts, /face/mate/next, dances, pre-synthesis.
All offline: fake providers, a local fake ElevenLabs WebSocket, httpx.MockTransport."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.bus import Bus
from app.face import run as face_run, speech, tts as ttsmod
from app.face.speech import state_for
from app.face.tts import TTS, ElevenHTTP, ElevenTTD, Espeak, Provider, TTSError, build_tts, speakable
from app.main import build_app
from app.schemas import DirectorDecision, Interjection
from tests.test_main import make_client  # noqa: F401  (fixture)

KEYS = {"op", "id", "text", "audio", "title", "on", "clear"}
SECRET = "sk-SENTINEL-7f3a9c"
_POOL = ThreadPoolExecutor(max_workers=4)


@pytest.fixture(autouse=True)
def _no_real_voice(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


class Fake(Provider):
    def __init__(self, name="fake", voice="v", delay=0.0, fail=False):
        self.name, self.model, self.voice, self.delay, self.fail, self.calls = name, "m", voice, delay, fail, []

    async def synth(self, text):
        self.calls.append(text)
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TTSError("boom")
        return b"\x01\x00" * 2400                  # 0.1 s


def header(wav: bytes) -> tuple:
    riff, size, wave, fmt, _, pcm, ch, rate, brate, align, bits, data, n = struct.unpack("<4sI4s4sIHHIIHH4sI", wav[:44])
    assert (riff, wave, fmt, data, size, n) == (b"RIFF", b"WAVE", b"fmt ", b"data", len(wav) - 8, len(wav) - 44)
    assert brate == rate * ch * bits // 8 and align == ch * bits // 8
    return pcm, ch, rate, bits


# ----------------------------------------------------------------------------- cache + chain
async def test_cache_wav_header_and_speakable_audio_only(tmp_path):
    fake = Fake()
    tts = TTS([fake], cache_dir=tmp_path)
    path = await tts.wav("Bus use fell 27% from 2009/10.")
    assert header(path.read_bytes()) == (1, 1, 24000, 16)
    assert fake.calls == ["Bus use fell 27 percent from 2009 to 2010."]
    assert await tts.wav("Bus use fell 27% from 2009/10.") == path and len(fake.calls) == 1   # cache hit
    assert re.fullmatch(r"[0-9a-f]{20}\.wav", path.name)
    other = TTS([Fake(voice="w")], cache_dir=tmp_path)
    await other.wav("Bus use fell 27% from 2009/10.")
    assert other.cached("Bus use fell 27% from 2009/10.") != path                              # key has the voice


async def test_concurrent_requests_share_one_synthesis(tmp_path):
    fake = Fake(delay=0.05)
    tts = TTS([fake], cache_dir=tmp_path)
    a, b = await asyncio.gather(tts.wav("Point of fact."), tts.wav("Point of fact."))
    assert a == b and fake.calls == ["Point of fact."]


async def test_miss_wait_gives_up_but_the_late_result_is_cached(tmp_path):
    fake = Fake(delay=0.2)
    tts = TTS([fake], cache_dir=tmp_path, miss_wait=0.02)
    assert await tts.wav("Slow line.") is None
    await asyncio.sleep(0.3)
    assert tts.cached("Slow line.") and await tts.wav("Slow line.") == tts.cached("Slow line.")
    assert len(fake.calls) == 1


async def test_chain_falls_through_and_fails_loudly(tmp_path):
    bad, good = Fake("bad", fail=True), Fake("good")
    path = await TTS([bad, good], cache_dir=tmp_path).wav("Hello.")
    assert path and bad.calls == good.calls == ["Hello."]
    with pytest.raises(TTSError):
        await TTS([Fake("bad", fail=True)], cache_dir=tmp_path).wav("Other.")


def test_speakable_rewrites_symbols_for_the_ear():
    assert speakable("It cost £3bn, up 27% since 2009/10.") == \
        "It cost 3 billion pounds, up 27 percent since 2009 to 2010."
    assert speakable("£300 a week vs $1.2m") == "300 pounds a week versus 1.2 million dollars"
    assert speakable("1999/00 to 2010-2015: 1.5m households, 3pp") == \
        "1999 to 2000 to 2010 to 2015: 1.5 million households, 3 percentage points"
    assert speakable("£5, then 50p") == "5 pounds, then 50 pence"


def test_build_tts_from_env(monkeypatch):
    monkeypatch.setattr(ttsmod.shutil, "which", lambda exe: None)
    names = lambda **env: [p.name for p in build_tts(env).providers]   # noqa: E731
    assert names() == [] and names(TTS_PROVIDER="eleven") == []
    assert names(ELEVENLABS_API_KEY="k") == ["eleven", "eleven_http"]
    assert names(ELEVENLABS_API_KEY="k", TTS_PROVIDER="eleven_http") == ["eleven_http"]
    assert names(ELEVENLABS_API_KEY="k", TTS_PROVIDER="none") == []
    assert names(ELEVENLABS_API_KEY="k", TTS_PROVIDER="elevn") == ["eleven", "eleven_http"]      # typo: loud + auto
    p = build_tts({"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "vox", "ELEVENLABS_STABILITY": "0.8",
                   "TTS_MISS_WAIT_S": "1.5"})
    (ttd, http), wait = p.providers, p.miss_wait
    assert (ttd.model, http.model, ttd.voice, ttd.stability, wait) == ("eleven_v4_turbo", "eleven_multilingual_v2",
                                                                       "vox", 0.8, 1.5)
    monkeypatch.setattr(ttsmod.shutil, "which", lambda exe: "/usr/bin/espeak-ng")
    assert names(ELEVENLABS_API_KEY="k") == ["eleven", "eleven_http", "espeak"]
    assert names(TTS_PROVIDER="espeak") == ["espeak"]


# ----------------------------------------------------------------------------- providers
async def test_eleven_ttd_against_a_fake_websocket():
    from websockets.asyncio.server import serve
    got: dict = {"msgs": []}
    chunks = [b"\x01\x02" * 100, b"\x03\x04" * 50, b"\x05\x06"]

    async def handler(ws):
        got["path"], got["headers"] = ws.request.path, ws.request.headers
        for _ in range(4):
            got["msgs"].append(json.loads(await ws.recv()))
        for c in chunks:
            await ws.send(json.dumps({"audio": base64.b64encode(c).decode(), "alignment": None}))
        await ws.send(json.dumps({"is_final_audio_for_turn": True}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=_quiet()) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/v1/text-to-dialogue/stream-input"
        p = ElevenTTD(SECRET, "vox", "eleven_v4_turbo", 0.6, url=url)
        assert await p.synth("Some context: it fell 27 percent.") == b"".join(chunks)
    first, inputs, flush, close = got["msgs"]
    assert close == {"close_socket": True}
    assert first == {"voices": ["vox"], "voice_settings": {"stability": 0.6}}            # no key in any message
    assert SECRET not in json.dumps(got["msgs"]) and got["headers"]["xi-api-key"] == SECRET
    (line,) = inputs["inputs"]
    assert line["text"] == "Some context: it fell 27 percent." and line["voice_id"] == "vox"
    assert flush == {"flush": True}
    q = parse_qs(urlsplit(got["path"]).query)
    assert q == {"model_id": ["eleven_v4_turbo"], "output_format": ["pcm_24000"]}


async def test_eleven_ttd_quiet_gap_and_error():
    from websockets.asyncio.server import serve
    mode = {}

    async def handler(ws):
        for _ in range(4):
            await ws.recv()
        if mode["error"]:
            await ws.send(json.dumps({"message": "quota exceeded", "error": "quota_exceeded", "code": 1008}))
        else:
            await ws.send(json.dumps({"audio": base64.b64encode(b"\x07\x08").decode()}))   # and no end marker
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=_quiet()) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        mode["error"] = False                  # audio, then silence with the socket open: a stall, not the end
        with pytest.raises(TTSError, match="stalled"):
            await ElevenTTD("k", "v", "m", 0.5, url=url, quiet_s=0.1).synth("Hi.")
        mode["error"] = True
        with pytest.raises(TTSError, match="quota_exceeded"):
            await ElevenTTD("k", "v", "m", 0.5, url=url).synth("Hi.")


async def test_a_stalled_eleven_stream_is_not_cached_as_the_line(tmp_path):
    """Venue wifi stalls mid-line: the first chunk lands, then nothing for longer than quiet_s. That half line must
    not become the line's cached audio for good: the chain moves on, and later requests hit the complete line."""
    from websockets.asyncio.server import serve
    connections = []

    async def handler(ws):
        connections.append(ws)
        for _ in range(4):
            await ws.recv()
        await ws.send(json.dumps({"audio": base64.b64encode(b"\x07\x08" * 100).decode()}))   # then the stall
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0, logger=_quiet()) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        eleven, http = ElevenTTD("k", "v", "m", 0.5, url=url, quiet_s=0.2), Fake("eleven_http")
        tts = TTS([eleven, http], cache_dir=tmp_path)
        path = await tts.wav("Point of fact.")
        assert path == tts._path(http, "Point of fact.") and not tts._path(eleven, "Point of fact.").exists()
        assert len(path.read_bytes()) == 44 + 4800 and http.calls == ["Point of fact."]
        assert await tts.wav("Point of fact.") == path and len(connections) == 1                # a hit from now on


async def test_eleven_http_against_a_mock_transport():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, content=b"\x01\x00" * 10) if req.headers["xi-api-key"] == SECRET \
            else httpx.Response(401, json={"detail": "invalid key"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = ElevenHTTP(SECRET, "vox", "eleven_multilingual_v2", 0.6, client=client)
    assert await p.synth("Point of fact.") == b"\x01\x00" * 10
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/text-to-speech/vox/stream"
    assert req.url.params["output_format"] == "pcm_24000"
    assert json.loads(req.content) == {"text": "Point of fact.", "model_id": "eleven_multilingual_v2",
                                       "voice_settings": {"stability": 0.6}}
    with pytest.raises(TTSError, match="HTTP 401"):
        await ElevenHTTP("wrong", "vox", "m", 0.6, client=client).synth("x")


async def test_espeak_wav_is_converted_to_24k_mono(tmp_path):
    exe = tmp_path / "espeak-ng"   # fake: 22050 Hz WAV with espeak's placeholder data size, 0.5 s
    exe.write_text(f"#!{sys.executable}\nimport struct, sys\nsys.stdin.read()\npcm = b'\\x10\\x00' * 11025\n"
                   "sys.stdout.buffer.write(struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 0x7ffff024, b'WAVE',"
                   " b'fmt ', 16, 1, 1, 22050, 44100, 2, 16, b'data', 0x7ffff000) + pcm)\n")
    exe.chmod(0o755)
    pcm = await Espeak(str(exe)).synth("Hello.")
    assert len(pcm) == 2 * 12000 and set(pcm[i:i + 2] for i in range(0, len(pcm), 2)) == {b"\x10\x00"}


# ----------------------------------------------------------------------------- /face/tts
def _with_tts(c, tmp_path, providers, miss_wait=2.5) -> TTS:
    tts = state_for(c.app.state.bus).tts = TTS(providers, cache_dir=tmp_path / "tts", miss_wait=miss_wait)
    return tts


def test_tts_route_503_200_504_502(make_client, tmp_path):
    c = make_client()
    _with_tts(c, tmp_path, [])
    assert c.get("/face/tts", params={"text": "Hello."}).status_code == 503
    _with_tts(c, tmp_path, [Fake()])
    r = c.get("/face/tts", params={"text": "Hello."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and header(r.content) == (1, 1, 24000, 16)
    _with_tts(c, tmp_path, [Fake("slow", delay=0.3)], miss_wait=0.02)
    assert c.get("/face/tts", params={"text": "Slow."}).status_code == 504
    _with_tts(c, tmp_path, [Fake("bad", fail=True)])
    assert c.get("/face/tts", params={"text": "Broken."}).status_code == 502
    assert c.get("/face/tts").status_code == 422


async def test_the_elevenlabs_key_never_reaches_responses_or_logs(tmp_path, monkeypatch, caplog):
    """Both ElevenLabs providers fail with errors that echo the key; DEBUG logging is on everywhere."""
    from websockets.asyncio.server import serve
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("ELEVENLABS_API_KEY", SECRET)
    monkeypatch.setenv("TTS_PROVIDER", "auto")

    async def handler(ws):
        for _ in range(4):
            await ws.recv()
        await ws.send(json.dumps({"error": "auth", "message": f"bad key {SECRET}", "code": 1008}))

    echo = httpx.MockTransport(lambda req: httpx.Response(401, text=f"bad key {req.headers['xi-api-key']}"))
    app = build_app("mock", log_dir=tmp_path)
    async with serve(handler, "127.0.0.1", 0, logger=_quiet()) as server:
        tts = state_for(app.state.bus).tts
        tts.providers = ttd, http = tts.providers[:2]          # (drop espeak-ng if this machine has it)
        tts.cache_dir = tmp_path / "tts"
        ttd.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        http._client = httpx.AsyncClient(transport=echo)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/face/tts", params={"text": "Hello."})
            other = [await c.get(u) for u in ("/health", "/face/mate/next", "/debug/history")]
    assert r.status_code == 502
    for resp in [r, *other]:
        assert SECRET not in resp.text
    assert "tts eleven failed" in caplog.text and "tts eleven_http failed" in caplog.text
    assert SECRET not in caplog.text and "***" in caplog.text


def _quiet() -> logging.Logger:
    lg = logging.getLogger("tests.fake_ws")   # the fake server would log the key it received at DEBUG
    lg.setLevel(logging.WARNING)
    return lg


# ----------------------------------------------------------------------------- /face/mate/next
def poll(c, after, epoch, wait=0):
    r = c.get("/face/mate/next", params={"after": after, "epoch": epoch, "wait": wait})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"v", "epoch", "cursor", "cmds"} and body["v"] == 1
    assert re.fullmatch(r"[0-9a-f]{8}", body["epoch"])
    for cmd in body["cmds"]:
        assert set(cmd) == KEYS and None not in cmd.values(), cmd     # JsonUtility: fixed keys, never null
    return body


def resync(c):
    b = poll(c, -1, "")
    assert b["cmds"] == [] and 0 <= b["cursor"] <= c.get("/health").json()["seq"]
    return b["cursor"], b["epoch"]


def test_first_poll_and_restart_resync_without_replay(make_client, tmp_path):
    c = make_client()
    _with_tts(c, tmp_path, [Fake()])
    c.post("/publish", json={"event": "Interjection", "id": "i-0001", "text": "Point of fact."})
    cur, ep = resync(c)                                        # after=-1: cursor only, the line is history
    assert poll(c, cur, ep)["cmds"] == []
    stale = poll(c, cur - 1, "deadbeef")                       # a cursor from an older server: resync
    assert stale["cmds"] == [] and stale["epoch"] == ep and stale["cursor"] >= cur
    ahead = poll(c, cur + 50, ep)                              # ahead of this server: it restarted
    assert ahead["cmds"] == [] and cur <= ahead["cursor"] < cur + 50
    assert poll(c, cur - 1, ep)["cmds"][0]["op"] == "say"      # same epoch + valid cursor: delivered


def test_events_map_to_commands(make_client, tmp_path):
    c = make_client()
    tts = _with_tts(c, tmp_path, [Fake()])
    cur, ep = resync(c)
    text = "Some context: GOV.UK notes it fell 27% from 2009/10."
    for ev in [{"event": "Interjection", "id": "i-0003", "text": text},
               {"event": "Interjection", "id": "i-0002", "text": "Too old.", "ts": time.time() - 10},
               {"event": "TTSState", "speaking": True, "interjection_id": "i-0003"},
               {"event": "TTSState", "speaking": False},
               {"event": "Control", "flag": "tts", "value": False},
               {"event": "Control", "flag": "mute", "value": True},
               {"event": "Control", "flag": "mute", "value": False},
               {"event": "Control", "flag": "director", "value": "off"},
               {"event": "Control", "flag": "aggr", "value": 4},
               {"event": "Utterance", "speaker": "A", "text": "ignored"}]:
        assert c.post("/publish", json=ev).status_code == 200
    body = poll(c, cur, ep)
    blank = {"id": "", "text": "", "audio": "", "title": "", "on": False, "clear": False}
    assert body["cmds"] == [
        {**blank, "op": "say", "id": "i-0003", "text": text, "audio": tts.url_for(text)},
        {**blank, "op": "talk", "id": "i-0003", "on": True},
        {**blank, "op": "talk"},
        {**blank, "op": "stop"},                               # tts off: mouth shut, bubble stays
        {**blank, "op": "stop", "clear": True},                # mute
        {**blank, "op": "stop", "clear": True},                # director off
    ]
    assert body["cmds"][0]["audio"].startswith("/face/tts?text=Some%20context%3A") and body["cursor"] > cur
    assert c.get(body["cmds"][0]["audio"]).status_code == 200  # the URL ME gets is the one the browser plays
    assert poll(c, body["cursor"], ep)["cmds"] == []           # cursor moves on: nothing twice
    c.post("/session", json={"motion": "m"})
    assert poll(c, body["cursor"], ep)["cmds"] == [{**blank, "op": "stop", "clear": True}]   # SessionStart


def test_say_audio_is_empty_without_a_server_voice(make_client, tmp_path):
    c = make_client()
    _with_tts(c, tmp_path, [])
    cur, ep = resync(c)
    c.post("/publish", json={"event": "Interjection", "id": "i-0009", "text": "Point of fact."})
    (say,) = poll(c, cur, ep)["cmds"]
    assert say["op"] == "say" and say["audio"] == ""


def test_long_poll_returns_as_soon_as_something_happens(make_client, tmp_path):
    c = make_client()
    _with_tts(c, tmp_path, [Fake()])
    cur, ep = resync(c)
    t0 = time.monotonic()
    fut = _POOL.submit(poll, c, cur, ep, 10)
    time.sleep(0.3)
    c.post("/publish", json={"event": "Utterance", "speaker": "A", "text": "not for ME"})
    time.sleep(0.2)
    assert not fut.done()                                      # irrelevant events don't end the wait
    c.post("/publish", json={"event": "TTSState", "speaking": True, "interjection_id": "i-1"})
    body = fut.result(timeout=5)
    assert [x["op"] for x in body["cmds"]] == ["talk"] and time.monotonic() - t0 < 5
    t0 = time.monotonic()
    assert poll(c, body["cursor"], ep, wait=1)["cmds"] == [] and time.monotonic() - t0 >= 0.9   # wait expires


def test_dance_routes_queue_commands_for_the_avatar(make_client, tmp_path):
    c = make_client()
    cur, ep = resync(c)
    fut = _POOL.submit(poll, c, cur, ep, 10)
    time.sleep(0.3)
    assert c.post("/face/mate/dance", json={"title": "PET_DANCING_01"}).json()["ok"] is True
    assert fut.result(timeout=5)["cmds"] == [{"op": "dance", "id": "", "text": "", "audio": "",
                                              "title": "PET_DANCING_01", "on": False, "clear": False}]
    c.post("/face/mate/dance")
    c.post("/face/mate/dance/stop")
    assert [(x["op"], x["title"]) for x in poll(c, cur, ep)["cmds"]] == [("dance", ""), ("dance_stop", "")]
    assert poll(c, cur, ep)["cmds"] == []                      # delivered once
    assert "dance" not in c.get("/debug/history").text.lower()  # not bus events: never logged or replayed


@pytest.mark.skipif(not shutil.which("deno"), reason="needs deno to run app.js")
def test_app_js_voice_rules():
    """TTSState true only once audible, false only after the sound stopped, the watchdog never cuts a WAV before its
    own end, fallback to browser speech, mute, never-audible lines (tests/js/face_voice.js)."""
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(["deno", "run", "--allow-read", str(root / "tests/js/face_voice.js"),
                        str(root / "app/face/static/app.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "all passed" in r.stdout, r.stdout + r.stderr


async def test_a_poll_whose_client_hung_up_ends_and_takes_nothing(tmp_path):
    """Mate Engine killed mid-poll (or its request timed out): the server doesn't cancel the handler, so the dead
    poll must notice the disconnect, drop its subscription and leave queued dances for the next, live poll."""
    app = build_app("mock", log_dir=tmp_path)
    bus, hang_up, sent = app.state.bus, asyncio.Event(), []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        first = (await c.get("/face/mate/next", params={"after": -1})).json()
        msgs = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            if msgs:
                return msgs.pop()
            await hang_up.wait()
            return {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)

        query = f"after={first['cursor']}&epoch={first['epoch']}&wait=20".encode()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": "/face/mate/next", "raw_path": b"/face/mate/next", "query_string": query, "headers": [],
                 "client": ("127.0.0.1", 1), "server": ("t", 80), "root_path": ""}
        dead = asyncio.create_task(app(scope, receive, send))
        for _ in range(100):
            if any(s.name == "face:mate" for s in bus.subs):
                break
            await asyncio.sleep(0.01)
        assert [s.name for s in bus.subs] == ["face:mate"] and state_for(bus).polls == 1
        hang_up.set()
        await asyncio.wait_for(dead, 2)                        # not after the 20 s wait
        assert bus.subs == [] and state_for(bus).polls == 0
        await c.post("/face/mate/dance", json={"title": "PET_DANCING_01"})
        live = (await c.get("/face/mate/next", params={"after": first["cursor"], "epoch": first["epoch"],
                                                       "wait": 1})).json()
    assert [(x["op"], x["title"]) for x in live["cmds"]] == [("dance", "PET_DANCING_01")]


# ----------------------------------------------------------------------------- run.py (FACE=real)
async def test_presynthesis_makes_the_spoken_line_a_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_PRESYNTH", "candidates")
    bus = Bus()
    fake = Fake()
    st = state_for(bus)
    st.tts = TTS([fake], cache_dir=tmp_path)
    task = asyncio.create_task(face_run.run(bus))
    await asyncio.sleep(0)
    bus.publish(DirectorDecision(key="k", action="skipped", text="Never said."))
    bus.publish(DirectorDecision(key="k", action="candidate", text="Some context: it fell 27%."))
    bus.publish(DirectorDecision(key="k", action="held", text="Some context: it fell 27%."))
    for _ in range(100):
        if st.tts.cached("Some context: it fell 27%."):
            break
        await asyncio.sleep(0.01)
    assert fake.calls == ["Some context: it fell 27 percent."]
    assert await st.tts.wav("Some context: it fell 27%.") and len(fake.calls) == 1        # the Interjection: a hit
    bus.publish(Interjection(id="i-1", text="A new line."))
    for _ in range(100):
        if len(fake.calls) == 2:
            break
        await asyncio.sleep(0.01)
    assert fake.calls[-1] == "A new line."
    task.cancel()


async def test_default_presynthesis_spends_credits_only_on_spoken_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("TTS_PRESYNTH", raising=False)     # default "lines": a free plan is 10,000 characters a month
    bus = Bus()
    fake = Fake()
    st = state_for(bus)
    st.tts = TTS([fake], cache_dir=tmp_path)
    task = asyncio.create_task(face_run.run(bus))
    await asyncio.sleep(0)
    bus.publish(DirectorDecision(key="k", action="candidate", text="Some context: it fell 27%."))
    bus.publish(Interjection(id="i-1", text="Point of fact."))
    for _ in range(100):
        if fake.calls:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert fake.calls == ["Point of fact."]
    task.cancel()


@pytest.mark.parametrize("raw,mode", [(None, "lines"), ("candidates", "candidates"), ("OFF", "off"), ("bogus", "lines")])
def test_presynth_mode_parsing(monkeypatch, raw, mode):
    if raw is None:
        monkeypatch.delenv("TTS_PRESYNTH", raising=False)
    else:
        monkeypatch.setenv("TTS_PRESYNTH", raw)
    assert face_run.presynth_mode() == mode
    assert face_run.line_of(Interjection(id="i", text="x"), "off") == ""


def test_mate_status_up_on_poll_down_after_silence_only_on_change():
    bus = Bus()
    st = state_for(bus)
    sub = bus.subscribe("t", {"ModuleStatus"})
    face_run.watch_mate(bus, st, 100.0)
    assert sub.q.empty()                                       # Mate Engine never connected: no dot at all
    st.seen = 100.0
    for now in (100.5, 101.0):
        face_run.watch_mate(bus, st, now)
    st.polls = 1                                               # a long-poll in progress counts as alive
    face_run.watch_mate(bus, st, 120.0)
    st.polls = 0
    face_run.watch_mate(bus, st, 100.0 + speech.MATE_DOWN_S + 0.1)
    face_run.watch_mate(bus, st, 200.0)
    got = [(e.module, e.state) for e in [sub.q.get_nowait() for _ in range(sub.q.qsize())]]
    assert got == [("mate", "up"), ("mate", "down")]
