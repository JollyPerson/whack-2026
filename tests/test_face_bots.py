"""The side bots' avatar channels: /face/mate/next/{referee|for|against}, their dances and status dots. All offline."""
from __future__ import annotations

import time

import pytest

from app.bus import Bus
from app.face import run as face_run, speech
from app.face.speech import CHANNELS, state_for
from tests.test_face_speech import _POOL, KEYS, Fake, _with_tts
from tests.test_main import make_client  # noqa: F401  (fixture)

BLANK = {"id": "", "text": "", "audio": "", "title": "", "on": False, "clear": False}
STOP = {**BLANK, "op": "stop", "clear": True}


@pytest.fixture(autouse=True)
def _no_real_voice(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


def poll(c, channel, after, epoch, wait=0):
    path = "/face/mate/next" if channel is None else f"/face/mate/next/{channel}"
    r = c.get(path, params={"after": after, "epoch": epoch, "wait": wait})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"v", "epoch", "cursor", "cmds"} and body["v"] == 1
    for cmd in body["cmds"]:
        assert set(cmd) == KEYS and None not in cmd.values(), cmd     # JsonUtility: fixed keys, never null
    return body


def resync(c, channel=None):
    b = poll(c, channel, -1, "")
    assert b["cmds"] == []
    return b["cursor"], b["epoch"]


def line(persona, id, text, **kw):
    return {"event": "BotLine", "persona": persona, "id": id, "text": text, **kw}


def test_each_bot_channel_gets_only_its_own_lines(make_client, tmp_path):
    c = make_client()
    tts = _with_tts(c, tmp_path, [Fake()])
    cur, ep = resync(c)
    assert resync(c, "for")[1] == ep                           # one epoch and cursor for every channel
    for ev in [line("for", "b-0001", "ONS says 4.2%, not 6%. Try again.", kind="rebut", target="B", sourced=True),
               line("against", "b-0002", "Source for that 40%?", target="A"),
               {"event": "Interjection", "id": "i-0001", "text": "Point of fact."},
               {"event": "TTSState", "speaking": True, "interjection_id": "i-0001"},
               line("for", "b-0003", "Too old.", ts=time.time() - 10),
               line("judge", "b-0004", "No such bot."),
               {"event": "BotTalk", "persona": "for", "line_id": "b-0001", "on": False},
               {"event": "BotTalk", "persona": "against", "line_id": "b-0002", "on": False},
               {"event": "Control", "flag": "tts", "value": False},
               {"event": "Control", "flag": "director", "value": False},
               {"event": "Control", "flag": "aggr", "value": 4}]:
        assert c.post("/publish", json=ev).status_code == 200
    assert poll(c, "for", cur, ep)["cmds"] == [
        {**BLANK, "op": "say", "id": "b-0001", "text": "ONS says 4.2%, not 6%. Try again."},   # text only: no audio
        {**BLANK, "op": "talk", "id": "b-0001", "on": True},   # the BotLine itself opens the mouth
        {**BLANK, "op": "talk", "id": "b-0001"},               # BotTalk(on=false) closes it
    ]
    assert poll(c, "against", cur, ep)["cmds"] == [
        {**BLANK, "op": "say", "id": "b-0002", "text": "Source for that 40%?"},
        {**BLANK, "op": "talk", "id": "b-0002", "on": True},
        {**BLANK, "op": "talk", "id": "b-0002"},
    ]
    referee = poll(c, None, cur, ep)
    assert referee["cmds"] == [                                # exactly today's bridge: no bot line, no bot mouth
        {**BLANK, "op": "say", "id": "i-0001", "text": "Point of fact.", "audio": tts.url_for("Point of fact.")},
        {**BLANK, "op": "talk", "id": "i-0001", "on": True},
        {**BLANK, "op": "stop"},
        STOP,
    ]
    assert poll(c, "referee", cur, ep) == referee              # /face/mate/next == /face/mate/next/referee


def test_bot_channels_clear_on_session_start_mute_and_bots_off(make_client):
    c = make_client()
    cur, ep = resync(c)
    for ev in [{"event": "Control", "flag": "mute", "value": True},
               {"event": "Control", "flag": "mute", "value": False},
               {"event": "Control", "flag": "bots", "value": "off"},  # a bool flag: "off" is False
               {"event": "Control", "flag": "bots", "value": True}]:
        c.post("/publish", json=ev)
    for ch in ("for", "against"):
        assert poll(c, ch, cur, ep)["cmds"] == [STOP, STOP], ch            # mute, bots off
    assert poll(c, None, cur, ep)["cmds"] == [STOP]                        # mute: bots is not the referee's flag
    cur = c.get("/health").json()["seq"]
    c.post("/session", json={"motion": "m"})
    assert all(poll(c, ch, cur, ep)["cmds"] == [STOP] for ch in CHANNELS)  # SessionStart


def test_unknown_channel_is_a_404_and_leaves_no_state(make_client):
    c = make_client()
    for path in ("/face/mate/next/judge", "/face/mate/next/For", "/face/mate/next/for/x"):
        assert c.get(path, params={"after": -1}).status_code == 404, path
    assert c.post("/face/mate/dance", json={"channel": "judge"}).status_code == 404
    assert c.post("/face/mate/dance/stop", json={"channel": "judge"}).status_code == 404
    st = state_for(c.app.state.bus)
    assert tuple(st.mates) == CHANNELS and all(m.seen == 0 and not m.dances for m in st.mates.values())


def test_a_bot_long_poll_wakes_only_for_its_own_lines(make_client, tmp_path):
    c = make_client()
    _with_tts(c, tmp_path, [Fake()])
    cur, ep = resync(c)
    t0 = time.monotonic()
    for_, ref = _POOL.submit(poll, c, "for", cur, ep, 10), _POOL.submit(poll, c, None, cur, ep, 10)
    time.sleep(0.3)
    c.post("/publish", json=line("against", "b-0001", "Not yours."))
    time.sleep(0.2)
    assert not for_.done() and not ref.done()                 # the other bot's line wakes neither
    c.post("/publish", json={"event": "Interjection", "id": "i-0001", "text": "Point of fact."})
    assert [x["op"] for x in ref.result(timeout=5)["cmds"]] == ["say"]
    time.sleep(0.2)
    assert not for_.done()                                     # the referee's line never reaches a bot
    c.post("/publish", json=line("for", "b-0002", "Source for that?"))
    assert [(x["op"], x["id"]) for x in for_.result(timeout=5)["cmds"]] == [("say", "b-0002"), ("talk", "b-0002")]
    assert time.monotonic() - t0 < 5


def test_each_channel_is_seen_on_its_own(make_client):
    c = make_client()
    resync(c, "against")
    st = state_for(c.app.state.bus)
    assert st.mates["against"].seen > 0 and st.mates["against"].polls == 0
    assert st.mates["referee"].seen == 0 and st.mates["for"].seen == 0


def test_mate_status_per_channel_only_on_change():
    bus = Bus()
    st = state_for(bus)
    sub = bus.subscribe("t", {"ModuleStatus"})
    face_run.watch_mate(bus, st, 100.0)
    st.mates["for"].seen = 100.0                               # only the For avatar connects
    face_run.watch_mate(bus, st, 100.5)
    st.mates["against"].polls = 1                              # then Against, mid-poll
    face_run.watch_mate(bus, st, 101.0)
    face_run.watch_mate(bus, st, 102.0)
    face_run.watch_mate(bus, st, 100.0 + speech.MATE_DOWN_S + 0.1)          # For went quiet; Against still polling
    got = [sub.q.get_nowait() for _ in range(sub.q.qsize())]
    assert [(e.module, e.state) for e in got] == [("mate_for", "up"), ("mate_against", "up"), ("mate_for", "down")]
    assert got[0].detail == "avatar polling /face/mate/next/for" and got[0].src == "face"
    assert "mate" not in {e.module for e in got}               # the referee's avatar never connected: no dot


def test_dances_go_to_one_channel_or_to_every_one(make_client):
    c = make_client()
    cur, ep = resync(c, "for")
    dance = {**BLANK, "op": "dance", "title": "PET_DANCING_01"}
    r = c.post("/face/mate/dance", json={"title": "PET_DANCING_01", "channel": "for"}).json()
    assert r == {"ok": True, "mate": True}                     # For polled a moment ago
    assert c.post("/face/mate/dance", json={"channel": "against"}).json()["mate"] is False   # never polled
    assert poll(c, "referee", cur, ep)["cmds"] == []
    assert poll(c, "for", cur, ep)["cmds"] == [dance]
    assert poll(c, "against", cur, ep)["cmds"] == [{**BLANK, "op": "dance"}]
    c.post("/face/mate/dance", json={"title": "X"})            # no channel: every avatar
    c.post("/face/mate/dance/stop")                            # no body: every avatar (as before)
    c.post("/face/mate/dance/stop", json={"channel": "against"})
    got = {ch: [(x["op"], x["title"]) for x in poll(c, ch, cur, ep)["cmds"]] for ch in CHANNELS}
    assert got == {"referee": [("dance", "X"), ("dance_stop", "")], "for": [("dance", "X"), ("dance_stop", "")],
                   "against": [("dance", "X"), ("dance_stop", ""), ("dance_stop", "")]}
    assert all(poll(c, ch, cur, ep)["cmds"] == [] for ch in CHANNELS)      # delivered once per channel


def test_a_channel_nobody_polls_keeps_no_expired_dances():
    mate = speech.Mate()
    mate.dances = [(time.monotonic() - speech.DANCE_TTL_S - 1, {"op": "dance"})]
    mate.queue({"op": "dance_stop"})
    assert [c["op"] for _, c in mate.dances] == ["dance_stop"]
