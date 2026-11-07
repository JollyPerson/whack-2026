"""Face routes. Owner: D. Mounted by app.main when this module exists.

POST /face/replay       one-click fallback: the recorded debate re-drives the running pipeline
                        (mock_ears' SessionStart + Utterance + Pause, re-timed to now), so verdicts and
                        the latency meter stay real. ?speed=2 plays it faster.
POST /face/replay/stop  stops it.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.mocks import mock_ears
from app.schemas import Control, ModuleStatus

log = logging.getLogger("face")
router = APIRouter()


async def _drive(bus, fixture, speed: float) -> None:
    bus.publish(ModuleStatus(module="replay", state="up", detail=f"replaying {fixture.name}"), src="face")
    try:
        await mock_ears.run(bus, fixture=fixture, speed=speed)
        bus.publish(ModuleStatus(module="replay", state="up", detail=f"finished {fixture.name}"), src="face")
    except asyncio.CancelledError:
        bus.publish(ModuleStatus(module="replay", state="up", detail="stopped"), src="face")
        raise
    except Exception as e:
        log.exception("replay of %s failed", fixture)
        bus.publish(ModuleStatus(module="replay", state="down", detail=repr(e)[:200]), src="face")


def _stop(app) -> bool:
    task = getattr(app.state, "face_replay", None)
    app.state.face_replay = None
    if task and not task.done():
        task.cancel()
        return True
    return False


@router.post("/face/replay")
async def start_replay(req: Request, speed: float = Query(1.0, gt=0, le=20)):
    fixture = mock_ears.default_fixture()
    if not fixture.exists():
        raise HTTPException(404, f"no recorded debate at {fixture}")
    bus = req.app.state.bus
    _stop(req.app)                                    # a second click restarts instead of overlapping
    bus.publish(Control(flag="screen", value="live"), src="face")
    req.app.state.face_replay = asyncio.create_task(_drive(bus, fixture, speed))
    return {"ok": True, "fixture": fixture.name, "speed": speed}


@router.post("/face/replay/stop")
async def stop_replay(req: Request):
    return {"ok": True, "stopped": _stop(req.app)}
