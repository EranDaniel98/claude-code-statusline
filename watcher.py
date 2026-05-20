"""External watcher: cross-window status panel for Claude Code.

The in-TUI statusline only repaints on Claude Code events, so it can't reliably
show "is this session stuck?" in real time. This watcher polls all session
state files and their transcripts on its own schedule and prints a live table.
Run it in a separate terminal alongside your Claude Code windows.

Detects three states:
  ●  BUSY    session.status == "busy" and transcript was touched recently
  ⌛ THINK   session.status == "busy" but transcript silent ≥60s (yellow)
  ⚠  STUCK  session.status == "busy" and transcript silent ≥180s (red, fires sound)
  ▶  WAIT   session.status == "waiting" (permission prompt, red, fires sound)
  ·  idle   session.status == "idle"

Usage:
  python watcher.py                  # default 300ms poll, ~/.claude/sessions/
  python watcher.py --interval 1.0   # slower poll
  python watcher.py --no-sound       # don't beep on STUCK / WAIT transitions
  python watcher.py --sessions-dir <path>

Env vars:
  CLAUDE_SESSIONS_DIR    override sessions dir (same as --sessions-dir)
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


STUCK_THRESHOLD = 180.0
SLOW_THRESHOLD = 60.0
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def ansi(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def fmt_elapsed(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


@dataclass
class SessionInfo:
    pid: int
    session_id: str
    cwd: str
    status: str
    project: str
    transcript_age: float | None


def _encoded_cwd(cwd: str) -> str:
    return cwd.replace("\\", "-").replace("/", "-").replace(":", "-")


def find_transcript(session_id: str, cwd: str) -> Path | None:
    if not session_id:
        return None
    candidates = []
    if cwd:
        candidates.append(PROJECTS_DIR / _encoded_cwd(cwd) / f"{session_id}.jsonl")
    if PROJECTS_DIR.exists():
        for sub in PROJECTS_DIR.iterdir():
            if not sub.is_dir():
                continue
            p = sub / f"{session_id}.jsonl"
            if p.exists():
                candidates.append(p)
    for c in candidates:
        if c.exists():
            return c
    return None


def transcript_age(session_id: str, cwd: str) -> float | None:
    p = find_transcript(session_id, cwd)
    if not p:
        return None
    try:
        return time.time() - p.stat().st_mtime
    except OSError:
        return None


def scan_sessions(sessions_dir: Path) -> list[SessionInfo]:
    if not sessions_dir.exists():
        return []
    out: list[SessionInfo] = []
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cwd = data.get("cwd") or ""
        project = os.path.basename(cwd.rstrip("\\/")) or "(?)"
        out.append(SessionInfo(
            pid=int(data.get("pid") or 0),
            session_id=data.get("sessionId") or "",
            cwd=cwd,
            status=data.get("status") or "idle",
            project=project,
            transcript_age=transcript_age(data.get("sessionId") or "", cwd),
        ))
    return out


def classify(s: SessionInfo) -> tuple[str, str, str]:
    """Returns (label, color_code, severity). severity in {"normal","warn","alert"}."""
    if s.status == "waiting":
        return "▶ WAIT", "1;31", "alert"
    if s.status == "busy":
        age = s.transcript_age
        if age is not None and age >= STUCK_THRESHOLD:
            return "⚠ STUCK", "1;31", "alert"
        if age is not None and age >= SLOW_THRESHOLD:
            return "⌛ THINK", "1;33", "warn"
        return "● BUSY", "1;32", "normal"
    if s.status == "idle":
        return "· idle", "2;37", "normal"
    return f"  {s.status}", "2;37", "normal"


def beep() -> None:
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep(0x30)
            return
        except Exception:
            pass
    sys.stdout.write("\a")
    sys.stdout.flush()


def clear_screen() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")


def render(sessions: list[SessionInfo]) -> None:
    clear_screen()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(ansi(f"Claude Code Watcher · {now}", "1;36"))
    print("─" * 78)
    print(f"{'PID':>7}  {'STATE':<10}  {'PROJECT':<32}  {'ELAPSED':>8}")
    print("─" * 78)
    if not sessions:
        print(ansi("  (no Claude Code sessions found)", "2;37"))
    for s in sessions:
        label, color, _ = classify(s)
        project = s.project[:32]
        elapsed = fmt_elapsed(s.transcript_age) if s.transcript_age is not None else "—"
        line = f"{s.pid:>7}  {label:<10}  {project:<32}  {elapsed:>8}"
        print(ansi(line, color))
    print("─" * 78)
    print(ansi("Ctrl-C to exit", "2;37"))
    sys.stdout.flush()


def diff_alerts(prev: dict[str, str], curr: dict[str, str]) -> list[str]:
    """Return session_ids whose severity escalated to 'alert' since last tick."""
    escalated = []
    for sid, sev in curr.items():
        if sev == "alert" and prev.get(sid) != "alert":
            escalated.append(sid)
    return escalated


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.3,
                    help="poll interval in seconds (default: 0.3)")
    ap.add_argument("--sessions-dir", default=None,
                    help="override sessions dir (default: $CLAUDE_SESSIONS_DIR or ~/.claude/sessions)")
    ap.add_argument("--no-sound", action="store_true",
                    help="do not beep on STUCK / WAIT transitions")
    args = ap.parse_args()

    sessions_dir_str = args.sessions_dir or os.environ.get("CLAUDE_SESSIONS_DIR")
    sessions_dir = Path(sessions_dir_str) if sessions_dir_str else (
        Path.home() / ".claude" / "sessions"
    )

    prev_severity: dict[str, str] = {}
    try:
        while True:
            sessions = scan_sessions(sessions_dir)
            render(sessions)

            curr_severity = {s.session_id or f"pid{s.pid}": classify(s)[2] for s in sessions}
            if not args.no_sound:
                for _ in diff_alerts(prev_severity, curr_severity):
                    beep()
            prev_severity = curr_severity

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
