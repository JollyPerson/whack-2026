"""Pretty-print a JSONL event log, or follow it live like `tail -f`. Same one-line format as PRINT=1.

  python -m app.tools.tail logs/latest.jsonl         # whole file
  python -m app.tools.tail -f                        # last 20 lines of logs/latest.jsonl, then follow;
                                                     # keeps following when a server restart re-points latest
Options: -n N · --interims (show interim Utterances) · --only Claim Verdict · --skip X Y (default
DirectorStatus; `--skip` alone shows everything) · --no-color
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from app.mocks.printer import fmt

ANSI = re.compile(r"\033\[[0-9;]*m")
DEFAULT_LOG = Path(__file__).resolve().parents[2] / "logs" / "latest.jsonl"


def render(line: str, interims: bool = False, only: Iterable[str] = (), skip: Iterable[str] = (),
           color: bool = True) -> Optional[str]:
    """One log line -> one display line, or None when filtered out."""
    line = line.strip()
    if not line or line.startswith("//"):
        return None
    try:
        d = json.loads(line)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return f"  ?? unparseable line: {line[:160]}"
    name = d.get("event")
    if (only and name not in only) or name in skip:
        return None
    if name == "Utterance" and not d.get("final", True) and not interims:
        return None
    s = fmt(d)
    return s if color else ANSI.sub("", s)


def read_complete(path: Path) -> tuple[list[str], int]:
    """Complete lines of the file and the byte offset just past the last newline (a line being written
    right now is left for follow())."""
    data = path.read_bytes() if path.exists() else b""
    end = data.rfind(b"\n") + 1
    return data[:end].decode("utf-8", "replace").splitlines(), end


def follow(path: Path, emit: Callable[[str], None], offset: int = 0, poll: float = 0.2,
           stop: Callable[[], bool] = lambda: False) -> None:
    """Emit each new complete line. Reopens from the start when `path` resolves to a new file."""
    f, ident, buf = None, None, ""
    try:
        while not stop():
            try:
                st = os.stat(path)                       # follows the latest.jsonl symlink
            except OSError:
                time.sleep(poll)
                continue
            if (st.st_dev, st.st_ino) != ident:
                if f:
                    f.close()
                    offset = 0
                    emit(f"// --- now following {os.path.realpath(path)}")
                f, ident, buf = open(path, encoding="utf-8", errors="replace"), (st.st_dev, st.st_ino), ""
                f.seek(offset)
            chunk = f.read()
            if not chunk:
                time.sleep(poll)
                continue
            *lines, buf = (buf + chunk).split("\n")
            for ln in lines:
                emit(ln)
    finally:
        if f:
            f.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tools.tail", description="Pretty-print / follow a JSONL log")
    ap.add_argument("path", nargs="?", default=str(DEFAULT_LOG))
    ap.add_argument("-f", "--follow", action="store_true")
    ap.add_argument("-n", type=int, default=None, help="start with the last N lines (default: all, or 20 with -f)")
    ap.add_argument("--interims", action="store_true")
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--skip", nargs="*", default=["DirectorStatus"])
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args(argv)
    color = not a.no_color and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    only, skip = set(a.only), set(a.skip)

    def show(line: str) -> None:
        if line.startswith("// ---"):
            print(line, flush=True)
            return
        s = render(line, a.interims, only, skip, color)
        if s:
            print(s, flush=True)

    path = Path(a.path)
    lines, offset = read_complete(path)
    if not a.follow and path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    n = a.n if a.n is not None else (20 if a.follow else None)
    shown = [ln for ln in lines if render(ln, a.interims, only, skip, False)]
    if n is not None:
        shown = shown[-n:] if n > 0 else []
    for ln in shown:
        show(ln)
    if a.follow:
        try:
            follow(path, show, offset)
        except KeyboardInterrupt:
            pass
    elif not path.exists():
        print(f"no such log: {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
