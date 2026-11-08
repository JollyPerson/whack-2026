"""ears/bridge.py: ears.py sentences -> Utterance and Pause events on /publish. Fake publisher, fake urlopen, no network."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
import types
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ears_bridge", ROOT / "ears" / "bridge.py")
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


class FakePublisher:
    def __init__(self):
        self.events = []

    def send(self, event):
        self.events.append(event)

    def kinds(self):
        return [(e["event"], e["speaker"]) for e in self.events]


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def said(source, start, end, text="Something."):
    return {"type": "sentence", "source": source, "start": start, "end": end, "text": text}


@pytest.fixture
def clock(monkeypatch):
    """bridge's wall clock, moved by hand; real sleeps so its watcher thread still ticks."""
    now = [1000.0]
    monkeypatch.setattr(bridge, "time", types.SimpleNamespace(time=lambda: now[0], sleep=time.sleep,
                                                              monotonic=time.monotonic))
    return now


# ----------------------------------------------------------------------------- Pauses
def test_no_pause_while_the_next_sentence_of_a_monologue_may_still_be_coming(clock):
    # ears only sends finished sentences: after an 8 s one, the next can be as far away again.
    pub = FakePublisher()
    b = bridge.Bridge(pub, {"mic1": "A"}, 2.0)
    b.sentence(said("mic1", 0.0, 8.0))
    clock[0] += 2.3
    time.sleep(0.35)                                         # a few watcher ticks
    assert pub.kinds() == [("Utterance", "A")]
    clock[0] = 1000.0 + 9.3                                  # 8 s + 1.2 s, as the brain's PauseSynth waits
    assert wait_for(lambda: pub.kinds() == [("Utterance", "A"), ("Pause", "A")])
    b.finish()


@pytest.mark.parametrize("length, gap", [(1.0, 6.0), (8.0, 9.2), (30.0, 15.0)])
def test_the_pause_comes_after_pause_after_or_the_sentence_length_capped(length, gap):
    pub = FakePublisher()
    b = bridge.Bridge(pub, {"mic1": "A"}, 6.0)
    b.sentence(said("mic1", 0.0, length))
    t = pub.events[0]["t_end"]
    b.check(t + gap - 0.05)
    assert pub.kinds() == [("Utterance", "A")]
    b.check(t + gap)
    assert pub.kinds() == [("Utterance", "A"), ("Pause", "A")]
    assert pub.events[1]["t"] == t + gap
    b.check(t + gap + 5)                                     # once per turn
    assert len(pub.events) == 2
    b.finish()


def test_a_pause_after_longer_than_the_cap_is_still_honoured():
    pub = FakePublisher()
    b = bridge.Bridge(pub, {"mic1": "A"}, 20.0)
    b.sentence(said("mic1", 0.0, 1.0))
    t = pub.events[0]["t_end"]
    b.check(t + 19.9)
    assert len(pub.events) == 1
    b.finish()


def test_the_other_speaker_starting_ends_the_first_ones_turn_at_once():
    pub = FakePublisher()
    b = bridge.Bridge(pub, {"mic1": "A", "mic2": "B"}, 6.0)
    b.sentence(said("mic1", 0.0, 3.0))
    b.sentence(said("mic2", 3.5, 5.0))
    b.sentence(said("mic2", 5.0, 6.0))
    assert pub.kinds() == [("Utterance", "A"), ("Pause", "A"), ("Utterance", "B"), ("Utterance", "B")]
    b.finish()


def test_more_speech_pushes_the_pause_back(clock):
    pub = FakePublisher()
    b = bridge.Bridge(pub, {"mic1": "A"}, 6.0)
    b.sentence(said("mic1", 0.0, 1.0))
    clock[0] += 4.0
    b.sentence(said("mic1", 1.0, 2.0))
    b.check(1000.0 + 6.0)
    assert len(pub.events) == 2
    b.check(1004.0 + 6.0)
    assert pub.kinds() == [("Utterance", "A"), ("Utterance", "A"), ("Pause", "A")]
    b.finish()


def test_utterance_times_end_on_arrival_and_keep_the_sentence_length():
    pub = FakePublisher()
    b = bridge.Bridge(pub, {}, 6.0)
    b.sentence(said("mic9", 10.0, 12.5, "  Hello.  "))
    b.sentence(said("mic9", 13.0, 14.0, "   "))               # nothing said: no event
    [u] = pub.events
    assert (u["speaker"], u["text"], u["final"]) == ("mic9", "Hello.", True)
    assert u["t_end"] - u["t_start"] == pytest.approx(2.5)
    b.finish()


# ----------------------------------------------------------------------------- end of input
def test_end_of_input_pauses_the_last_speaker_before_closing(monkeypatch, capsys):
    lines = [said("mic1", 0.0, 2.0, "First."), said("mic2", 2.5, 4.0, "Second.")]
    monkeypatch.setattr(sys, "argv", ["bridge.py", "--dry-run"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(x) + "\n" for x in lines) + "not json\n"))
    bridge.main()
    sent = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["event"], e["speaker"]) for e in sent] == [("Utterance", "A"), ("Pause", "A"), ("Utterance", "B"),
                                                          ("Pause", "B")]


def test_close_says_how_many_events_never_went_out(monkeypatch, capsys):
    release = threading.Event()

    def stuck(req, timeout):
        release.wait(5)
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", stuck)
    pub = bridge.Publisher("http://x", "u", "")
    for i in range(3):
        pub.send({"event": "Pause", "speaker": "A", "t": i})
    pub.close(timeout=0.2)
    release.set()
    pub.thread.join(5)
    assert "3 event(s) unsent" in capsys.readouterr().err


# ----------------------------------------------------------------------------- Publisher
class OK:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"[]"


def flaky(monkeypatch, *failures):
    """urlopen that raises `failures` in turn, then succeeds. Returns the list of bodies it was tried with."""
    tries, todo = [], list(failures)

    def urlopen(req, timeout):
        tries.append(json.loads(req.data))
        if todo:
            e = todo.pop(0)
            raise e(req) if callable(e) else e
        return OK()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)
    return tries


def http(code):
    return lambda req: urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"busy"))


def publisher(**kw):
    pub = bridge.Publisher("http://x", "u", "pw")
    pub.backoff, pub.max_age = (0.01, 0.02), kw.get("max_age", 5.0)
    return pub


@pytest.mark.parametrize("failures", [
    [http(502)],                                                                   # Caddy while the app restarts
    [http(503), http(504)],
    [urllib.error.URLError("Temporary failure in name resolution")] * 2,          # venue Wi-Fi blip
    [TimeoutError("timed out"), ConnectionResetError()],
])
def test_a_server_restart_or_network_blip_is_retried_until_it_goes_through(monkeypatch, failures):
    tries = flaky(monkeypatch, *failures)
    pub = publisher()
    pub.send({"event": "Pause", "speaker": "A", "t": 1.0})
    pub.close()
    assert len(tries) == len(failures) + 1
    assert all(t == {"event": "Pause", "speaker": "A", "t": 1.0} for t in tries)


@pytest.mark.parametrize("code", [401, 422])
def test_a_client_error_is_not_retried(monkeypatch, code, capsys):
    tries = flaky(monkeypatch, http(code))
    pub = publisher()
    pub.send({"event": "Pause", "speaker": "A", "t": 1.0})
    pub.send({"event": "Pause", "speaker": "B", "t": 2.0})
    pub.close()
    assert [t["speaker"] for t in tries] == ["A", "B"]
    assert f"{code} from http://x/publish" in capsys.readouterr().err


def test_an_event_too_old_to_matter_is_given_up_and_the_next_one_still_goes(monkeypatch, capsys):
    tries = flaky(monkeypatch, *[urllib.error.URLError("down")] * 1000)
    pub = publisher(max_age=0.2)
    pub.send({"event": "Pause", "speaker": "A", "t": 1.0})
    err = []
    assert wait_for(lambda: err.append(capsys.readouterr().err) or "gave up" in "".join(err))
    assert len(tries) > 2                                    # retried, then gave up
    tries.clear()
    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda req, timeout: tries.append(req) or OK())
    pub.send({"event": "Pause", "speaker": "B", "t": 2.0})
    pub.close()
    assert [json.loads(req.data)["speaker"] for req in tries] == ["B"]


def test_one_event_that_blows_up_does_not_stop_the_ones_after_it(monkeypatch, capsys):
    import http.client

    class Unreadable(urllib.error.HTTPError):
        def read(self, *a):
            raise http.client.IncompleteRead(b"")

    tries = flaky(monkeypatch, lambda req: Unreadable(req.full_url, 401, "x", {}, io.BytesIO(b"")))
    pub = publisher()
    pub.send({"event": "Pause", "speaker": "A", "t": 1.0})
    pub.send({"event": "Pause", "speaker": "B", "t": 2.0})
    pub.close(timeout=2.0)
    assert [t["speaker"] for t in tries] == ["A", "B"]
    assert "unsent" not in capsys.readouterr().err


def test_an_event_already_too_old_when_its_turn_comes_is_not_sent(monkeypatch, capsys):
    # A hanging connection (remote server, Wi-Fi gone) holds each try for its timeout while sentences keep coming:
    # once the network is back, the backlog must not reach the server as a burst of minute-old Utterances.
    tries = []

    def urlopen(req, timeout):
        tries.append(json.loads(req.data)["speaker"])
        if len(tries) == 1:
            time.sleep(0.3)
            raise urllib.error.URLError("timed out")
        return OK()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)
    pub = publisher(max_age=0.2)
    pub.send({"event": "Pause", "speaker": "A", "t": 1.0})
    pub.send({"event": "Pause", "speaker": "B", "t": 2.0})          # queued behind A's 0.3 s try
    assert wait_for(lambda: "gave up" in capsys.readouterr().err or len(tries) > 1)
    pub.send({"event": "Pause", "speaker": "C", "t": 3.0})
    pub.close()
    assert tries == ["A", "C"]


def test_the_server_coming_back_is_announced(monkeypatch, capsys):
    flaky(monkeypatch, urllib.error.URLError("Connection refused"), urllib.error.URLError("Connection refused"))
    pub = publisher()
    pub.send({"event": "Pause", "speaker": "A", "t": 1.0})
    pub.send({"event": "Pause", "speaker": "B", "t": 2.0})
    pub.close()
    err = capsys.readouterr().err
    assert err.count("reachable again") == 1
