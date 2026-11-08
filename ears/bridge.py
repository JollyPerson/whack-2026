#!/usr/bin/env python3
"""Send ears.py sentences to the pipeline as Utterance and Pause events.

ears.py prints one JSON line per final sentence: {"type": "sentence", "source": "mic1", "start", "end", "text"}.
This reads those lines and POSTs them to the server's /publish, so Ears can run on the laptop with the mics while
the pipeline runs anywhere. Standard library only, so it runs in the ears uv environment as it is.

    ./runstt.sh | python bridge.py --url https://<VULTR_DOMAIN>          # live, password from EARS_PASSWORD
    python bridge.py --url http://localhost:8000 --pace < events.jsonl   # replay a saved run at real speed

Each source becomes a speaker (default mic1=A, mic2=B, matching the dashboard's A = prop, B = opp).
A Pause is sent when the other speaker starts, or after --pause-after seconds with no new sentence (longer after a
long sentence, up to 15 s: ears only sends finished sentences, so the next can be a whole sentence away).
The Moderator only speaks at a Pause, so a lower value makes it quicker but riskier to talk over someone.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request


PAUSE_GAP = 1.2         # added to a sentence's length to get the wait for the next: the brain's PauseSynth gap
MAX_PAUSE_GAP = 15.0    # ...capped, like the brain's


def parse_map(text: str) -> dict[str, str]:
    pairs = (p.split("=", 1) for p in text.split(",") if "=" in p)
    return {k.strip(): v.strip() for k, v in pairs}


class Publisher:
    """Posts events from a queue on its own thread, so a slow network never holds up reading the mics."""

    def __init__(self, url: str, user: str, password: str, dry_run: bool = False):
        self.url = url.rstrip("/") + "/publish"
        self.headers = {"content-type": "application/json", "x-src": "ears"}
        if password:
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self.headers["authorization"] = f"Basic {token}"
        self.dry_run = dry_run
        self.queue: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="publisher")
        self.thread.start()

    def send(self, event: dict) -> None:
        self.queue.put(event)

    def close(self, timeout: float = 10.0) -> None:
        self.queue.put(None)
        self.thread.join(timeout)
        if self.thread.is_alive():
            # The None is still queued too, standing in for the event being sent right now.
            print(f"bridge: exiting with {self.queue.qsize()} event(s) unsent", file=sys.stderr)

    def _run(self) -> None:
        while (event := self.queue.get()) is not None:
            if self.dry_run:
                print(json.dumps(event), flush=True)
                continue
            body = json.dumps(event).encode()
            for attempt in (1, 2):
                try:
                    req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
                    with urllib.request.urlopen(req, timeout=5) as r:
                        r.read()
                    break
                except urllib.error.HTTPError as e:
                    print(f"bridge: {e.code} from {self.url}: {e.read()[:200]!r}", file=sys.stderr)
                    break               # a 401 or 422 won't fix itself on retry
                except Exception as e:
                    print(f"bridge: send failed (try {attempt}): {e!r}", file=sys.stderr)


class Bridge:
    """A Pause goes out when the other speaker starts, or once a speaker has said nothing new for a while.
    ears only sends finished sentences, so after a long one the next can be as far away again: the wait is
    the brain's own rule for finals-only ears (its PauseSynth), never shorter than pause_after. The brain takes
    our Pauses as real ones and switches its own synthesis off, so a shorter wait lets the Moderator cut in."""

    def __init__(self, publisher: Publisher, speakers: dict[str, str], pause_after: float):
        self.publisher, self.speakers, self.pause_after = publisher, speakers, pause_after
        self.lock = threading.Lock()
        self.due: dict[str, float] = {}         # speaker mid-turn -> wall time their Pause is due
        self.stopped = threading.Event()
        threading.Thread(target=self._watch, daemon=True, name="pauses").start()

    def speaker_for(self, source: str) -> str:
        return self.speakers.get(source, source)

    def sentence(self, line: dict) -> None:
        text = (line.get("text") or "").strip()
        if not text:
            return
        speaker = self.speaker_for(str(line.get("source", "A")))
        now = time.time()
        length = max(0.0, float(line.get("end", 0) or 0) - float(line.get("start", 0) or 0))
        gap = max(self.pause_after, min(length + PAUSE_GAP, MAX_PAUSE_GAP))
        with self.lock:
            for other in [s for s in self.due if s != speaker]:     # the other speaker starting ends their turn
                self._pause(other, now)
            self.due[speaker] = now + gap
        # ears.py gives stream-relative times, so the end is stamped on arrival and the start set back by the
        # sentence's length. The latency meter then counts from when the sentence reached us.
        self.publisher.send({"event": "Utterance", "speaker": speaker, "text": text, "final": True,
                             "t_start": now - length, "t_end": now})

    def check(self, now: float) -> None:
        """Send the Pauses due by `now`."""
        with self.lock:
            for speaker in [s for s, due in self.due.items() if now >= due]:
                self._pause(speaker, now)

    def finish(self) -> None:
        """End of input: stop watching, and whoever was still mid-turn has stopped."""
        self.stopped.set()
        now = time.time()
        with self.lock:
            for speaker in list(self.due):
                self._pause(speaker, now)

    def _pause(self, speaker: str, now: float) -> None:
        del self.due[speaker]
        self.publisher.send({"event": "Pause", "speaker": speaker, "t": now})

    def _watch(self) -> None:
        while not self.stopped.wait(0.1):
            self.check(time.time())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("EARS_URL", "http://localhost:8000"))
    p.add_argument("--user", default=os.environ.get("EARS_USER", "debate"),
                   help="site password user (env EARS_USER); the password comes from env EARS_PASSWORD")
    p.add_argument("--map", default="mic1=A,mic2=B", help="source=speaker pairs")
    p.add_argument("--pause-after", type=float, default=6.0,
                   help="least seconds of no new sentence before a Pause (more after a long sentence, up to 15)")
    p.add_argument("--pace", action="store_true", help="replay stdin at the speed its start times say")
    p.add_argument("--dry-run", action="store_true", help="print events instead of sending them")
    args = p.parse_args()

    publisher = Publisher(args.url, args.user, os.environ.get("EARS_PASSWORD", ""), args.dry_run)
    bridge = Bridge(publisher, parse_map(args.map), args.pause_after)
    print(f"bridge: sending to {publisher.url} as {parse_map(args.map)}", file=sys.stderr)
    first_start = first_wall = None
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if line.get("type", "sentence") != "sentence":
                continue
            if args.pace:
                end = float(line.get("end", 0) or 0)
                if first_start is None:
                    first_start, first_wall = end, time.time()
                time.sleep(max(0.0, (end - first_start) - (time.time() - first_wall)))
            bridge.sentence(line)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.finish()                             # end of input: the last speaker has stopped too
        publisher.close()


if __name__ == "__main__":
    main()
