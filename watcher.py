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


def build_tooltip(sessions: list[SessionInfo],
                  focused_session_id: str | None = None) -> str:
    """Multi-line tray tooltip. Capped at 120 chars (Windows NotifyIcon limit).

    When `focused_session_id` is given, that session is sorted to the top
    with a ▶ marker so the user can spot it without reading the list.
    """
    if not sessions:
        return "Claude Code · no sessions"
    if focused_session_id:
        focused = next((s for s in sessions if s.session_id == focused_session_id), None)
        others = [s for s in sessions if s.session_id != focused_session_id]
        ordered = ([focused] if focused else []) + others
    else:
        ordered = list(sessions)
    lines = ["Claude Code"]
    for s in ordered:
        label = classify(s)[0].strip()
        proj = s.project[:18]
        marker = "▶ " if focused_session_id and s.session_id == focused_session_id else "  "
        lines.append(f"{marker}{proj}: {label}")
    return "\n".join(lines)[:120]


def _make_dot_icon(severity: str):
    """Simple colored circle for the tray. Color reflects severity."""
    from PIL import Image, ImageDraw
    color = _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["idle"])
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, size - 6, size - 6), fill=color)
    return img


_TERMINAL_PROCESS_NAMES = {
    "WindowsTerminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "conhost.exe", "wt.exe", "claude.exe", "node.exe",
}


def _foreground_is_claude_terminal() -> bool:
    """Heuristic: is the OS foreground window a terminal/Claude process?
    Win32-only. Returns False on non-Windows or on any error.

    The 'which tab' question can't be answered without UI Automation, so
    we settle for 'any terminal is foreground' and let `_focused_session`
    pick the most-recently-active Claude as the focus proxy.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return False
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                exe = buf.value.split("\\")[-1]
                return exe in _TERMINAL_PROCESS_NAMES
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:
        return False
    return False


def _focused_session(sessions: list[SessionInfo]) -> SessionInfo | None:
    """Proxy for 'which session is currently focused' — returns the
    most-recently-active session, but only when a terminal is foreground
    on Windows. Returns None otherwise (no focus marker shown)."""
    if not _foreground_is_claude_terminal():
        return None
    candidates = [s for s in sessions if s.status in ("busy", "waiting")] or sessions
    if not candidates:
        return None

    def key(s: SessionInfo) -> float:
        return s.transcript_age if s.transcript_age is not None else float("inf")

    return min(candidates, key=key)


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


def _watcher_launch_vbs_path() -> Path:
    """Path to the VBS launcher dropped next to watcher.py."""
    return Path(__file__).resolve().parent / "_claude_code_watcher_launch.vbs"


def _write_launch_vbs() -> Path:
    """Drop a VBScript wrapper that launches the watcher with a hidden
    window. Required because uv-built venvs ship `pythonw.exe` as a
    trampoline shim that still flashes a console; wscript + WshShell.Run
    style=0 forces a truly hidden window regardless of the target."""
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    if pyw.exists():
        py = pyw
    script_path = Path(__file__).resolve()
    vbs_path = _watcher_launch_vbs_path()
    # Inner Run arg is the command line for WshShell — quote each path.
    inner = f'""{py}"" ""{script_path}"" --tray'
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
        vbs = _watcher_launch_vbs_path()
        return f'wscript.exe "{vbs}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --tray'


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
    <string>{Path(__file__).resolve()}</string>
    <string>--tray</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    print(f"Installed autostart: {plist_path}")
    print("Activate with: launchctl load ~/Library/LaunchAgents/com.anthropic.claude-code-watcher.plist")
    return 0


def _autostart_uninstall_macos() -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.anthropic.claude-code-watcher.plist"
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed: {plist_path}")
    else:
        print("No autostart plist to remove.")
    return 0


def _autostart_install_linux() -> int:
    desktop_dir = Path.home() / ".config" / "autostart"
    desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop_path = desktop_dir / "claude-code-watcher.desktop"
    body = f"""[Desktop Entry]
Type=Application
Name=Claude Code Watcher
Exec={sys.executable} {Path(__file__).resolve()} --tray
X-GNOME-Autostart-enabled=true
"""
    desktop_path.write_text(body, encoding="utf-8")
    print(f"Installed autostart: {desktop_path}")
    return 0


def _autostart_uninstall_linux() -> int:
    desktop_path = Path.home() / ".config" / "autostart" / "claude-code-watcher.desktop"
    if desktop_path.exists():
        desktop_path.unlink()
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
    import threading

    icons = {sev: _make_dot_icon(sev) for sev in ("idle", "normal", "warn", "alert")}

    stop_event = threading.Event()

    def on_quit(icon, _item):
        stop_event.set()
        icon.stop()

    def menu_items():
        sessions = scan_sessions(sessions_dir)
        items = []
        if sessions:
            for s in sessions:
                label = classify(s)[0].strip()
                items.append(pystray.MenuItem(
                    f"{s.project}: {label}", None, enabled=False
                ))
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
                focused = _focused_session(sessions)
                focused_sid = focused.session_id if focused else None

                icon.icon = icons[sev]
                icon.title = build_tooltip(sessions, focused_session_id=focused_sid)

                sess_by_key = {
                    s.session_id or f"pid{s.pid}": s for s in sessions
                }
                curr_severity = {k: classify(s)[2] for k, s in sess_by_key.items()}
                escalated = diff_alerts(prev_severity, curr_severity)
                for key in escalated:
                    s = sess_by_key.get(key)
                    if s is None:
                        continue
                    state = "WAIT" if s.status == "waiting" else "STUCK"
                    _toast(f"[{s.project}] {state}",
                           "Permission needed" if state == "WAIT" else "Session silent ≥3 min")
                    if not args.no_sound:
                        beep()
                prev_severity = curr_severity
            except Exception:
                # Never let a transient scan error kill the tray.
                pass
            stop_event.wait(args.interval)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=poll_loop, daemon=True).start()

    print("Claude Code Watcher · tray icon active. Right-click → Quit to exit.")
    icon.run(setup=setup)


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
    args = ap.parse_args()

    if args.install_autostart:
        sys.exit(_autostart_install())
    if args.uninstall_autostart:
        sys.exit(_autostart_uninstall())

    sessions_dir_str = args.sessions_dir or os.environ.get("CLAUDE_SESSIONS_DIR")
    sessions_dir = Path(sessions_dir_str) if sessions_dir_str else (
        Path.home() / ".claude" / "sessions"
    )

    if args.tray:
        run_tray(args, sessions_dir)
    else:
        run_tui(args, sessions_dir)


if __name__ == "__main__":
    main()
