"""Statusline state matrix.

Pipes synthetic payloads into statusline.py, verifies output for each state.
Creates throwaway session/transcript files (uuid-suffixed, cleaned in finally).

Run: python ~/.claude/tests/test_statusline.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path.home() / ".claude"
STATUSLINE = ROOT / "statusline.py"
SESSIONS = ROOT / "sessions"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible(s: str) -> str:
    return ANSI.sub("", s)


def render(payload, columns=None) -> str:
    env = os.environ.copy()
    if columns is not None:
        env["COLUMNS"] = str(columns)
    elif "COLUMNS" in env:
        del env["COLUMNS"]
    body = json.dumps(payload) if isinstance(payload, dict) else payload
    r = subprocess.run(
        [sys.executable, str(STATUSLINE)],
        input=body, capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace",
    )
    return (r.stdout or "").rstrip("\n")


@contextmanager
def session_file(status: str):
    sid = f"test-{uuid4().hex[:8]}"
    p = SESSIONS / f"{sid}.json"
    p.write_text(json.dumps({"pid": 0, "sessionId": sid, "status": status, "cwd": ""}))
    try:
        yield sid
    finally:
        p.unlink(missing_ok=True)


@contextmanager
def transcript_file(age_seconds: float = 0):
    p = Path(tempfile.gettempdir()) / f"stl-test-{uuid4().hex[:8]}.jsonl"
    p.write_text("")
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(p, (t, t))
    try:
        yield str(p)
    finally:
        p.unlink(missing_ok=True)


def payload(session_id="no-match", transcript_path="", **overrides) -> dict:
    base = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": "C:\\test",
        "session_name": "test project",
        "model": {"display_name": "Opus 4.7 (1M context)"},
        "workspace": {"current_dir": "C:\\test"},
        "context_window": {
            "used_percentage": 13,
            "context_window_size": 200000,
            "current_usage": {
                "input_tokens": 26000,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 19},
            "seven_day": {"used_percentage": 48},
        },
    }
    base.update(overrides)
    return base


def check(name: str, out: str, predicate, expected: str) -> bool:
    ok = predicate(out)
    mark = "PASS" if ok else "FAIL"
    v = visible(out) or "(empty)"
    print(f"[{mark}] {name}")
    print(f"       expect: {expected}")
    print(f"       got:    {v}")
    return ok


def main() -> None:
    results: list[bool] = []

    with session_file("busy") as sid, transcript_file(age_seconds=1) as tr:
        out = render(payload(sid, tr), columns=120)
        results.append(check(
            "busy session → green ●", out,
            lambda o: "\x1b[1;32m●" in o,
            "green ● (code 1;32)",
        ))

    with session_file("waiting") as sid, transcript_file(age_seconds=500) as tr:
        out = render(payload(sid, tr), columns=120)
        results.append(check(
            "waiting session → WAIT", out,
            lambda o: "WAIT" in visible(o), "WAIT present",
        ))

    with session_file("idle") as sid, transcript_file(age_seconds=500) as tr:
        out = render(payload(sid, tr), columns=120)
        results.append(check(
            "idle + stale → no status segment", out,
            lambda o: "●" not in o and "WAIT" not in visible(o),
            "no ● / no WAIT",
        ))

    with transcript_file(age_seconds=1) as tr:
        out = render(payload("ghost-session", tr), columns=120)
        results.append(check(
            "no session match → no status (mtime fallback removed)", out,
            lambda o: "●" not in o and "WAIT" not in visible(o),
            "no status segment",
        ))

    out = render("not json {", columns=120)
    results.append(check(
        "malformed JSON → 'claude' fallback", out,
        lambda o: visible(o) == "claude", "literal 'claude'",
    ))

    long_name = "a-very-long-project-name-that-must-be-truncated"
    with session_file("busy") as sid:
        out = render(payload(sid, session_name=long_name), columns=60)
        results.append(check(
            "60col truncates project", out,
            lambda o: len(visible(o)) <= 60 and "…" in visible(o),
            "≤60 chars, contains …",
        ))

    with session_file("busy") as sid:
        out = render(payload(sid, session_name=long_name), columns=80)
        results.append(check(
            "80col truncates project", out,
            lambda o: len(visible(o)) <= 80,
            "≤80 chars",
        ))

    with session_file("busy") as sid:
        out = render(payload(sid, session_name="short"), columns=120)
        results.append(check(
            "120col no truncation needed", out,
            lambda o: "…" not in visible(o), "no ellipsis",
        ))

    minimal = {
        "session_id": "ghost",
        "cwd": "C:\\test",
        "model": {"display_name": "Opus 4.7"},
        "workspace": {"current_dir": "C:\\test"},
    }
    out = render(minimal, columns=120)
    results.append(check(
        "minimal payload renders without crash", out,
        lambda o: "Opus 4.7" in visible(o), "model name appears",
    ))

    passed = sum(results)
    total = len(results)
    print()
    print(f"{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
