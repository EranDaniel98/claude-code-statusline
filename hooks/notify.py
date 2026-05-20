"""Notification hook: distinct sound + Windows toast per notification type.

Fires when Claude Code emits a notification (permission prompt, idle prompt, etc.).
Goal: tell the user *which* of N parallel Claude Code windows needs them, without
making them tab through every window.

Set CLAUDE_QUIET=1 to silence.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

LOG = Path.home() / ".claude" / "notification.log"


def classify(message: str, notif_type: str) -> str:
    if notif_type:
        return notif_type
    m = (message or "").lower()
    if "permission" in m or "allow" in m or "approve" in m:
        return "permission_prompt"
    if "waiting" in m or "idle" in m or "input" in m:
        return "idle_prompt"
    return "other"


def play_sound(kind: str) -> None:
    try:
        import winsound
    except ImportError:
        return
    sounds = {
        "permission_prompt": 0x30,
        "idle_prompt": 0x40,
        "elicitation_dialog": 0x20,
        "other": 0x0,
    }
    try:
        winsound.MessageBeep(sounds.get(kind, 0x0))
    except Exception:
        pass


APP_ID = "Anthropic.ClaudeCode"


def show_toast(title: str, body: str) -> None:
    ps_script = r"""
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
    env = os.environ.copy()
    env["TOAST_TITLE"] = title
    env["TOAST_BODY"] = body or ""
    env["TOAST_APP_ID"] = APP_ID
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def main() -> None:
    if os.environ.get("CLAUDE_QUIET"):
        return

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return

    message = data.get("message") or ""
    notif_type = data.get("notification_type") or data.get("type") or ""
    cwd = data.get("cwd") or ""
    project = os.path.basename(cwd.rstrip("\\/")) or "Claude"

    kind = classify(message, notif_type)

    play_sound(kind)

    titles = {
        "permission_prompt": f"[{project}] Permission needed",
        "idle_prompt": f"[{project}] Awaiting your input",
        "elicitation_dialog": f"[{project}] Question",
        "other": f"[{project}] Claude",
    }
    show_toast(titles.get(kind, titles["other"]), message[:200])

    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{kind}\t{project}\t{message[:120]}\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
