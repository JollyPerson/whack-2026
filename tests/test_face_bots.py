"""The side bots' avatar channels: /face/mate/next/{referee|for|against}, their dances and status dots. All offline."""
from __future__ import annotations

import time

import pytest

from app.bus import Bus
from app.face import run as face_run, speech
from app.face.speech import CHANNELS, state_for
from app.face.tts import TTS
from app.schemas import BotLine, DebateResult, SessionStart
from tests.test_face_speech import _POOL, KEYS, Fake, _with_tts
from tests.test_main import make_client  # noqa: F401  (fixture)

BLANK = {"id": "", "text": "", "audio": "", "title": "", "on": False, "clear": False, "seconds": 0}
STOP = {**BLANK, "op": "stop", "clear": True}
DANCE_STOP = {**BLANK, "op": "dance_stop"}


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
    assert poll(c, None, cur, ep)["cmds"] == [STOP]                        # SessionStart; the bots' dances stop too
    assert all(poll(c, ch, cur, ep)["cmds"] == [STOP, DANCE_STOP] for ch in ("for", "against"))


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


# ----------------------------------------------------------------------------- verdict dances (DebateResult)
WIN, LOSE, DRAW = "MMD-Conqueror", "MMD-Lost One_s Weeping", "MMD-皆大欢喜-卜逻desu"


def result(winner="prop", **kw):
    """A DebateResult as app/judge publishes it: the winner's bot (prop -> for, opp -> against) gets the music."""
    win, lose = ("for", "against") if winner == "prop" else ("against", "for")
    return {"event": "DebateResult", "winner": winner, "margin": "clear", "headline": "Prop wins on the evidence.",
            "scores": {"prop": {"factuality": 8, "overall": 7.5}, "opp": {"factuality": 5, "overall": 5.5}},
            "report": "logs/summaries/run-summary.md", "dances": {win: WIN, lose: LOSE},
            "music": {win: True, lose: False}, "dance_s": 30.0, "via": "llm", **kw}


def dance(title, on=False, seconds=30):
    return {**BLANK, "op": "dance", "title": title, "on": on, "seconds": seconds}


def test_the_winner_dances_with_music_and_the_loser_silently(make_client):
    c = make_client()
    cur, ep = resync(c)
    c.post("/publish", json=result("opp"))
    assert poll(c, "against", cur, ep)["cmds"] == [dance(WIN, on=True)]        # Opp won: its second, Against
    assert poll(c, "for", cur, ep)["cmds"] == [dance(LOSE)]                    # the loser dances in silence
    assert poll(c, None, cur, ep)["cmds"] == []                                # the referee is unchanged


def test_a_draw_has_both_bots_dance_the_same_title(make_client):
    c = make_client()
    cur, ep = resync(c)
    c.post("/publish", json=result("draw", margin="", dances={"for": DRAW, "against": DRAW},
                                   music={"for": False, "against": False}, dance_s=12.5))
    got = {ch: poll(c, ch, cur, ep)["cmds"] for ch in ("for", "against")}
    assert got == {"for": [dance(DRAW, seconds=13)], "against": [dance(DRAW, seconds=13)]}   # whole seconds, rounded up


def test_a_bot_without_a_title_does_not_dance_and_keeps_talking(make_client):
    c = make_client()
    cur, ep = resync(c)
    c.post("/publish", json=result(dances={"for": WIN, "against": ""}))
    c.post("/publish", json=result(dances={"for": WIN}))
    c.post("/publish", json=line("against", "b-0001", "Still here."))
    assert [x["op"] for x in poll(c, "against", cur, ep)["cmds"]] == ["say", "talk"]
    assert poll(c, "for", cur, ep)["cmds"] == [dance(WIN, on=True)] * 2


def test_a_result_older_than_its_dance_is_not_danced(make_client):
    c = make_client()
    cur, ep = resync(c)
    c.post("/publish", json=result(ts=time.time() - 31))
    c.post("/publish", json=line("for", "b-0001", "Back to it."))
    assert [x["op"] for x in poll(c, "for", cur, ep)["cmds"]] == ["say", "talk"]   # nothing dancing to protect


def test_bot_lines_wait_out_the_dance_or_the_next_session(make_client):
    c = make_client()
    cur, ep = resync(c)
    c.post("/publish", json=line("for", "b-0001", "Before the verdict."))
    c.post("/publish", json=result())
    c.post("/publish", json=line("for", "b-0002", "Mid-dance."))
    c.post("/publish", json={"event": "BotTalk", "persona": "for", "line_id": "b-0001", "on": False})
    body = poll(c, "for", cur, ep)
    assert [(x["op"], x["id"]) for x in body["cmds"]] == [("say", "b-0001"), ("talk", "b-0001"), ("dance", ""),
                                                          ("talk", "b-0001")]   # no say: it would stop the dance
    cur = body["cursor"]
    c.post("/publish", json=line("for", "b-0003", "Still dancing."))
    assert poll(c, "for", cur, ep)["cmds"] == []
    c.post("/session", json={"motion": "m"})
    c.post("/publish", json=line("for", "b-0004", "New debate."))
    assert [(x["op"], x["id"]) for x in poll(c, "for", cur, ep)["cmds"]] == [
        ("stop", ""), ("dance_stop", ""), ("say", "b-0004"), ("talk", "b-0004")]


def test_the_dance_window_ends_after_dance_s():
    mate, t0 = speech.Mate(), 1000.0
    res = DebateResult.model_validate(result(dance_s=30.0, ts=t0))

    def says(t: float, persona="for", m=mate) -> list[str]:
        return [x["op"] for x in speech.bot_cmds(BotLine(persona=persona, id=f"b-{t}", text="x", ts=t), t, persona, m)]

    assert speech.bot_cmds(res, t0 + 1, "for", mate) == [dance(WIN, on=True)]   # delivered at t0+1: on until t0+31
    assert says(t0 + 2) == [] and says(t0 + 30.9) == []
    assert says(t0 + 31) == ["say", "talk"]
    assert says(t0 + 2, "against", speech.Mate()) == ["say", "talk"]           # each channel has its own window
    speech.bot_cmds(res, t0 + 1, "for", mate)
    assert speech.bot_cmds(SessionStart(ts=t0 + 3), t0 + 3, "for", mate) == [STOP, DANCE_STOP]
    assert says(t0 + 4) == ["say", "talk"]                                     # a new debate: talking again
    assert speech.bot_cmds(res, t0 + 40, "for", mate) == []                    # past its dance: never started
    assert says(t0 + 41) == ["say", "talk"]


def test_a_dance_to_the_end_still_holds_the_lines_back():
    mate = speech.Mate()
    res = DebateResult.model_validate(result(dance_s=0, ts=100.0))
    assert speech.bot_cmds(res, 100.0, "for", mate) == [dance(WIN, on=True, seconds=0)]
    assert speech.bot_cmds(BotLine(persona="for", id="b", text="x", ts=160.0), 160.0, "for", mate) == []


def test_the_referee_channel_ignores_results_and_keeps_its_session_start():
    tts, res = TTS([]), DebateResult.model_validate(result(ts=time.time()))
    assert speech.to_cmds(res, tts, time.time(), "referee", speech.Mate()) == []
    assert speech.to_cmds(SessionStart(), tts, time.time(), "referee", speech.Mate()) == [STOP]


def test_operator_dances_take_optional_music_and_seconds(make_client):
    c = make_client()
    cur, ep = resync(c, "for")
    c.post("/face/mate/dance", json={"title": WIN, "channel": "for", "on": True, "seconds": 12.2})
    c.post("/face/mate/dance", json={"title": "PET_DANCING_01", "channel": "for"})
    assert poll(c, "for", cur, ep)["cmds"] == [dance(WIN, on=True, seconds=13), dance("PET_DANCING_01", seconds=0)]
    assert c.post("/face/mate/dance", json={"seconds": -1}).status_code == 422


# ----------------------------------------------------------------------------- citations in the bots' bubbles
ONS = {"url": "https://www.ons.gov.uk/employment/x", "title": "Labour market overview - Office for National Statistics",
       "quote": "The unemployment rate was 4.2%."}


def test_a_sourced_line_names_its_source_on_a_second_line(make_client, tmp_path):
    """Like a stage character's sign: "— <source name>: <short title>" under the line. BotLine.text is unchanged
    (the bus, the log and the dashboard keep it as it was), and the referee's channel never sees any of it."""
    c = make_client()
    _with_tts(c, tmp_path, [])
    cur, ep = resync(c)
    text = "The ONS says 4.2%, not 6%. Try again."
    ev = line("for", "b-0001", text, kind="rebut", target="B", sourced=True, evidence=[ONS])
    assert c.post("/publish", json=ev).status_code == 200
    say = poll(c, "for", cur, ep)["cmds"][0]
    assert say["op"] == "say" and say["text"] == f"{text}\n— the ONS: Labour market overview"
    assert [e for e in c.get("/debug/history").json() if e["event"] == "BotLine"][0]["text"] == text
    assert poll(c, None, cur, ep)["cmds"] == []                 # the referee's channel: unchanged, no bot line


def test_unsourced_lines_keep_their_text_and_tag(make_client, tmp_path):
    """An AI-knowledge challenge or concede keeps "(unsourced)" and gets no source line, even if it carries
    evidence; a line marked sourced with no evidence is its text alone."""
    c = make_client()
    _with_tts(c, tmp_path, [])
    cur, ep = resync(c)
    lines = [line("against", "b-0001", "It was 3%, not 30%. (unsourced)", kind="challenge", evidence=[ONS]),
             line("against", "b-0002", "Annoyingly, that checks out. (unsourced)", kind="concede"),
             line("against", "b-0003", "Source for that 40%?", sourced=True)]
    for ev in lines:
        assert c.post("/publish", json=ev).status_code == 200
    says = [x["text"] for x in poll(c, "against", cur, ep)["cmds"] if x["op"] == "say"]
    assert says == [ev["text"] for ev in lines]


def _bl(evidence, sourced=True, text="Try again."):
    return BotLine(persona="for", text=text, kind="rebut", sourced=sourced, evidence=evidence)


def test_the_source_line_is_short_plain_text():
    long_title = "A very long report title about the economics of congestion charging in many cities worldwide"
    cases = {
        "— the WHO: Air quality": [{"url": "https://www.who.int/a", "title": "Air quality | WHO"}],
        "— the ONS": [{"url": "https://ons.gov.uk/a", "title": ""}],                 # no title: the name alone
        "— Snopes": [{"url": "https://x.org/a", "title": "That claim is false - Snopes"}],   # no judgement words
        "— A very long report title about the economics…": [{"url": "https://x.org/a", "title": long_title}],
        "— Congestion charge": [{"url": "https://foo.example/a", "title": "Congestion charge"}],   # said once
        "— the BBC": [{"url": "https://bbc.co.uk/a", "title": "see https://bbc.co.uk/news/x"}],   # never a URL
        "": [{"url": "", "title": ""}],
    }
    for want, ev in cases.items():
        assert speech.cite(ev) == want, ev
    for ev in ([{"url": "https://ons.gov.uk/a", "title": long_title}],
               [{"url": "https://x.org/a", "title": "Supercalifragilistic" * 6}],
               [{"url": "https://x.org/a", "title": "Pneumonoultramicroscopic silicovolcanoconiosis " * 4}]):
        got = speech.cite(ev)
        name_and_title = got.removeprefix("— ").replace(":", "")
        assert len(got) <= speech.CITE_MAX_CHARS and len(name_and_title.split()) <= speech.CITE_MAX_WORDS, got
        assert got.endswith("…") and "http" not in got and "\n" not in got, got
    assert speech.bot_text(_bl([ONS])) == "Try again.\n— the ONS: Labour market overview"
    assert speech.bot_text(_bl([ONS], sourced=False)) == "Try again."
    assert speech.bot_text(_bl([])) == "Try again."

