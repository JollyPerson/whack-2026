"""The referee's voice: text -> cached 24 kHz mono WAV, behind one provider interface. Owner: D.

Provider.synth(text) -> PCM16 mono bytes at 24 kHz. TTS_PROVIDER picks the chain:
  auto (default)  every configured provider below, in this order; the first that succeeds wins
  eleven          ElevenLabs Text to Dialogue WebSocket, eleven_v4_turbo (v4 Turbo is served only there),
                  one connection per line behind a lock: no keep-alive or reconnect machinery
  eleven_http     ElevenLabs HTTP TTS stream, eleven_multilingual_v2 (Flash v2.5 doesn't normalise numbers)
  espeak          local espeak-ng, if it is on PATH (robotic, but offline and free)
  none            no server voice: /face/tts answers 503 and the browser uses its own speech
The bubble and the banner show the exact text; speakable() rewrites it for the ear only ("27%" -> "27 percent").
ELEVENLABS_API_KEY only ever travels in a request header: it is never logged, cached or returned.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import sys
from array import array
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger("face.tts")
_WS_LOG = logging.getLogger("face.tts.websocket")
_WS_LOG.setLevel(logging.INFO)          # websockets logs handshake headers (the key) at DEBUG: never emit them

RATE = 24000
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache" / "tts"   # cache/ is gitignored
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs premade "George" (British), the voice in their API examples
PROVIDERS = ("eleven", "eleven_http", "espeak")


class TTSError(Exception):
    pass


class Provider:
    name, model, voice = "none", "", ""

    async def synth(self, text: str) -> bytes:
        """PCM16 mono at 24 kHz. Raise on failure: the chain moves on to the next provider."""
        raise NotImplementedError


class ElevenTTD(Provider):
    """Text to Dialogue WebSocket. Protocol (elevenlabs.io/docs/api-reference/text-to-dialogue/ttd-websocket):
    first message {"voices": [id], "voice_settings": {...}}, then {"inputs": [...]}; flush forces audio out,
    close_socket ends the session (server sends is_final, then closes). Audio arrives as base64 chunks."""
    name = "eleven"
    URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"

    def __init__(self, key: str, voice: str, model: str, stability: float, url: str = URL,
                 timeout: float = 15.0, quiet_s: float = 2.0):
        self._key, self.voice, self.model, self.stability = key, voice, model, stability
        self.url, self.timeout, self.quiet_s = url, timeout, quiet_s
        self._lock = asyncio.Lock()     # one line (= one dialogue session) at a time

    async def synth(self, text: str) -> bytes:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosedOK
        query = urlencode({"model_id": self.model, "output_format": f"pcm_{RATE}"})
        pcm = bytearray()
        async with self._lock, asyncio.timeout(self.timeout):
            async with connect(f"{self.url}?{query}", additional_headers={"xi-api-key": self._key},
                               open_timeout=5, close_timeout=1, logger=_WS_LOG) as ws:
                for msg in ({"voices": [self.voice], "voice_settings": {"stability": self.stability}},
                            {"inputs": [{"text": text, "voice_id": self.voice, "new_turn": False}]},
                            {"flush": True}, {"close_socket": True}):
                    await ws.send(json.dumps(msg))
                while True:   # the line ends with is_final or the server closing; silence after audio is a stall
                    try:
                        raw = await (asyncio.wait_for(ws.recv(), self.quiet_s) if pcm else ws.recv())
                    except ConnectionClosedOK:
                        break
                    except TimeoutError:          # never cache half a line: the next provider says all of it
                        raise TTSError(f"stream stalled: no audio for {self.quiet_s:g} s") from None
                    msg = json.loads(raw)
                    if msg.get("error"):
                        raise TTSError(f"{msg.get('error')}: {msg.get('message')}")
                    if msg.get("audio"):
                        pcm += base64.b64decode(msg["audio"])
                    if msg.get("is_final_audio_for_turn") or msg.get("is_final"):
                        break
        if not pcm:
            raise TTSError("no audio")
        return bytes(pcm)


class ElevenHTTP(Provider):
    """POST /v1/text-to-speech/{voice}/stream (elevenlabs.io/docs/api-reference/text-to-speech/stream)."""
    name = "eleven_http"
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"

    def __init__(self, key: str, voice: str, model: str, stability: float, client: httpx.AsyncClient | None = None,
                 timeout: float = 15.0):
        self._key, self.voice, self.model, self.stability = key, voice, model, stability
        self._client, self.timeout = client, timeout

    async def synth(self, text: str) -> bytes:
        self._client = self._client or httpx.AsyncClient()
        r = await self._client.post(self.URL.format(voice=self.voice), params={"output_format": f"pcm_{RATE}"},
                                    headers={"xi-api-key": self._key}, timeout=self.timeout,
                                    json={"text": text, "model_id": self.model,
                                          "voice_settings": {"stability": self.stability}})
        if r.status_code != 200:
            raise TTSError(f"HTTP {r.status_code}: {r.text[:200]}")
        if not r.content:
            raise TTSError("no audio")
        return r.content


class Espeak(Provider):
    name, model, voice = "espeak", "espeak-ng", "en-gb"

    def __init__(self, exe: str, timeout: float = 10.0):
        self.exe, self.timeout = exe, timeout

    async def synth(self, text: str) -> bytes:
        p = await asyncio.create_subprocess_exec(self.exe, "--stdin", "--stdout", "-v", self.voice, "-s", "165",
                                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(p.communicate(text.encode()), self.timeout)
        except TimeoutError:
            raise TTSError(f"espeak-ng hung for {self.timeout:g} s") from None
        finally:
            if p.returncode is None:      # hung (audio backend, speech-dispatcher) or cancelled: never orphan it
                p.kill()
        if p.returncode:
            raise TTSError(f"espeak-ng exited {p.returncode}")
        return await asyncio.to_thread(_pcm24k, out)


def _pcm24k(wav: bytes) -> bytes:
    """A 16-bit PCM WAV (espeak-ng writes 22050 Hz, with a placeholder data size on stdout) -> 24 kHz mono PCM."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise TTSError("not a WAV")
    pos, rate, channels, bits = 12, RATE, 1, 16
    while pos + 8 <= len(wav):
        cid, size = wav[pos:pos + 4], struct.unpack_from("<I", wav, pos + 4)[0]
        if cid == b"fmt ":
            channels, rate = struct.unpack_from("<HI", wav, pos + 10)
            bits = struct.unpack_from("<H", wav, pos + 22)[0]
        elif cid == b"data":
            break
        pos += 8 + size + (size & 1)
    else:
        raise TTSError("WAV has no data chunk")
    if bits != 16:
        raise TTSError(f"{bits}-bit WAV")
    body = wav[pos + 8:pos + 8 + size]           # a placeholder size just runs to the end of the file
    data = array("h", body[:len(body) // 2 * 2])
    if sys.byteorder == "big":
        data.byteswap()
    src = data[::channels]                       # first channel only (espeak is mono anyway)
    if rate != RATE and len(src) > 1:            # linear interpolation is plenty for speech
        step, last = rate / RATE, len(src) - 1
        out = array("h", bytes(2 * int(len(src) * RATE / rate)))
        for i in range(len(out)):
            x = i * step
            j = min(int(x), last)
            k = min(j + 1, last)
            out[i] = int(src[j] + (src[k] - src[j]) * (x - j))
        src = out
    if sys.byteorder == "big":
        src.byteswap()
    return src.tobytes()


def wav_bytes(pcm: bytes, rate: int = RATE) -> bytes:
    """44-byte RIFF header + PCM16 mono."""
    pcm = pcm[:len(pcm) // 2 * 2]
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2,
                       16, b"data", len(pcm)) + pcm


# ----------------------------------------------------------------------------- speakable (audio only)
_CURRENCY = {"£": "pounds", "$": "dollars", "€": "euros"}
_SCALE = {"k": "thousand", "m": "million", "bn": "billion", "tn": "trillion", "trn": "trillion"}
_NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"


def _money(m: re.Match) -> str:
    scale = (m[3] or "").lower()
    return f"{m[2]} {_SCALE.get(scale, scale)} {_CURRENCY[m[1]]}".replace("  ", " ")


def _years(m: re.Match) -> str:
    y1, y2 = int(m[1]), int(m[2]) if len(m[2]) == 4 else int(m[1][:2] + m[2])
    return f"{y1} to {y2 + 100 if y2 < y1 else y2}"   # 1999/00 -> 1999 to 2000


_RULES = [
    (re.compile(rf"([£$€])\s?({_NUM})(?:\s?(bn|trn|tn|m|k|billion|million|thousand|trillion)\b)?", re.I), _money),
    (re.compile(r"\b((?:19|20)\d\d)\s?[/–-]\s?((?:19|20)\d\d|\d\d)\b"), _years),   # 2009/10 -> 2009 to 2010
    (re.compile(rf"({_NUM})\s?(bn|trn|tn)\b"), lambda m: f"{m[1]} {_SCALE[m[2]]}"),
    (re.compile(rf"({_NUM})m\b(?=\s+[a-z])"), r"\1 million"),                     # 1.5m households
    (re.compile(rf"({_NUM})\s?pp\b"), r"\1 percentage points"),
    (re.compile(rf"({_NUM})\s?%"), r"\1 percent"),
    (re.compile(r"%"), " percent"),
    (re.compile(r"\b(\d+)p\b"), r"\1 pence"),
    (re.compile(rf"({_NUM})x\b"), r"\1 times"),
    (re.compile(r"(\d)\+"), r"\1 plus"),
    (re.compile(r"\bvs\.?(?=\s)", re.I), "versus"),
    (re.compile(r"\be\.g\.", re.I), "for example"),
    (re.compile(r"\bi\.e\.", re.I), "that is"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"~\s?(?=\d)"), "about "),
]


def speakable(text: str) -> str:
    """For the voice only: symbols TTS engines misread, spelled out. "£3bn" -> "3 billion pounds"."""
    for pat, rep in _RULES:
        text = pat.sub(rep, text)
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------------------------------------------------------- the cached chain
class TTS:
    """Cache: cache/tts/<sha1(provider|model|voice|speakable)[:20]>.wav. Concurrent requests for one line share
    one synthesis; wav() gives up after miss_wait seconds, but the late result is still cached."""

    def __init__(self, providers: list[Provider], cache_dir: Path = CACHE_DIR, miss_wait: float = 2.5,
                 secrets: tuple[str, ...] = ()):
        self.providers, self.cache_dir, self.miss_wait = providers, Path(cache_dir), miss_wait
        self._secrets = tuple(s for s in secrets if s)
        self._inflight: dict[str, asyncio.Task] = {}

    def url_for(self, text: str) -> str:
        """Relative URL of the line's WAV, or "" when there is no server voice."""
        return f"/face/tts?text={quote(text, safe='')}" if self.providers else ""

    def _path(self, p: Provider, say: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(f'{p.name}|{p.model}|{p.voice}|{say}'.encode()).hexdigest()[:20]}.wav"

    def cached(self, text: str) -> Path | None:
        say = speakable(text)
        return next((path for p in self.providers if (path := self._path(p, say)).exists()), None)

    def prefetch(self, text: str) -> asyncio.Task | None:
        """Start synthesising (pre-synthesis); never waits. None if cached or there is no provider."""
        say = speakable(text)
        if not self.providers or not say or self.cached(text):
            return None
        task = self._inflight.get(say)
        if task is None:
            task = self._inflight[say] = asyncio.create_task(self._make(say))
            task.add_done_callback(lambda _t: self._inflight.pop(say, None))
        return task

    async def wav(self, text: str, wait: float | None = None) -> Path | None:
        """The cached WAV, synthesising it if needed. None on a miss after `wait` s; TTSError if every provider
        failed."""
        task = self.prefetch(text)
        if task is None:
            return self.cached(text)
        try:
            path = await asyncio.wait_for(asyncio.shield(task), self.miss_wait if wait is None else wait)
        except TimeoutError:
            return None
        if path is None:
            raise TTSError("every TTS provider failed")
        return path

    async def _make(self, say: str) -> Path | None:
        for p in self.providers:
            try:
                pcm = await p.synth(say)
            except Exception as e:
                log.warning("tts %s failed: %s", p.name, self._scrub(f"{type(e).__name__}: {e}"))
                continue
            path = self._path(p, say)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".part")
                tmp.write_bytes(wav_bytes(pcm))
                tmp.replace(path)                # atomic: a reader never sees half a file
            except OSError as e:                 # not the provider's fault: paying the next one again can't help
                log.error("tts cache write failed: %s", self._scrub(f"{type(e).__name__}: {e}"))
                return None
            log.info("tts %s: %.1f s of audio for %r", p.name, len(pcm) / (2 * RATE), say[:60])
            return path
        return None

    def _scrub(self, s: str) -> str:
        for secret in self._secrets:
            s = s.replace(secret, "***")
        return s[:300]


def build_tts(env=os.environ) -> TTS:
    """The chain from env: TTS_PROVIDER, ELEVENLABS_API_KEY/VOICE_ID/MODEL/HTTP_MODEL/STABILITY, TTS_MISS_WAIT_S."""
    def num(name: str, default: float) -> float:
        try:
            return float(env.get(name, default))
        except ValueError:
            return default

    key = env.get("ELEVENLABS_API_KEY", "").strip()
    voice = env.get("ELEVENLABS_VOICE_ID", "").strip() or DEFAULT_VOICE
    stability = num("ELEVENLABS_STABILITY", 0.6)
    found: dict[str, Provider] = {}
    if key:
        found["eleven"] = ElevenTTD(key, voice, env.get("ELEVENLABS_MODEL") or "eleven_v4_turbo", stability)
        found["eleven_http"] = ElevenHTTP(key, voice, env.get("ELEVENLABS_HTTP_MODEL") or "eleven_multilingual_v2",
                                          stability)
    if exe := shutil.which("espeak-ng"):
        found["espeak"] = Espeak(exe)
    choice = env.get("TTS_PROVIDER", "auto").strip().lower() or "auto"
    if choice not in ("auto", "none", *PROVIDERS):
        log.error("TTS_PROVIDER=%r is not auto|eleven|eleven_http|espeak|none - using auto", choice)
        choice = "auto"
    names = [n for n in PROVIDERS if n in found] if choice == "auto" else [choice] if choice in found else []
    if choice not in ("auto", "none") and not names:
        log.warning("TTS_PROVIDER=%s is not available (no ELEVENLABS_API_KEY / espeak-ng): no server voice", choice)
    log.info("tts chain: %s", " -> ".join(names) or "none (the browser speaks)")
    return TTS([found[n] for n in names], miss_wait=num("TTS_MISS_WAIT_S", 2.5), secrets=(key,))
