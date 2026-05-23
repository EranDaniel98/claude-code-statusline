"""Notification hook: distinct sound + native toast per event type.

Fires when Claude Code emits a notification (permission prompt, idle prompt, etc.).
Goal: tell the user *which* of N parallel Claude Code windows needs them, without
making them tab through every window.

Selects a platform-appropriate backend automatically:
  - Windows: winsound.MessageBeep + PowerShell WinRT toast (needs registered AppId)
  - macOS:   afplay system sound + osascript display notification
  - Linux:   paplay/aplay system sound + notify-send

Env vars:
  CLAUDE_QUIET=1   silence everything (no sound, no toast)
"""
import json
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

LOG = Path.home() / ".claude" / "notification.log"
APP_ID = "Anthropic.ClaudeCode"


def classify(message: str, notif_type: str) -> str:
    if notif_type:
        return notif_type
    m = (message or "").lower()
    if "permission" in m or "allow" in m or "approve" in m:
        return "permission_prompt"
    if "waiting" in m or "idle" in m or "input" in m:
        return "idle_prompt"
    return "other"


class NotificationBackend(ABC):
    @abstractmethod
    def play_sound(self, kind: str) -> None: ...

    @abstractmethod
    def show_toast(self, title: str, body: str) -> None: ...


# ── Windows ──────────────────────────────────────────────────────────────────

class WindowsBackend(NotificationBackend):
    _SOUND_MAP = {
        "permission_prompt": 0x30,
        "idle_prompt": 0x40,
        "elicitation_dialog": 0x20,
        "other": 0x0,
    }

    _PS_SCRIPT = r"""
$ErrorActionPreference='SilentlyContinue'
try {
  $null = [Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
  $null = [Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]
  $title = [System.Security.SecurityElement]::Escape($env:TOAST_TITLE)
  $body  = [System.Security.SecurityElement]::Escape($env:TOAST_BODY)
  $xml = "<toast><visual><binding template=`"ToastText02`"><text id=`"1`">$title</text><text id=`"2`">$body</text></binding></visual></toast>"
  $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
  $doc.LoadXml($xml)
  $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:TOAST_APP_ID).Show($toast)
} catch {}
"""

    def play_sound(self, kind: str) -> None:
        try:
            import winsound
        except ImportError:
            return
        try:
            winsound.MessageBeep(self._SOUND_MAP.get(kind, 0x0))
        except Exception:
            pass

    def show_toast(self, title: str, body: str) -> None:
        env = os.environ.copy()
        env["TOAST_TITLE"] = title
        env["TOAST_BODY"] = body or ""
        env["TOAST_APP_ID"] = APP_ID
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", self._PS_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass


# ── macOS ────────────────────────────────────────────────────────────────────

class MacOSBackend(NotificationBackend):
    _SOUND_MAP = {
        "permission_prompt": "Sosumi",
        "idle_prompt": "Glass",
        "elicitation_dialog": "Tink",
        "other": "Pop",
    }

    def play_sound(self, kind: str) -> None:
        name = self._SOUND_MAP.get(kind, "Pop")
        path = f"/System/Library/Sounds/{name}.aiff"
        if not os.path.exists(path):
            return
        try:
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def show_toast(self, title: str, body: str) -> None:
        safe_title = (title or "").replace('"', '\\"')
        safe_body = (body or "").replace('"', '\\"')
        script = f'display notification "{safe_body}" with title "{safe_title}"'
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ── Linux ────────────────────────────────────────────────────────────────────

class LinuxBackend(NotificationBackend):
    _URGENCY_MAP = {
        "permission_prompt": "critical",
        "idle_prompt": "normal",
        "elicitation_dialog": "normal",
        "other": "low",
    }

    def play_sound(self, kind: str) -> None:
        candidates = [
            ["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"],
            ["aplay", "-q", "/usr/share/sounds/alsa/Front_Center.wav"],
        ]
        for cmd in candidates:
            if shutil.which(cmd[0]) and os.path.exists(cmd[-1]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                return
        sys.stdout.write("\a")
        sys.stdout.flush()

    def show_toast(self, title: str, body: str) -> None:
        if not shutil.which("notify-send"):
            return
        urgency = self._URGENCY_MAP.get("other", "normal")
        try:
            subprocess.Popen(
                ["notify-send", f"--urgency={urgency}", "--app-name=Claude Code",
                 title or "Claude Code", body or ""],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def get_backend() -> NotificationBackend:
    if sys.platform == "win32":
        return WindowsBackend()
    if sys.platform == "darwin":
        return MacOSBackend()
    return LinuxBackend()


def main() -> None:
    raw_stdin = sys.stdin.read()
    try:
        data = json.loads(raw_stdin or "{}")
    except json.JSONDecodeError:
        data = {}

    message = data.get("message") or ""
    notif_type = data.get("notification_type") or data.get("type") or ""
    cwd = data.get("cwd") or ""
    project = os.path.basename(cwd.rstrip("\\/")) or "Claude"

    kind = classify(message, notif_type)

    # Log the full inbound payload to the structured log so spurious
    # toasts (e.g. "Awaiting your input" when the user wasn't actually
    # idle) can be diagnosed — every payload Claude Code sends ends up
    # here with its `kind` classification, raw message, and all fields.
    # Logged BEFORE the CLAUDE_QUIET return so we capture invocations
    # even when toast output is suppressed.
    quiet = bool(os.environ.get("CLAUDE_QUIET"))
    try:
        from .watcher import _log
        _log("notify_fired", kind=kind, project=project,
             message=message[:200], notif_type=notif_type,
             quiet=quiet,
             keys=sorted(data.keys()) if isinstance(data, dict) else None)
    except Exception:
        pass

    if quiet:
        return

    titles = {
        "permission_prompt": f"[{project}] Permission needed",
        "idle_prompt": f"[{project}] Awaiting your input",
        "elicitation_dialog": f"[{project}] Question",
        "other": f"[{project}] Claude",
    }

    backend = get_backend()
    backend.play_sound(kind)
    backend.show_toast(titles.get(kind, titles["other"]), message[:200])

    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{kind}\t{project}\t{message[:120]}\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
