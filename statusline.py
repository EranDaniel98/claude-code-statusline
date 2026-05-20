"""Statusline: project | ● | model | ctx% | rate-limits | elapsed.

Claude Code does not refresh the statusline during silent extended thinking,
so we rely on refreshInterval (set in settings.json) and read the transcript
JSONL mtime as the liveness signal.

Set CLAUDE_STATUSLINE_DEBUG=1 to dump the raw stdin payload to
~/.claude/statusline-payload.json for inspection.
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# Mirror watcher.py thresholds so the two surfaces classify identically.
SLOW_THRESHOLD = 60.0
STUCK_THRESHOLD = 180.0


def ansi(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _win_console_width() -> int | None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT),
            ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT),
            ("Bottom", wintypes.SHORT),
        ]

    class CSBI(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    try:
        k32 = ctypes.windll.kernel32
        handle = k32.CreateFileW("CONOUT$", 0xC0000000, 0x3, None, 3, 0, None)
        if not handle or handle == -1:
            return None
        try:
            csbi = CSBI()
            if k32.GetConsoleScreenBufferInfo(handle, ctypes.byref(csbi)):
                width = csbi.srWindow.Right - csbi.srWindow.Left + 1
                return width if width > 0 else None
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None
    return None


def detect_terminal_width() -> int | None:
    col = os.environ.get("COLUMNS")
    if col and col.isdigit():
        n = int(col)
        if n > 0:
            return n
    if sys.platform == "win32":
        w = _win_console_width()
        if w:
            return w
    try:
        return os.get_terminal_size(1).columns
    except OSError:
        return None


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    m = n / 1_000_000
    return f"{int(m)}M" if m == int(m) else f"{m:.1f}M"


def render_context(data: dict) -> str:
    ctx = data.get("context_window") or {}
    pct = ctx.get("used_percentage")
    total = ctx.get("context_window_size")
    usage = ctx.get("current_usage") or {}
    consumed: int | None = None
    if usage:
        consumed = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        )
    if pct is None and consumed is not None and total:
        pct = (consumed / total) * 100
    if pct is None and consumed is None:
        return ""

    bits: list[str] = []
    if pct is not None:
        bits.append(f"{int(pct)}%")
    if consumed is not None:
        bits.append(f"({fmt_tokens(consumed)})")
    label = " ".join(bits)

    if pct is None or pct < 60:
        return ansi(label, "2;37")
    if pct < 80:
        return ansi(label, "33")
    if pct < 90:
        return ansi(label, "1;33")
    return ansi(label, "1;31")


def _find_session_state(session_id: str) -> dict | None:
    override = os.environ.get("CLAUDE_SESSIONS_DIR")
    sessions_dir = Path(override) if override else Path.home() / ".claude" / "sessions"
    if not sessions_dir.exists():
        return None
    for path in sessions_dir.glob("*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("sessionId") == session_id:
            return state
    return None


def _last_meaningful_timestamp(transcript: str) -> float | None:
    """POSIX timestamp of the last non-thinking JSONL entry, or None.

    Thinking blocks land in the JSONL on completion and update file mtime,
    which would otherwise mask genuine stuckness during a long reasoning turn.
    We walk the tail backwards and skip `subtype == "thinking"` entries.
    """
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            if size > 65536:
                f.seek(size - 65536)
                f.readline()  # drop partial first line in the chunk
            tail = f.read()
    except OSError:
        return None

    for ln in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except (ValueError, TypeError):
            continue
        msg = d.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if (isinstance(content, list) and content
                    and isinstance(content[0], dict)
                    and content[0].get("type") == "thinking"):
                continue
        ts_str = d.get("timestamp")
        if not ts_str:
            continue
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def _meaningful_activity_ts(transcript: str | None) -> float | None:
    """Last meaningful activity as POSIX seconds. Falls back to file mtime
    when the JSONL has no parseable non-thinking entries (empty/brand-new file)."""
    if not transcript or not os.path.exists(transcript):
        return None
    ts = _last_meaningful_timestamp(transcript)
    if ts is not None:
        return ts
    try:
        return os.path.getmtime(transcript)
    except OSError:
        return None


def _transcript_age(transcript: str | None) -> float | None:
    ts = _meaningful_activity_ts(transcript)
    if ts is None:
        return None
    return max(time.time() - ts, 0.0)


def render_status(state: dict | None, transcript: str | None) -> str:
    """Single colored `●` whose color mirrors watcher.py's tray-icon severity:
    green=BUSY, yellow=THINK (silent ≥60s), red=STUCK (≥180s) or WAIT (permission).
    Permission prompts are visually loud in the TUI anyway, so a peripheral red
    dot is enough — saves the line a text label.
    """
    status = state.get("status") if state else None
    if status == "waiting":
        return ansi("●", "1;31")
    if status != "busy":
        return ""
    age = _transcript_age(transcript)
    if age is not None and age >= STUCK_THRESHOLD:
        return ansi("●", "1;31")
    if age is not None and age >= SLOW_THRESHOLD:
        return ansi("●", "1;33")
    return ansi("●", "1;32")


def render_fast(data: dict) -> str:
    if not data.get("fast_mode"):
        return ""
    return ansi("FAST", "1;36")


def _rl_color(pct: float) -> str:
    if pct < 60:
        return "2;37"
    if pct < 85:
        return "33"
    return "1;31"


def render_rate_limits(data: dict) -> str:
    rl = data.get("rate_limits") or {}
    five = (rl.get("five_hour") or {}).get("used_percentage")
    seven = (rl.get("seven_day") or {}).get("used_percentage")
    chunks = []
    if isinstance(five, (int, float)):
        chunks.append(ansi(f"5h:{int(five)}%", _rl_color(five)))
    if isinstance(seven, (int, float)):
        chunks.append(ansi(f"7d:{int(seven)}%", _rl_color(seven)))
    return " ".join(chunks)


def render_last_activity(transcript: str | None) -> str:
    ts = _meaningful_activity_ts(transcript)
    if ts is None:
        return ""
    now = time.time()
    if ts > now:
        # Future-dated timestamp (clock skew, NTP step, copied-with-preserved-timestamps).
        # Collapse to "now" so we never display a wall-clock time in the future.
        ts = now
    secs = now - ts
    label = "last " + time.strftime("%H:%M:%S", time.localtime(ts))
    if secs < 30:
        return ansi(label, "2;37")
    if secs < 120:
        return ansi(label, "33")
    return ansi(label, "1;31")


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        print("claude")
        return

    if os.environ.get("CLAUDE_STATUSLINE_DEBUG"):
        try:
            Path.home().joinpath(".claude", "statusline-payload.json").write_text(
                raw, encoding="utf-8"
            )
        except OSError:
            pass

    workspace = data.get("workspace") or {}
    cwd = workspace.get("current_dir") or data.get("cwd") or ""

    session_id = data.get("session_id") or ""
    state = _find_session_state(session_id) if session_id else None

    project = (
        data.get("session_name")
        or (state.get("name") if state else None)
        or os.path.basename(cwd.rstrip("\\/"))
        or "?"
    )

    raw_model = (data.get("model") or {}).get("display_name") or "claude"
    model = raw_model.split("(")[0].strip()
    if "1M" in raw_model and "1M" not in model:
        model = f"{model} 1M"

    parts = [ansi(project, "1;36")]

    status_part = render_status(state, data.get("transcript_path"))
    if status_part:
        parts.append(status_part)

    parts.append(ansi(model, "2;37"))

    fast_part = render_fast(data)
    if fast_part:
        parts.append(fast_part)

    ctx_part = render_context(data)
    if ctx_part:
        parts.append(ctx_part)

    rl_part = render_rate_limits(data)
    if rl_part:
        parts.append(rl_part)

    last_activity_part = render_last_activity(data.get("transcript_path"))
    if last_activity_part:
        parts.append(last_activity_part)

    width = detect_terminal_width()
    if width and len(parts) > 1:
        total = sum(visible_len(p) for p in parts) + 3 * (len(parts) - 1)
        budget = width - 1
        if total > budget:
            others = sum(visible_len(p) for p in parts[1:]) + 3 * (len(parts) - 1)
            available = budget - others
            project_len = visible_len(parts[0])
            if 4 <= available < project_len:
                truncated = project[: available - 1] + "…"
                parts[0] = ansi(truncated, "1;36")

    print(" | ".join(parts))


if __name__ == "__main__":
    main()
