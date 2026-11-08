"""Wiring: module registry, supervisor, websocket fan-out, HTTP integration endpoints. Owner: B.

Three ways for a teammate's part to join the bus (pick whatever fits your design):
  1. In-process Python:   app/<role>/run.py with `async def run(bus)` or `def run(bus)` (lowest latency);
                           returning is fine (one-shot setup), raising restarts it with backoff
  2. Any language, WS:     connect to /ws, send an event (or {"op":"publish","event":{...}}), receive every
                           event (optionally {"op":"subscribe","events":["Claim"]} first)
  3. Any language, HTTP:   POST /publish  with one event or a list of events
Role implementations are chosen per profile and can be overridden per role with env vars:
  EARS=real|mock|off|external|some.module:function   (same for BRAIN, EVIDENCE, FACE)
  BOTS=real                                           the side bots (app/bots): no profile starts them
  JUDGE=real                                          the post-debate summary + verdict (app/judge): likewise
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import importlib
import inspect
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from app.bus import MAX_GAP_S, SUB_OWNER, TIME_FIELDS, Bus, replay
from app.config import FLAGS
from app.schemas import SCHEMA_VERSION, Control, ModuleStatus, SessionStart, parse_event

log = logging.getLogger("main")
ROOT = Path(__file__).resolve().parent.parent

MODULES = {   # role -> impl -> "module:function"; imported lazily so a broken import only kills its role
    "ears":     {"real": "app.ears.run:run",     "mock": "app.mocks.mock_ears:run"},
    "brain":    {"real": "app.brain.run:run",    "mock": "app.mocks.mock_brain:run"},
    "evidence": {"real": "app.evidence.run:run", "mock": "app.mocks.mock_evidence:run"},
    "face":     {"real": "app.face.run:run"},
    "bots":     {"real": "app.bots.run:run"},        # in no profile: runs only with BOTS=real
    "judge":    {"real": "app.judge.run:run"},       # in no profile: runs only with JUDGE=real
}
PROFILES = {
    "live":   dict(ears="real", brain="real", evidence="real", face="real"),
    "drive":  dict(ears="mock", brain="real", evidence="real", face="real"),   # recorded debate -> real pipeline
    "mock":   dict(ears="mock", brain="mock", evidence="mock", face="off"),    # zero API keys
    "dev-a":  dict(ears="real", brain="off",  evidence="off",  face="off"),
    "dev-b":  dict(ears="mock", brain="real", evidence="mock", face="off"),
    "dev-c":  dict(ears="mock", brain="mock", evidence="real", face="off"),
    "dev-d":  dict(ears="mock", brain="mock", evidence="mock", face="real"),
    "replay": dict(ears="off",  brain="off",  evidence="off",  face="real"),   # + --file
}


KEYWORDS = ("real", "mock", "off", "external")


def norm_choice(choice: str) -> str:
    c = choice.strip()
    return c.lower() if c.lower() in KEYWORDS else c


def resolve(role: str, choice: str) -> str | None:
    """'real'/'mock' -> registry entry; 'off'/'external' -> nothing; 'pkg.mod:fn' -> as given; 'pkg.mod' ->
    pkg.mod:run. Anything else raises ValueError: a typo must not silently run nothing."""
    choice = norm_choice(choice)
    if choice in ("off", "external", ""):
        return None
    if ":" in choice:
        return choice
    if choice in MODULES.get(role, {}):
        return MODULES[role][choice]
    if "." in choice:
        return f"{choice}:run"
    raise ValueError(f"unknown {role.upper()}={choice!r} - use {'|'.join(MODULES.get(role, {}))}|off|external"
                     f"|pkg.module:function")


def load(spec: str):
    mod, fn = spec.split(":")
    return getattr(importlib.import_module(mod), fn)


HEALTHY_RUN_S = 30.0   # an attempt that ran this long before crashing restarts after 1 s again, not the backoff


async def _restart_after_crash(bus: Bus, name: str, e: Exception, owner: object, started: float, delay: float) -> float:
    """Red dot, close the subscriptions the crashed attempt opened (their queues would fill up forever and look
    'too slow'), wait, and return the next backoff."""
    log.exception("module %s crashed", name)
    bus.publish(ModuleStatus(module=name, state="down", detail=repr(e)[:200]), src="main")
    bus.close_owned(owner)
    if time.monotonic() - started >= HEALTHY_RUN_S:
        delay = 1.0
    await asyncio.sleep(delay)
    return min(delay * 2, 10.0)


async def supervise(bus: Bus, name: str, spec: str, served: str = ""):
    """Run one role. Returning = done (one-shot setup, finished fixture): stays green, never re-run.
    `served` names what the server already serves for this role (routes/static) if run.py is missing."""
    delay, mod = 1.0, spec.split(":")[0]
    while True:
        owner, started = object(), time.monotonic()
        token = SUB_OWNER.set(owner)                 # tags the subs of this attempt and of the tasks it spawns
        try:
            fn = load(spec)
            bus.publish(ModuleStatus(module=name, state="up", detail=spec), src="main")
            res = fn(bus)                            # async def run(bus) or plain def run(bus)
            if inspect.isawaitable(res):
                await res
            bus.publish(ModuleStatus(module=name, state="up", detail=f"{spec} returned (finished or running in "
                                                                      f"the background)"), src="main")
            return
        except asyncio.CancelledError:
            raise
        except ModuleNotFoundError as e:
            if e.name and (mod == e.name or mod.startswith(e.name + ".")):   # app.<role>.run not written
                alt = ("mock|" if "mock" in MODULES.get(name, {}) else "") + "external|off"
                bus.publish(ModuleStatus(module=name, state="up" if served else "down",
                                         detail=f"{served} (no {mod})" if served else
                                         f"{mod} not found - use {name.upper()}={alt}"), src="main")
                return
            if e.name and not e.name.startswith("app."):   # a package isn't installed: retrying can't help
                log.error("module %s: missing dependency %r - add it to requirements/<role>.txt and rerun ./run.sh",
                          name, e.name)
                bus.publish(ModuleStatus(module=name, state="down", detail=f"missing dependency {e.name!r}: add it "
                                         f"to requirements/<role>.txt and rerun ./run.sh"), src="main")
                return
            delay = await _restart_after_crash(bus, name, e, owner, started, delay)
        except Exception as e:                       # crash -> red dot -> restart with backoff
            delay = await _restart_after_crash(bus, name, e, owner, started, delay)
        finally:
            SUB_OWNER.reset(token)


ONE_SHOT_FLAGS = {"force_next", "veto", "time"}   # commands, never kept in FLAGS; every other flag is state


async def flags_watcher(bus: Bus):
    async for ev in bus.subscribe("flags", {"Control"}):
        if ev.flag not in ONE_SHOT_FLAGS:            # the JSON form: a NaN would 500 /health and break every Snapshot
            FLAGS[ev.flag] = ev.model_dump(mode="json")["value"]


async def loop_lag(bus: Bus, tick: float = 0.1, warn: float = 0.05, recover: int = 20):
    """Catches blocking calls in the shared loop: degraded on a stall, back to up after `recover` clean ticks."""
    bad = clean = 0
    while True:
        t = time.perf_counter()
        await asyncio.sleep(tick)
        lag = time.perf_counter() - t - tick
        if lag > warn:
            bus.publish(ModuleStatus(module="loop", state="degraded", detail=f"lag {lag*1000:.0f}ms"), src="main")
            bad, clean = 1, 0
        elif bad and (clean := clean + 1) >= recover:
            bus.publish(ModuleStatus(module="loop", state="up", detail="lag ok"), src="main")
            bad = 0


CLOCK_SKEW_S = 120.0   # an external time this far from the server clock is stream-relative or skewed


def _publish_payload(bus: Bus, payload: Any, src: str) -> list[dict]:
    """Validate the whole batch before publishing any of it (a 422 never half-commits). Times far from the
    server clock (stream-relative seconds, a skewed laptop) become 0 = unknown, so consumers use the bus ts."""
    evs, now = [parse_event(i) for i in (payload if isinstance(payload, list) else [payload])], time.time()
    for ev in evs:
        for f in TIME_FIELDS:
            v = getattr(ev, f, None)
            if isinstance(v, float) and v and abs(v - now) > CLOCK_SKEW_S:
                setattr(ev, f, 0.0)
    return [bus.publish(e, src=src).model_dump(mode="json") for e in evs]


def _ws_message(bus: Bus, sub, m: Any) -> None:
    """One client frame. A bare event (or a JSON array of them) is a publish, exactly like POST /publish."""
    if isinstance(m, list) or (isinstance(m, dict) and "op" not in m and "event" in m):
        m = {"op": "publish", "event": m}
    if not isinstance(m, dict):
        raise ValueError('expected a JSON object: an event, or {"op": "publish"|"session_start"|"control"|"subscribe"}')
    op, body = m.get("op"), {k: v for k, v in m.items() if k != "op"}
    if op == "publish":
        _publish_payload(bus, m["event"], src=m.get("src", "ws"))
    elif op == "session_start":
        bus.publish(SessionStart.model_validate(body), src="dashboard")
    elif op == "control":
        bus.publish(Control.model_validate(body), src="dashboard")
    elif op == "subscribe":
        names = m.get("events") or []
        if isinstance(names, str):                   # "Claim" would otherwise filter on {'C', 'l', 'a', 'i', 'm'}
            names = [names]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError('"events" must be a list of event names, e.g. ["Claim", "Verdict"] (empty = all)')
        sub.events = set(names)
    else:
        raise ValueError(f"unknown op {op!r} - use publish|session_start|control|subscribe")


async def run_replay(bus: Bus, path: Path, **kw) -> None:
    """The replay as a module: its own status dot, and a loud error instead of a silently dead task."""
    bus.publish(ModuleStatus(module="replay", state="up", detail=f"replaying {path.name}"), src="main")
    try:
        n = await replay(bus, path, **kw)
        detail = f"finished {path.name}: {n} events" if n else f"nothing to replay in {path.name}"
        bus.publish(ModuleStatus(module="replay", state="up" if n else "degraded", detail=detail), src="main")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("replay of %s failed", path)
        bus.publish(ModuleStatus(module="replay", state="down", detail=repr(e)[:200]), src="main")


def _create_hint(path: Path) -> str:
    if path.name == "latest.jsonl":
        return "No run recorded yet: run ./run.sh mock (or drive) first, it writes logs/latest.jsonl."
    return f"Save a recorded run there first: cp logs/latest.jsonl {path} (e.g. the 20:00 golden run)."


def replay_file(file: str | Path) -> Path:
    """Resolve a replay --file NOW: follows logs/latest.jsonl before it is re-pointed; a missing file fails loudly."""
    try:
        path = Path(file).resolve(strict=True)
    except FileNotFoundError as e:
        raise FileNotFoundError(e.errno, f"No such file. {_create_hint(Path(file))}", str(file)) from None
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    return path


def build_app(profile: str = "mock", file: str | None = None, speed: float = 1.0, only=(),
              log_dir: Path | None = None, last: bool = True, max_gap: float = MAX_GAP_S) -> FastAPI:
    if file:
        file = replay_file(file)
    log_dir = log_dir or ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{profile}.jsonl"
    latest = log_dir / "latest.jsonl"
    if not file:          # latest.jsonl = the last recorded run, never a replay of one
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(log_path.name)
        except OSError:
            pass
    bus = Bus(log_path)
    choice = dict(PROFILES[profile])
    for role in MODULES:
        if role.upper() in os.environ:
            choice[role] = norm_choice(os.environ[role.upper()])
    served: dict[str, str] = {}   # role -> what the server serves for it without a run.py

    @asynccontextmanager
    async def lifespan(app):
        bus.bind_loop(asyncio.get_running_loop())
        tasks = [asyncio.create_task(flags_watcher(bus)), asyncio.create_task(loop_lag(bus))]
        for role, impl in choice.items():
            try:
                spec = resolve(role, impl)
            except ValueError as e:
                log.error("%s", e)
                bus.publish(ModuleStatus(module=role, state="down", detail=str(e)), src="main")
                continue
            if spec:
                tasks.append(asyncio.create_task(supervise(bus, role, spec, served.get(role, ""))))
            elif impl == "external":
                bus.publish(ModuleStatus(module=role, state="up", detail="external (connects via /ws or /publish)"),
                            src="main")
        if file:
            tasks.append(asyncio.create_task(run_replay(bus, file, speed=speed, only=only, last=last,
                                                        max_gap=max_gap)))
        if os.environ.get("PRINT"):
            tasks.append(asyncio.create_task(supervise(bus, "printer", "app.mocks.printer:run")))
        yield
        for t in tasks:
            t.cancel()
        bus.close()

    app = FastAPI(title="debate-ref", lifespan=lifespan)
    app.state.bus, app.state.choice, app.state.log_path = bus, choice, log_path

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        sub = bus.subscribe(f"ws-{id(sock)}", maxsize=2000)   # subscribe + snapshot in one sync tick
        snap_seq = bus.last_seq                               # => no gap, no duplicates
        hist = [e.model_dump(mode="json") for e in bus.history]

        async def reader():                                   # client -> bus; a bad frame never ends it
            while True:
                msg = await sock.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                m = None
                try:
                    m = json.loads(msg["text"] if msg.get("text") is not None else msg.get("bytes") or b"")
                    _ws_message(bus, sub, m)
                except Exception as e:
                    op = m.get("op", "publish") if isinstance(m, dict) else "publish" if isinstance(m, list) else None
                    await sock.send_json({"event": "Error", "detail": str(e)[:300], "op": op})

        async def writer():                                   # bus -> client: the snapshot, then live events
            await sock.send_json({"event": "Snapshot", "schema": SCHEMA_VERSION, "last_seq": snap_seq,
                                  "flags": FLAGS, "modules": choice, "events": hist})
            async for ev in sub:
                if sub.dropped:
                    await sock.close(code=1013)               # too slow: client reconnects + resyncs
                    return
                await sock.send_text(ev.model_dump_json())

        # Whichever side ends first ends both: a client that left in a quiet period must not leave the writer
        # (and its 2000-slot Sub) parked until the next event.
        tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]   # writer first: Snapshot first
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                try:
                    t.result()
                except (WebSocketDisconnect, RuntimeError):
                    pass
        finally:
            sub.close()
            for t in tasks:
                t.cancel()

    @app.post("/publish")
    async def publish(req: Request):
        """Publish one event (JSON object) or many (JSON array). Returns them with seq/ts filled in."""
        try:
            return _publish_payload(bus, await req.json(), src=req.headers.get("x-src", "http"))
        except Exception as e:
            raise HTTPException(422, str(e)[:500])

    @app.post("/session")
    async def session(req: Request):
        try:
            ev = SessionStart.model_validate(await req.json())
        except Exception as e:
            raise HTTPException(422, str(e)[:500])
        return bus.publish(ev, src="http").model_dump(mode="json")

    @app.post("/control")
    async def control(req: Request):
        try:
            ev = Control.model_validate(await req.json())
        except Exception as e:
            raise HTTPException(422, str(e)[:500])
        return bus.publish(ev, src="http").model_dump(mode="json")

    @app.get("/health")
    async def health():
        return {"ok": True, "schema": SCHEMA_VERSION, "seq": bus.last_seq, "modules": choice,
                "flags": FLAGS, "log": str(log_path)}

    @app.get("/debug/subs")
    async def debug_subs():
        return [{"name": s.name, "qsize": s.q.qsize(), "dropped": s.dropped, "events": sorted(s.events)}
                for s in bus.subs]

    @app.get("/debug/history")
    async def debug_history(n: int = 200):
        return [e.model_dump(mode="json") for e in bus.history[-n:]] if n > 0 else []   # [-0:] is everything

    @app.get("/debug")
    async def debug_page():
        return FileResponse(ROOT / "app" / "tools" / "ws_debug.html")

    # Teammate routers are optional: mount them if the module exists, skip silently otherwise.
    for role in ("ears", "evidence", "face"):
        mod = f"app.{role}.routes"
        try:
            app.include_router(importlib.import_module(mod).router)
            served[role] = "serving routes.py"
            log.info("mounted %s", mod)
        except ModuleNotFoundError as e:
            if e.name != mod:
                log.exception("router %s failed to import", mod)
        except Exception:
            log.exception("router %s failed to import", mod)

    face_static = ROOT / "app" / "face" / "static"
    if face_static.is_dir():                         # D's assets: /static/app.js, /static/style.css, ...
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=face_static), name="static")
        served.setdefault("face", "serving static/")

    if not any(getattr(r, "path", None) == "/" for r in app.routes):
        @app.get("/")
        async def index():
            face = ROOT / "app" / "face" / "static" / "index.html"
            if face.exists():
                return FileResponse(face)
            return JSONResponse({"debate-ref": "running", "debug_ui": "/debug", "ws": "/ws", "health": "/health"})

    return app


def speed_arg(s: str) -> float:
    v = float(s)
    if not v > 0:                                    # also NaN; 0 divides by zero, < 0 runs time backwards
        raise argparse.ArgumentTypeError(f"must be > 0, not {s}")
    return v


def max_gap_arg(s: str) -> float:
    v = float(s)
    if not v >= 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (0 = keep every gap), not {s}")
    return v


def port_in_use(host: str, port: int) -> bool:
    """Another server (an accidental second ./run.sh) already listens there. uvicorn only finds out after the app
    is built, when logs/latest.jsonl already points at this run's empty log. Other bind errors: uvicorn's to report."""
    try:
        socket.create_server((host, port), family=socket.AF_INET6 if ":" in host else socket.AF_INET).close()
    except OSError as e:
        return e.errno == errno.EADDRINUSE
    return False


def main():
    import uvicorn
    p = argparse.ArgumentParser(description="debate-ref: live debate fact-checker")
    p.add_argument("profile", nargs="?", default="mock", choices=PROFILES)
    p.add_argument("--file", help="replay a JSONL (fixture or logs/*.jsonl; logs/latest.jsonl = the last run)")
    p.add_argument("--speed", type=speed_arg, default=1.0)
    p.add_argument("--only", nargs="*", default=[], help="replay only these event types, e.g. Utterance Pause")
    p.add_argument("--all-sessions", action="store_true",
                   help="replay every session in the file (default: from the last SessionStart on)")
    p.add_argument("--max-gap", type=max_gap_arg, default=MAX_GAP_S,
                   help=f"cut replayed silences longer than this many seconds (default {MAX_GAP_S:g}; 0 = keep)")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    a = p.parse_args()
    if a.profile == "replay" and not a.file:
        p.error("replay needs --file, e.g. ./run.sh replay --file logs/latest.jsonl")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        file = replay_file(a.file) if a.file else None
    except FileNotFoundError as e:
        p.error(f"--file {a.file}: {e.strerror or e}")
    if port_in_use(a.host, a.port):                  # before build_app re-points logs/latest.jsonl
        p.error(f"port {a.port} is already in use: is another ./run.sh running? Stop it or pass --port")
    app = build_app(a.profile, file, a.speed, a.only, last=not a.all_sessions, max_gap=a.max_gap)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
