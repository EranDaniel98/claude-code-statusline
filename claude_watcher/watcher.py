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
  python watcher.py --tray           # headless: system tray icon (Windows/macOS/Linux)

Env vars:
  CLAUDE_SESSIONS_DIR    override sessions dir (same as --sessions-dir)
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# Thresholds override-able via env vars so users can tune without editing source.
# Defaults match the historical 60s/180s behavior.
SLOW_THRESHOLD = float(os.environ.get("CLAUDE_WATCHER_SLOW_SECONDS", "60"))
STUCK_THRESHOLD = float(os.environ.get("CLAUDE_WATCHER_STUCK_SECONDS", "180"))


# --- Structured logging ---------------------------------------------------
# Single append-only JSONL file at ~/.claude/claude-watcher.log. Rotates
# when it crosses _LOG_MAX_BYTES (one backup at .bak). Best-effort: every
# write is wrapped — logging must never break the watcher itself.

import threading as _threading
_LOG_PATH = Path.home() / ".claude" / "claude-watcher.log"
_LOG_MAX_BYTES = 1_000_000
_LOG_LOCK = _threading.Lock()


def _log(event: str, level: str = "INFO", **fields) -> None:
    """Append one JSON line to the log file. Silent on failure."""
    try:
        with _LOG_LOCK:
            try:
                if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _LOG_MAX_BYTES:
                    _LOG_PATH.replace(_LOG_PATH.with_suffix(".log.bak"))
            except OSError:
                pass
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "level": level,
                "event": event,
                **fields,
            }
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass
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


def _tool_in_flight(path: Path) -> str | None:
    """Name of the tool whose `assistant/tool_use` has not yet been matched
    by a `user/tool_result`, or None. Walks the tail forward, tracks the
    last unresolved tool_use. Tail-only, so very old in-flight tools (>16KB
    of intervening JSONL) won't be reported — acceptable in practice."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 16384:
                f.seek(size - 16384)
                f.readline()
            tail = f.read()
    except OSError:
        return None
    pending = None
    for ln in tail.decode("utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except (ValueError, TypeError):
            continue
        t = d.get("type")
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not (isinstance(content, list) and content):
            continue
        # An assistant turn can emit multiple tool_use entries in one message
        # (parallel tools). Look at every item, not just the first.
        for item in content:
            if not isinstance(item, dict):
                continue
            ctype = item.get("type")
            if t == "assistant" and ctype == "tool_use":
                pending = item.get("name", "?")
            elif t == "user" and ctype == "tool_result":
                pending = None
    return pending


# psutil is optional; CPU% is shown when available, otherwise hidden.
try:
    import psutil as _psutil  # noqa: F401
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

_cpu_proc_cache: dict[int, object] = {}


def session_cpu_percent(pid: int) -> float | None:
    """CPU% for the process `pid` (claude.exe) as a fraction of total
    machine CPU (0..100%, not 0..N*100%). Returns None on first call
    for a given pid (psutil needs two samples to compute a delta) and
    on any error.

    psutil's Process.cpu_percent is process-wide and can exceed 100% on
    multi-core systems (one process running on multiple cores). We
    divide by logical core count so the number stays in 0..100% range
    and reads as "how much of the whole machine Claude is using" —
    more intuitive than the raw multi-core number.
    """
    if not _HAS_PSUTIL or pid <= 0:
        return None
    import psutil
    try:
        p = _cpu_proc_cache.get(pid)
        if p is None or not p.is_running():
            p = psutil.Process(pid)
            _cpu_proc_cache[pid] = p
            p.cpu_percent()  # priming sample; first real read is on next call
            return None
        cores = psutil.cpu_count(logical=True) or 1
        return p.cpu_percent() / cores
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        _cpu_proc_cache.pop(pid, None)
        return None


def _last_meaningful_timestamp(path: Path) -> float | None:
    """POSIX timestamp of the last non-thinking JSONL entry, or None.

    Skips `subtype == "thinking"` entries so extended-thinking writes don't
    mask a session that's been silently reasoning. Matches statusline.py.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 65536:
                f.seek(size - 65536)
                f.readline()
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


def transcript_age(session_id: str, cwd: str) -> float | None:
    p = find_transcript(session_id, cwd)
    if not p:
        return None
    ts = _last_meaningful_timestamp(p)
    if ts is None:
        try:
            ts = p.stat().st_mtime
        except OSError:
            return None
    return max(time.time() - ts, 0.0)


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
    elif sys.platform == "darwin":
        # Terminal bell is muted in most Mac terminals; afplay a system sound.
        import subprocess
        try:
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
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


def overall_severity(sessions: list[SessionInfo]) -> str:
    """Loudest severity across all sessions, for the tray icon color.

    alert > warn > normal-busy > idle. Empty session list → 'idle'.
    """
    severities = [classify(s)[2] for s in sessions]
    if "alert" in severities:
        return "alert"
    if "warn" in severities:
        return "warn"
    if any(s.status == "busy" for s in sessions):
        return "normal"
    return "idle"


_SEVERITY_COLORS = {
    "idle":   (158, 158, 158, 255),  # gray
    "normal": (76, 175, 80, 255),    # green
    "warn":   (255, 193, 7, 255),    # yellow
    "alert":  (244, 67, 54, 255),    # red
}


def build_tooltip(sessions: list[SessionInfo]) -> str:
    """Multi-line tray tooltip. Capped at 120 chars (Windows NotifyIcon limit)."""
    if not sessions:
        return "Claude Code · no sessions"
    lines = ["Claude Code"]
    for s in sessions:
        label = classify(s)[0].strip()
        proj = s.project[:18]
        lines.append(f"  {proj}: {label}")
    return "\n".join(lines)[:120]


def _make_dot_icon(severity: str):
    """Simple colored circle for the tray. Color reflects severity."""
    from PIL import Image, ImageDraw
    color = _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["idle"])
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, size - 6, size - 6), fill=color)
    return img


def _toast(title: str, body: str) -> None:
    """Fire a desktop notification. Best-effort, non-blocking.
    Spawns the platform notifier as a detached child so a slow OS call
    doesn't stall the watcher poll loop."""
    import subprocess
    try:
        if sys.platform == "win32":
            # PowerShell + WinRT inline. Slow (~300ms) but no extra dep.
            ps_title = title.replace('"', '`"')
            ps_body = body.replace('"', '`"')
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager,"
                "Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
                "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
                "ContentType=WindowsRuntime] | Out-Null;"
                "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
                f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
                f"<text>{ps_title}</text><text>{ps_body}</text>"
                "</binding></visual></toast>');"
                "$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]::"
                "CreateToastNotifier('Anthropic.ClaudeCode').Show($t)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                close_fds=True,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                close_fds=True,
            )
        else:
            # Linux: requires libnotify (notify-send). Fail silently if missing.
            subprocess.Popen(
                ["notify-send", title, body],
                close_fds=True,
            )
    except Exception:
        pass


def _autostart_install() -> int:
    """Register the tray watcher to launch on user login. Returns exit code."""
    if sys.platform == "win32":
        return _autostart_install_windows()
    if sys.platform == "darwin":
        return _autostart_install_macos()
    return _autostart_install_linux()


def _autostart_uninstall() -> int:
    if sys.platform == "win32":
        return _autostart_uninstall_windows()
    if sys.platform == "darwin":
        return _autostart_uninstall_macos()
    return _autostart_uninstall_linux()


_AUTOSTART_NAME = "ClaudeCodeWatcher"
_CONFIG_PATH = Path.home() / ".claude" / "watcher.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_config(cfg: dict) -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


SNOOZE_DURATIONS = [
    ("Snooze 10 min", 10 * 60),
    ("Snooze 30 min", 30 * 60),
    ("Snooze 1 hour", 60 * 60),
]


def is_snoozed(cfg: dict, session_id: str) -> bool:
    """True if `session_id` has a future snooze_until in cfg['snoozed'].
    Auto-prunes expired entries from the dict (in place)."""
    snoozed = cfg.get("snoozed")
    if not isinstance(snoozed, dict):
        return False
    until = snoozed.get(session_id)
    if until is None:
        return False
    if time.time() >= until:
        snoozed.pop(session_id, None)
        return False
    return True


def _watcher_launch_vbs_path() -> Path:
    """Where the watcher's VBS launcher lives. User data dir so we don't
    write inside the (pip-installed) package directory."""
    return Path.home() / ".claude" / "_claude_watcher_launch.vbs"


def _hook_launch_vbs_path() -> Path:
    """Sibling VBS used by the SessionStart hook to launch
    launch_watcher silently. Separate file so we don't have to deal with
    nested-quoting WScript args."""
    return Path.home() / ".claude" / "_claude_watcher_hook.vbs"


def _write_launch_vbs() -> Path:
    """Drop a VBScript wrapper that launches the watcher with a hidden
    window. Required because uv-built venvs ship `pythonw.exe` as a
    trampoline shim that still flashes a console; wscript + WshShell.Run
    style=0 forces a truly hidden window regardless of the target."""
    py = _venv_python_path(prefer_windowless=True)
    vbs_path = _watcher_launch_vbs_path()
    vbs_path.parent.mkdir(parents=True, exist_ok=True)
    inner = f'""{py}"" -m claude_watcher.watcher --tray'
    body = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run "{inner}", 0, False\r\n'
    )
    vbs_path.write_text(body, encoding="utf-8")
    return vbs_path


def _write_hook_launch_vbs() -> Path:
    """Same idea as _write_launch_vbs but for the SessionStart hook —
    runs launch_watcher.py silently, which then spawns the watcher."""
    py = _venv_python_path(prefer_windowless=True)
    vbs_path = _hook_launch_vbs_path()
    vbs_path.parent.mkdir(parents=True, exist_ok=True)
    inner = f'""{py}"" -m claude_watcher.launch_watcher'
    body = (
        'Set WshShell = CreateObject("WScript.Shell")\r\n'
        f'WshShell.Run "{inner}", 0, False\r\n'
    )
    vbs_path.write_text(body, encoding="utf-8")
    return vbs_path


def _watcher_launch_command() -> str:
    """Command line registered in HKCU\\…\\Run. Goes through wscript so
    the launch is truly windowless even with uv's trampoline pythonw."""
    if sys.platform == "win32":
        return f'wscript.exe "{_watcher_launch_vbs_path()}"'
    py = _venv_python_path()
    return f'"{py}" -m claude_watcher.watcher --tray'


def _autostart_install_windows() -> int:
    try:
        vbs = _write_launch_vbs()
        cmd = _watcher_launch_command()
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as k:
            winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
        _log("autostart_install", os="windows", method="run_key", name=_AUTOSTART_NAME)
        print(f"Installed autostart (HKCU…\\Run\\{_AUTOSTART_NAME}):")
        print(f"  {cmd}")
        print(f"Launcher: {vbs}")
        return 0
    except OSError as e:
        print(f"autostart install failed: {e}", file=sys.stderr)
        return 1


def _autostart_uninstall_windows() -> int:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        ) as k:
            try:
                winreg.DeleteValue(k, _AUTOSTART_NAME)
                _log("autostart_uninstall", os="windows", name=_AUTOSTART_NAME)
                print(f"Removed autostart (HKCU…\\Run\\{_AUTOSTART_NAME})")
            except FileNotFoundError:
                print("No autostart entry to remove.")
        vbs = _watcher_launch_vbs_path()
        if vbs.exists():
            vbs.unlink()
            print(f"Removed launcher: {vbs}")
        return 0
    except OSError as e:
        print(f"autostart uninstall failed: {e}", file=sys.stderr)
        return 1


def _autostart_install_macos() -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.anthropic.claude-code-watcher.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.anthropic.claude-code-watcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>-m</string>
    <string>claude_watcher.watcher</string>
    <string>--tray</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    _log("autostart_install", os="macos", method="launchd_plist", path=str(plist_path))
    print(f"Installed autostart plist: {plist_path}")
    # Try to load it now so it starts immediately and survives reboot.
    import subprocess
    try:
        # Unload first in case a previous version is loaded; ignore failure.
        subprocess.run(["launchctl", "unload", str(plist_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = subprocess.run(["launchctl", "load", str(plist_path)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print("Loaded into launchd; the watcher is starting now.")
        else:
            print(f"launchctl load returned {r.returncode}: {r.stderr.strip()}")
            print(f"You may need to load it manually: launchctl load {plist_path}")
    except Exception as e:
        print(f"Auto-load failed: {e}")
        print(f"Load manually: launchctl load {plist_path}")
    return 0


def _autostart_uninstall_macos() -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.anthropic.claude-code-watcher.plist"
    if plist_path.exists():
        import subprocess
        try:
            subprocess.run(["launchctl", "unload", str(plist_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        plist_path.unlink()
        _log("autostart_uninstall", os="macos", path=str(plist_path))
        print(f"Removed: {plist_path}")
    else:
        print("No autostart plist to remove.")
    return 0


def _venv_python_path(prefer_windowless: bool = False) -> str:
    """Path to a Python executable in the current environment, preferring
    pythonw.exe on Windows when `prefer_windowless` is set so the
    invoking process doesn't flash a console window."""
    py = Path(sys.executable)
    if sys.platform == "win32" and prefer_windowless:
        pyw = py.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(py)


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _claude_hook_command() -> str:
    """Quoted command string to put in settings.json hooks.SessionStart.
    On Windows we route through a VBS wrapper so neither the launcher
    nor the spawned watcher flashes a console window."""
    if sys.platform == "win32":
        return f'wscript.exe "{_hook_launch_vbs_path()}"'
    py = _venv_python_path()
    return f'"{py}" -m claude_watcher.launch_watcher'


def _claude_hook_install() -> int:
    """Add a SessionStart hook to ~/.claude/settings.json that launches
    the tray watcher (idempotently) when Claude opens a session."""
    settings_path = _claude_settings_path()

    # On Windows, generate the two VBS launchers (one for the hook, one
    # for the watcher) so the whole chain stays fully hidden.
    if sys.platform == "win32":
        try:
            _write_hook_launch_vbs()
            _write_launch_vbs()
        except Exception as e:
            print(f"warning: could not generate VBS launchers: {e}", file=sys.stderr)

    command = _claude_hook_command()

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"could not parse {settings_path}: {e}", file=sys.stderr)
            return 1

    hooks = settings.setdefault("hooks", {})
    session_start = hooks.setdefault("SessionStart", [])

    for entry in session_start:
        for h in entry.get("hooks", []) or []:
            if h.get("command") == command:
                print("SessionStart hook already registered; nothing to do.")
                return 0

    session_start.append({"hooks": [{"type": "command", "command": command}]})

    if settings_path.exists():
        try:
            settings_path.with_suffix(".json.bak").write_text(
                settings_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:
            pass

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    _log("hook_register", command=command, settings=str(settings_path))
    print(f"Registered SessionStart hook in {settings_path}")
    print(f"  {command}")
    return 0


def _claude_hook_uninstall() -> int:
    settings_path = _claude_settings_path()
    if not settings_path.exists():
        print("No settings.json to edit.")
        return 0
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"could not parse {settings_path}: {e}", file=sys.stderr)
        return 1

    # Markers that identify our hook regardless of whether settings
    # was written by the current code path or an older one (which used
    # the in-repo hooks/launch_watcher.py path).
    markers = [
        "claude_watcher.launch_watcher",
        "_claude_watcher_hook.vbs",
        "launch_watcher.py",
    ]
    hooks = settings.get("hooks") or {}
    session_start = hooks.get("SessionStart") or []
    removed = 0
    new_entries: list = []
    for entry in session_start:
        new_hooks = []
        for h in entry.get("hooks", []) or []:
            cmd = h.get("command") or ""
            if any(m in cmd for m in markers):
                removed += 1
                continue
            new_hooks.append(h)
        if new_hooks:
            entry = dict(entry)
            entry["hooks"] = new_hooks
            new_entries.append(entry)

    if removed == 0:
        print("No matching SessionStart hook to remove.")
        return 0

    if new_entries:
        hooks["SessionStart"] = new_entries
    else:
        hooks.pop("SessionStart", None)
        if not hooks:
            settings.pop("hooks", None)

    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    _log("hook_unregister", removed=removed, settings=str(settings_path))
    print(f"Removed {removed} hook entry/entries from {settings_path}")
    # Clean up the hook VBS on Windows. Leave the watcher VBS alone —
    # OS-login autostart may still be using it.
    if sys.platform == "win32":
        hook_vbs = _hook_launch_vbs_path()
        if hook_vbs.exists():
            try:
                hook_vbs.unlink()
            except OSError:
                pass
    return 0


def _autostart_install_linux() -> int:
    desktop_dir = Path.home() / ".config" / "autostart"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop_path = desktop_dir / "claude-code-watcher.desktop"
    body = f"""[Desktop Entry]
Type=Application
Name=Claude Code Watcher
Exec={sys.executable} -m claude_watcher.watcher --tray
X-GNOME-Autostart-enabled=true
"""
    desktop_path.write_text(body, encoding="utf-8")
    _log("autostart_install", os="linux", method="xdg_desktop", path=str(desktop_path))
    print(f"Installed autostart: {desktop_path}")
    return 0


def _autostart_uninstall_linux() -> int:
    desktop_path = Path.home() / ".config" / "autostart" / "claude-code-watcher.desktop"
    if desktop_path.exists():
        desktop_path.unlink()
        _log("autostart_uninstall", os="linux", path=str(desktop_path))
        print(f"Removed: {desktop_path}")
    else:
        print("No autostart .desktop to remove.")
    return 0


def run_tray(args, sessions_dir: Path) -> None:
    """Headless mode: drive a system tray icon from the polling loop.
    Tray icon is a single colored circle; color = loudest severity."""
    try:
        import pystray  # noqa: F401
    except ImportError:
        print("--tray requires the pystray package. Install with:")
        print("  uv pip install --system pystray pillow")
        print("  (or `pip install pystray pillow`)")
        sys.exit(2)

    import pystray
    import subprocess
    import threading

    icons = {sev: _make_dot_icon(sev) for sev in ("idle", "normal", "warn", "alert")}

    # Settings persist across runs in ~/.claude/watcher.json. CLI --theme
    # still wins for the *initial* launch; the menu toggle overwrites it.
    cfg = _load_config()
    state = {
        "theme": cfg.get("theme") or args.theme,
        "pinned": bool(cfg.get("pin_flyout", False)),
    }

    stop_event = threading.Event()

    def on_quit(icon, _item):
        _log("tray_quit")
        stop_event.set()
        icon.stop()

    def open_flyout(_icon=None, _item=None):
        cmd = [sys.executable, str(Path(__file__).resolve()), "--flyout",
               "--sessions-dir", str(sessions_dir),
               "--theme", state["theme"]]
        if state["pinned"]:
            cmd.append("--pin")
        popen_kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            # On macOS/Linux give the flyout its own session so it survives
            # if the tray process is killed and isn't tied to our stdio.
            popen_kwargs["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **popen_kwargs)
        except Exception:
            pass

    def toggle_dark(icon, _item):
        state["theme"] = "dark" if state["theme"] != "dark" else "light"
        cfg["theme"] = state["theme"]
        _save_config(cfg)
        _log("theme_toggle", theme=state["theme"])
        icon.update_menu()

    def toggle_pin(icon, _item):
        state["pinned"] = not state["pinned"]
        cfg["pin_flyout"] = state["pinned"]
        _save_config(cfg)
        _log("pin_toggle", pinned=state["pinned"])
        icon.update_menu()

    def do_snooze(session_id: str, seconds: int):
        def handler(icon, _item):
            cfg.setdefault("snoozed", {})[session_id] = time.time() + seconds
            _save_config(cfg)
            _log("snooze", session=session_id[:8], minutes=seconds // 60)
            icon.update_menu()
        return handler

    def do_unsnooze(session_id: str):
        def handler(icon, _item):
            snoozed = cfg.get("snoozed")
            if isinstance(snoozed, dict):
                snoozed.pop(session_id, None)
                _save_config(cfg)
                _log("unsnooze", session=session_id[:8])
                icon.update_menu()
        return handler

    def menu_items():
        sessions = scan_sessions(sessions_dir)
        items = [
            pystray.MenuItem("Open", open_flyout, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Dark theme", toggle_dark,
                             checked=lambda _i: state["theme"] == "dark"),
            pystray.MenuItem("Pin flyout", toggle_pin,
                             checked=lambda _i: state["pinned"]),
        ]
        if sessions:
            items.append(pystray.Menu.SEPARATOR)
            for s in sessions:
                label = classify(s)[0].strip()
                snoozed = is_snoozed(cfg, s.session_id)
                text = f"{s.project}: {label}"
                if snoozed:
                    text += "  (zzz)"
                sub = [
                    pystray.MenuItem(name, do_snooze(s.session_id, secs))
                    for name, secs in SNOOZE_DURATIONS
                ]
                if snoozed:
                    sub.append(pystray.Menu.SEPARATOR)
                    sub.append(pystray.MenuItem("Unsnooze", do_unsnooze(s.session_id)))
                items.append(pystray.MenuItem(text, pystray.Menu(*sub)))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Quit", on_quit))
        return items

    icon = pystray.Icon(
        "claude-code-watcher",
        icons["idle"],
        "Claude Code · starting",
        menu=pystray.Menu(lambda: menu_items()),
    )

    def poll_loop():
        prev_severity: dict[str, str] = {}
        while not stop_event.is_set():
            try:
                sessions = scan_sessions(sessions_dir)
                sev = overall_severity(sessions)

                icon.icon = icons[sev]
                icon.title = build_tooltip(sessions)

                sess_by_key = {
                    s.session_id or f"pid{s.pid}": s for s in sessions
                }
                curr_severity = {k: classify(s)[2] for k, s in sess_by_key.items()}
                escalated = diff_alerts(prev_severity, curr_severity)
                for key in escalated:
                    s = sess_by_key.get(key)
                    if s is None:
                        continue
                    if is_snoozed(cfg, s.session_id):
                        _log("escalation_suppressed_snoozed", session=s.session_id[:8])
                        continue
                    label = "WAIT" if s.status == "waiting" else "STUCK"
                    _log("escalation", session=s.session_id[:8],
                         project=s.project, state=label,
                         age=round(s.transcript_age, 1) if s.transcript_age else None)
                    _toast(f"[{s.project}] {label}",
                           "Permission needed" if label == "WAIT" else "Session silent ≥3 min")
                    if not args.no_sound:
                        beep()
                prev_severity = curr_severity
            except Exception as e:
                # Never let a transient scan error kill the tray.
                _log("poll_loop_error", level="ERROR", exc=str(e),
                     exc_type=type(e).__name__)
            stop_event.wait(args.interval)

    def setup(icon):
        icon.visible = True
        if sys.platform == "darwin":
            # Hide Python from the Dock so the menu-bar icon is the only UI.
            # pystray brings in pyobjc transitively on macOS, so AppKit is
            # available without an extra dep. No-op if anything's missing.
            try:
                from AppKit import NSApp, NSApplicationActivationPolicyAccessory
                app = NSApp()
                if app is not None:
                    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception:
                pass
        threading.Thread(target=poll_loop, daemon=True).start()

    _log("tray_start", theme=state["theme"], pinned=state["pinned"],
         interval=args.interval, platform=sys.platform)
    print("Claude Code Watcher · tray icon active. Double-click for info, right-click → Quit.")
    icon.run(setup=setup)


def _default_font_family() -> str:
    """Platform's preferred sans-serif. Calibri isn't installed on Mac;
    Helvetica is ubiquitous there and looks at home in Aqua. Linux gets
    DejaVu Sans (almost universal)."""
    if sys.platform == "darwin":
        return "Helvetica"
    if sys.platform == "win32":
        return "Calibri"
    return "DejaVu Sans"


_FONT = _default_font_family()


_FLYOUT_THEMES = {
    "light": {
        "bg":     "#f5f5f7",
        "fg":     "#000000",
        "accent": "#0a7a7a",     # dark teal
        "muted":  "#777777",
        "title_font":  (_FONT, 11, "bold"),
        "name_font":   (_FONT, 11),
        "detail_font": (_FONT, 11),
        "status_font": (_FONT, 10, "bold"),
        "uppercase_title": False,
        "border": True,
    },
    "dark": {
        # Dark neon: deep blue-black, cyan text, gold accents.
        "bg":     "#0a0a14",
        "fg":     "#00e5ff",
        "accent": "#ffd700",     # gold
        "muted":  "#4d8499",
        "title_font":  (_FONT, 11, "bold"),
        "name_font":   (_FONT, 11, "bold"),
        "detail_font": (_FONT, 11),
        "status_font": (_FONT, 10, "bold"),
        "uppercase_title": True,
        "border": True,
    },
}

_SEV_HEX = {
    "normal": "#4caf50",
    "warn":   "#ffc107",
    "alert":  "#f44336",
    "idle":   "#9e9e9e",
}


def run_flyout(args, sessions_dir: Path) -> None:
    """Borderless 'futuristic' info window listing all sessions.
    Theme via --theme {light,dark}; closes on focus-loss or Esc.
    Spawned as a child process by run_tray() so its tkinter event loop
    does not contend with pystray's Win32 message pump."""
    import tkinter as tk

    theme = _FLYOUT_THEMES.get(args.theme, _FLYOUT_THEMES["light"])
    BG = theme["bg"]
    FG = theme["fg"]
    ACCENT = theme["accent"]
    MUTED = theme["muted"]
    SEV_HEX = _SEV_HEX

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BG)

    width = 400
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()

    container = tk.Frame(root, bg=BG, padx=10, pady=8,
                         highlightbackground=ACCENT,
                         highlightthickness=1 if theme.get("border") else 0)
    container.pack(fill="both", expand=True)

    header_row = tk.Frame(container, bg=BG)
    header_row.pack(fill="x")
    title_text = "CLAUDE.CODE" if theme["uppercase_title"] else "CLAUDE CODE"
    tk.Label(header_row, text=title_text, fg=FG, bg=BG,
             font=theme["title_font"]).pack(side="left")
    timestamp = tk.Label(header_row, text="", fg=MUTED, bg=BG,
                         font=theme["detail_font"])
    timestamp.pack(side="right")

    tk.Frame(container, bg=ACCENT, height=1).pack(fill="x", pady=(5, 4))

    rows = tk.Frame(container, bg=BG)
    rows.pack(fill="both", expand=True)

    def render():
        for w in rows.winfo_children():
            w.destroy()
        timestamp.config(text=time.strftime("%H:%M:%S"))
        sessions = scan_sessions(sessions_dir)
        cfg = _load_config()

        if not sessions:
            tk.Label(rows, text="No active sessions", fg=MUTED, bg=BG,
                     font=(_FONT, 10, "italic")).pack(anchor="w", pady=4)
            return

        def sort_key(s: SessionInfo) -> float:
            return s.transcript_age if s.transcript_age is not None else float("inf")

        for s in sorted(sessions, key=sort_key):
            sev = classify(s)[2]
            label = classify(s)[0].strip()
            status_word = label.split()[-1] if " " in label else label
            dot_color = SEV_HEX.get(sev, MUTED)
            snoozed = is_snoozed(cfg, s.session_id)

            # Top row: dot, project name (+ zzz if snoozed), status word.
            row = tk.Frame(rows, bg=BG)
            row.pack(fill="x", pady=(2, 0))
            tk.Label(row, text="●", fg=dot_color, bg=BG,
                     font=(_FONT, 14)).pack(side="left", padx=(0, 6))
            name = s.project[:24]
            if theme["uppercase_title"]:
                name = name.upper()
            tk.Label(row, text=name, fg=FG, bg=BG,
                     font=theme["name_font"]).pack(side="left")
            if snoozed:
                tk.Label(row, text="zzz", fg=MUTED, bg=BG,
                         font=(_FONT, 9, "italic")).pack(side="left", padx=(6, 0))
            tk.Label(row, text=status_word, fg=dot_color, bg=BG,
                     font=theme["status_font"]).pack(side="right")

            # Detail row: cpu%, age, tool-in-flight. Indented to align with
            # the project name above. All muted so the top row reads first.
            cpu = session_cpu_percent(s.pid)
            age = s.transcript_age
            tr_path = find_transcript(s.session_id, s.cwd) if s.session_id else None
            tool = _tool_in_flight(tr_path) if tr_path else None

            detail_parts = []
            if cpu is not None:
                detail_parts.append(f"cpu {cpu:>4.0f}%")
            elif _HAS_PSUTIL:
                detail_parts.append("cpu  —")
            if age is not None:
                detail_parts.append(f"age {fmt_elapsed(age)}")
            if tool:
                # When the session is waiting on permission, the unresolved
                # tool_use is what's being asked about, not what's running.
                verb = "needs" if s.status == "waiting" else "running"
                detail_parts.append(f"{verb}: {tool}")
            if detail_parts:
                detail = tk.Frame(rows, bg=BG)
                detail.pack(fill="x", padx=(22, 0), pady=(0, 1))
                tk.Label(detail, text="   ".join(detail_parts),
                         fg=MUTED, bg=BG,
                         font=theme["detail_font"]).pack(side="left")

    def fit_and_position():
        root.update_idletasks()
        h = container.winfo_reqheight() + 4
        x = sw - width - 20
        y = sh - h - 70
        root.geometry(f"{width}x{h}+{x}+{y}")

    def tick():
        try:
            render()
            fit_and_position()
        except Exception:
            pass
        root.after(700, tick)

    def close(_event=None):
        root.destroy()

    render()
    fit_and_position()
    if not args.pin:
        root.bind("<FocusOut>", close)
    root.bind("<Escape>", close)
    root.after(700, tick)
    root.focus_force()
    root.mainloop()


def run_tui(args, sessions_dir: Path) -> None:
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=0.3,
                    help="poll interval in seconds (default: 0.3)")
    ap.add_argument("--sessions-dir", default=None,
                    help="override sessions dir (default: $CLAUDE_SESSIONS_DIR or ~/.claude/sessions)")
    ap.add_argument("--no-sound", action="store_true",
                    help="do not beep on STUCK / WAIT transitions")
    ap.add_argument("--tray", action="store_true",
                    help="run headless with a system tray icon instead of the TUI table")
    ap.add_argument("--install-autostart", action="store_true",
                    help="register the tray watcher to launch on user login, then exit")
    ap.add_argument("--uninstall-autostart", action="store_true",
                    help="remove the autostart registration, then exit")
    ap.add_argument("--register-claude-hook", action="store_true",
                    help="register a SessionStart hook in ~/.claude/settings.json so the "
                         "tray watcher starts when Claude opens (alternative to OS-login "
                         "autostart). Idempotent. Then exit.")
    ap.add_argument("--unregister-claude-hook", action="store_true",
                    help="remove the SessionStart hook registered above, then exit")
    ap.add_argument("--flyout", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pin", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--theme", choices=["light", "dark"], default="light",
                    help="info-window theme: 'light' (minimalist hi-tech, black on near-white) "
                         "or 'dark' (neon: cyan on deep blue, magenta accents). The tray's "
                         "right-click menu has runtime toggles that overwrite this.")
    args = ap.parse_args()

    if args.install_autostart:
        sys.exit(_autostart_install())
    if args.uninstall_autostart:
        sys.exit(_autostart_uninstall())
    if args.register_claude_hook:
        sys.exit(_claude_hook_install())
    if args.unregister_claude_hook:
        sys.exit(_claude_hook_uninstall())

    sessions_dir_str = args.sessions_dir or os.environ.get("CLAUDE_SESSIONS_DIR")
    sessions_dir = Path(sessions_dir_str) if sessions_dir_str else (
        Path.home() / ".claude" / "sessions"
    )

    if args.flyout:
        run_flyout(args, sessions_dir)
    elif args.tray:
        run_tray(args, sessions_dir)
    else:
        run_tui(args, sessions_dir)


if __name__ == "__main__":
    main()
