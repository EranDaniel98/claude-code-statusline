"""Statusline state matrix.

Pipes synthetic payloads into the repo's statusline.py, verifies output for
each state. Sessions/transcript files live in an isolated tmpdir
(CLAUDE_SESSIONS_DIR override) — no pollution of ~/.claude/sessions.

Run: python tests/test_statusline.py
"""
import json
import os
import re
import shutil
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

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUSLINE = REPO_ROOT / "statusline.py"
SESSIONS = Path(tempfile.mkdtemp(prefix="ccs-test-sessions-"))
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible(s: str) -> str:
    return ANSI.sub("", s)


def render(payload, columns=None) -> str:
    env = os.environ.copy()
    env["CLAUDE_SESSIONS_DIR"] = str(SESSIONS)
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


@contextmanager
def jsonl_transcript(entries):
    """entries: list of (age_seconds, is_thinking) tuples, oldest first."""
    from datetime import datetime, timezone, timedelta
    p = Path(tempfile.gettempdir()) / f"stl-jsonl-{uuid4().hex[:8]}.jsonl"
    now = datetime.now(timezone.utc)
    lines = []
    for age, is_thinking in entries:
        ts = (now - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")
        sub = "thinking" if is_thinking else "text"
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": ts,
            "message": {"content": [{"type": sub}]},
        }))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
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

    try:
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
                "waiting session → ▶ WAIT red", out,
                lambda o: "\x1b[1;31m▶ WAIT\x1b[0m" in o,
                "red '▶ WAIT' glyph",
            ))

        with session_file("busy") as sid, transcript_file(age_seconds=75) as tr:
            out = render(payload(sid, tr), columns=120)
            results.append(check(
                "busy + ≥60s silent → ⌛ THINK yellow", out,
                lambda o: "\x1b[1;33m⌛ THINK\x1b[0m" in o,
                "yellow '⌛ THINK'",
            ))

        with session_file("busy") as sid, transcript_file(age_seconds=250) as tr:
            out = render(payload(sid, tr), columns=120)
            results.append(check(
                "busy + ≥180s silent → ⚠ STUCK red", out,
                lambda o: "\x1b[1;31m⚠ STUCK\x1b[0m" in o,
                "red '⚠ STUCK'",
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
                "no session match → no status segment", out,
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

        def last_activity_seg(o: str):
            parts = o.split(" | ")
            if not parts:
                return None
            m = re.match(r"(\x1b\[[0-9;]+m)(.*?)\x1b\[0m$", parts[-1])
            return m.groups() if m else None

        LABEL_RE = re.compile(r"last \d{2}:\d{2}:\d{2}")

        with session_file("busy") as sid, transcript_file(age_seconds=5) as tr:
            out = render(payload(sid, tr, context_window=None, rate_limits=None), columns=120)
            seg = last_activity_seg(out)
            results.append(check(
                "last-activity <30s → dim 'last HH:MM:SS'", out,
                lambda o: seg is not None and seg[0] == "\x1b[2;37m" and LABEL_RE.fullmatch(seg[1]) is not None,
                "dim (2;37), label 'last HH:MM:SS'",
            ))

        with session_file("busy") as sid, transcript_file(age_seconds=75) as tr:
            out = render(payload(sid, tr, context_window=None, rate_limits=None), columns=120)
            seg = last_activity_seg(out)
            results.append(check(
                "last-activity 30-120s → yellow", out,
                lambda o: seg is not None and seg[0] == "\x1b[33m" and LABEL_RE.fullmatch(seg[1]) is not None,
                "yellow (33), label 'last HH:MM:SS'",
            ))

        with session_file("busy") as sid, transcript_file(age_seconds=250) as tr:
            out = render(payload(sid, tr, context_window=None, rate_limits=None), columns=120)
            seg = last_activity_seg(out)
            results.append(check(
                "last-activity ≥120s → bold red", out,
                lambda o: seg is not None and seg[0] == "\x1b[1;31m" and LABEL_RE.fullmatch(seg[1]) is not None,
                "red (1;31), label 'last HH:MM:SS'",
            ))

        with session_file("busy") as sid, transcript_file(age_seconds=-30) as tr:
            out = render(payload(sid, tr, context_window=None, rate_limits=None), columns=120)
            seg = last_activity_seg(out)
            results.append(check(
                "future mtime → clamped, dim 'last HH:MM:SS' (no future leak)", out,
                lambda o: seg is not None and seg[0] == "\x1b[2;37m" and LABEL_RE.fullmatch(seg[1]) is not None,
                "dim, valid HH:MM:SS (collapsed to now)",
            ))

        # JSONL with stale text + fresh thinking → status escalates because
        # thinking entries are skipped when looking up last meaningful activity.
        with session_file("busy") as sid, jsonl_transcript([(75, False), (5, True)]) as tr:
            out = render(payload(sid, tr), columns=120)
            results.append(check(
                "fresh thinking + 75s-old text → ⌛ THINK (thinking skipped)", out,
                lambda o: "\x1b[1;33m⌛ THINK\x1b[0m" in o,
                "yellow '⌛ THINK' despite fresh thinking write",
            ))

        # Fresh non-thinking entry → status stays at ● BUSY.
        with session_file("busy") as sid, jsonl_transcript([(75, True), (5, False)]) as tr:
            out = render(payload(sid, tr), columns=120)
            results.append(check(
                "fresh text → green ●", out,
                lambda o: "\x1b[1;32m●\x1b[0m" in o,
                "green '●' (last meaningful entry is fresh)",
            ))

        # All-thinking transcript → fall back to file mtime so we don't
        # falsely show STUCK on a brand-new turn that only contains thinking.
        with session_file("busy") as sid, jsonl_transcript([(5, True), (3, True)]) as tr:
            out = render(payload(sid, tr), columns=120)
            results.append(check(
                "only-thinking transcript → falls back to file mtime, green ●", out,
                lambda o: "\x1b[1;32m●\x1b[0m" in o,
                "green '●' from file-mtime fallback",
            ))
    finally:
        shutil.rmtree(SESSIONS, ignore_errors=True)

    passed = sum(results)
    total = len(results)
    print()
    print(f"{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
