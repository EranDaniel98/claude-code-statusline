"""Fire each notification_type into notify.py.

Manual verification: listen for three distinct sounds + watch for three toasts.
The classifier maps notification_type → sound/title, so we exercise each branch.

Run: python ~/.claude/tests/test_notify.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

NOTIFY = Path.home() / ".claude" / "hooks" / "notify.py"

CASES = [
    ("permission_prompt", "Permission needed for Bash command"),
    ("idle_prompt", "Awaiting your input"),
    ("elicitation_dialog", "Quick question for you"),
]


def fire(notif_type: str, message: str) -> None:
    payload = {
        "notification_type": notif_type,
        "message": message,
        "cwd": "C:\\test-project",
    }
    subprocess.run(
        [sys.executable, str(NOTIFY)],
        input=json.dumps(payload),
        text=True,
        check=False,
    )


def main() -> None:
    print("Firing 3 notifications. Listen for distinct sounds + watch toasts.")
    print("Expected:")
    print("  1. permission_prompt → warning beep, toast '[test-project] Permission needed'")
    print("  2. idle_prompt       → chime,        toast '[test-project] Awaiting your input'")
    print("  3. elicitation_dialog→ asterisk,     toast '[test-project] Question'")
    print()
    for nt, msg in CASES:
        print(f"  → {nt}")
        fire(nt, msg)
        time.sleep(2)
    print()
    print("Did you hear 3 distinct sounds and see 3 toasts? (manual confirm)")
    print("If CLAUDE_QUIET=1 is set, both should be silent.")


if __name__ == "__main__":
    main()
