"""Face: the page is served and the one-click replay fallback re-drives the pipeline."""
import time

from tests.test_main import make_client  # noqa: F401  (fixture)


def events(c, name):
    return [e for e in c.get("/debug/history?n=2000").json() if e["event"] == name]


def wait_for(c, name, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        found = events(c, name)
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError(f"no {name} within {timeout} s")


def test_page_and_assets_are_served(make_client):
    c = make_client()
    page = c.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    for asset in ("app.js", "style.css", "motions.json"):
        assert c.get(f"/static/{asset}").status_code == 200


def test_replay_starts_a_session_with_fresh_timestamps(make_client):
    c = make_client(FIXTURE="fixtures/fake_debate.jsonl", MOCK_EARS_DELAY=0)
    started = time.time()
    r = c.post("/face/replay?speed=20")
    assert r.status_code == 200 and r.json()["fixture"] == "fake_debate.jsonl"
    session = wait_for(c, "SessionStart")[0]
    assert session["motion"] == "This house would ban cars from city centres"
    utterance = wait_for(c, "Utterance")[0]
    assert utterance["t_end"] >= started - 1    # re-timed to now, so the latency meter stays honest
    assert c.get("/health").json()["flags"]["screen"] == "live"   # leaves the final screen if it was up
    assert c.post("/face/replay/stop").json()["stopped"] is True


def test_second_replay_click_restarts_instead_of_overlapping(make_client):
    c = make_client(FIXTURE="fixtures/fake_debate.jsonl", MOCK_EARS_DELAY=0)
    c.post("/face/replay")
    wait_for(c, "SessionStart")
    first = c.app.state.face_replay
    c.post("/face/replay")
    assert c.app.state.face_replay is not first
    assert c.post("/face/replay/stop").json()["stopped"] is True
    assert c.post("/face/replay/stop").json()["stopped"] is False


def test_replay_reports_a_missing_recording(make_client):
    c = make_client(FIXTURE="fixtures/does_not_exist.jsonl")
    assert c.post("/face/replay").status_code == 404
    assert c.post("/face/replay?speed=0").status_code == 422


def test_motion_picker_offers_every_demo_motion():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    picker = json.loads((root / "app/face/static/motions.json").read_text())
    demo = [m["text"] for m in json.loads((root / "app/evidence/motions.json").read_text())["motions"]]
    assert picker == demo   # a Motion the picker lacks can't be chosen; one Evidence lacks has no index
