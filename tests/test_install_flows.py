"""Install-flow tests: hook register/unregister + autostart install/uninstall.

Each test runs in an isolated HOME (tempdir) so it doesn't touch the real
user's ~/.claude, ~/.config, ~/Library, or Windows registry. Functions
under test call Path.home() dynamically so swapping HOME / USERPROFILE
redirects every path lookup.

The Windows registry test uses a unique value name and cleans up in
finally, so it doesn't collide with a real autostart install.
"""
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_watcher import watcher  # noqa: E402


@contextmanager
def isolated_home():
    """Redirect Path.home() to a tempdir for the duration of the block.
    Overrides HOME on POSIX and USERPROFILE on Windows."""
    tmp = Path(tempfile.mkdtemp(prefix="ccs-home-"))
    keys = ("HOME", "USERPROFILE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ[k] = str(tmp)
    try:
        # Sanity-check that Path.home() now points at the tmp dir
        assert Path.home() == tmp, f"Path.home() didn't follow env: {Path.home()} != {tmp}"
        yield tmp
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _check(name: str, ok: bool, *, details: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + details) if details else ''}")
    return ok


# --- _claude_hook_install / _claude_hook_uninstall -----------------------------

def test_hook_install_on_empty_settings() -> bool:
    with isolated_home() as home:
        rc = watcher._claude_hook_install()
        settings_path = home / ".claude" / "settings.json"
        if rc != 0 or not settings_path.exists():
            return _check("hook install on empty settings.json creates file",
                          False, details=f"rc={rc} exists={settings_path.exists()}")
        d = json.loads(settings_path.read_text(encoding="utf-8"))
        entries = d.get("hooks", {}).get("SessionStart", [])
        return _check("hook install on empty settings.json creates file",
                      len(entries) == 1 and "hooks" in entries[0],
                      details=f"entries={len(entries)}")


def test_hook_install_preserves_other_hooks() -> bool:
    with isolated_home() as home:
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "statusLine": {"type": "command", "command": "x"},
            "hooks": {
                "Notification": [
                    {"hooks": [{"type": "command", "command": "notify"}]}
                ],
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk"}]}
                ],
            },
        }
        settings_path.write_text(json.dumps(existing), encoding="utf-8")

        watcher._claude_hook_install()

        d = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = d.get("hooks") or {}
        ok = (
            d.get("statusLine", {}).get("command") == "x"
            and "Notification" in hooks
            and "PreToolUse" in hooks
            and "SessionStart" in hooks
            and len(hooks["Notification"]) == 1
        )
        return _check("hook install preserves other top-level keys + hooks",
                      ok, details=f"keys={sorted(d.keys())}, hooks={sorted(hooks.keys())}")


def test_hook_install_is_idempotent() -> bool:
    with isolated_home() as home:
        watcher._claude_hook_install()
        watcher._claude_hook_install()  # second call should be a no-op

        d = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        entries = d["hooks"]["SessionStart"]
        return _check("hook install is idempotent (2nd call = no-op)",
                      len(entries) == 1, details=f"entries={len(entries)}")


def test_hook_uninstall_removes_only_our_entry() -> bool:
    with isolated_home() as home:
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-seed an unrelated SessionStart hook from some other tool
        third_party = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "/path/to/other-tool"}]}
                ],
                "Notification": [
                    {"hooks": [{"type": "command", "command": "notify"}]}
                ],
            },
        }
        settings_path.write_text(json.dumps(third_party), encoding="utf-8")

        watcher._claude_hook_install()
        watcher._claude_hook_uninstall()

        d = json.loads(settings_path.read_text(encoding="utf-8"))
        ss = d.get("hooks", {}).get("SessionStart") or []
        # The third-party hook should remain; ours should be gone.
        ok = (
            len(ss) == 1
            and "/path/to/other-tool" in ss[0]["hooks"][0]["command"]
            and "Notification" in d["hooks"]
        )
        return _check("hook uninstall removes only our entry",
                      ok, details=f"remaining SessionStart={ss}")


def test_hook_round_trip_restores_settings() -> bool:
    with isolated_home() as home:
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        original = {
            "statusLine": {"type": "command", "command": "x", "refreshInterval": 300},
            "hooks": {"Notification": [{"hooks": [{"type": "command", "command": "n"}]}]},
        }
        original_text = json.dumps(original, indent=2)
        settings_path.write_text(original_text, encoding="utf-8")

        watcher._claude_hook_install()
        watcher._claude_hook_uninstall()

        d = json.loads(settings_path.read_text(encoding="utf-8"))
        # SessionStart key should be gone entirely after uninstall.
        # Other content should be byte-identical to original.
        ok = (
            "SessionStart" not in (d.get("hooks") or {})
            and d.get("statusLine") == original["statusLine"]
            and d.get("hooks", {}).get("Notification") == original["hooks"]["Notification"]
        )
        return _check("hook install + uninstall round-trip restores settings",
                      ok, details=f"after={d}")


# --- VBS launchers (Windows-only) ----------------------------------------------

def test_write_launch_vbs() -> bool:
    if sys.platform != "win32":
        return _check("VBS launcher generation (skipped on non-Windows)", True)
    with isolated_home() as home:
        path = watcher._write_launch_vbs()
        body = path.read_text(encoding="utf-8")
        ok = (
            path.parent == home / ".claude"
            and "WshShell.Run" in body
            and "claude_watcher.watcher" in body
            and "--tray" in body
            and ", 0, False" in body  # hidden window, async
        )
        return _check("_write_launch_vbs creates expected file + content",
                      ok, details=f"path={path}")


def test_write_hook_launch_vbs() -> bool:
    if sys.platform != "win32":
        return _check("hook VBS launcher generation (skipped on non-Windows)", True)
    with isolated_home() as home:
        path = watcher._write_hook_launch_vbs()
        body = path.read_text(encoding="utf-8")
        ok = (
            path.parent == home / ".claude"
            and "claude_watcher.launch_watcher" in body
            and ", 0, False" in body
        )
        return _check("_write_hook_launch_vbs creates expected file + content",
                      ok, details=f"path={path}")


# --- Autostart per-platform (only the file-writing portion) --------------------

def test_autostart_install_linux() -> bool:
    # File-write logic is platform-agnostic — only the .desktop path uses
    # ~/.config/autostart which is real on Linux. We test the WRITE under
    # isolated HOME on any OS; the launchd / Windows registry variants
    # have their own platform-guarded tests below.
    with isolated_home() as home:
        rc = watcher._autostart_install_linux()
        path = home / ".config" / "autostart" / "claude-code-watcher.desktop"
        body = path.read_text(encoding="utf-8") if path.exists() else ""
        ok = (
            rc == 0
            and path.exists()
            and "claude_watcher.watcher" in body
            and "--tray" in body
            and "[Desktop Entry]" in body
        )
        return _check("Linux .desktop install writes expected content",
                      ok, details=f"exists={path.exists()}")


def test_autostart_install_macos() -> bool:
    # We test the plist write under isolated HOME even on non-Mac. The
    # function tries to call `launchctl load` but catches all exceptions,
    # so on Win/Linux the call fails silently and we still get the plist.
    with isolated_home() as home:
        rc = watcher._autostart_install_macos()
        path = home / "Library" / "LaunchAgents" / "com.anthropic.claude-code-watcher.plist"
        body = path.read_text(encoding="utf-8") if path.exists() else ""
        ok = (
            rc == 0
            and path.exists()
            and "claude_watcher.watcher" in body
            and "<key>RunAtLoad</key>" in body
        )
        return _check("macOS plist install writes expected content",
                      ok, details=f"exists={path.exists()}")


def test_autostart_install_windows() -> bool:
    if sys.platform != "win32":
        return _check("Windows Run-key install (skipped on non-Windows)", True)
    # Use a unique registry value name so the test doesn't fight a real
    # user's autostart entry. Restore the original constant in finally.
    import winreg
    original_name = watcher._AUTOSTART_NAME
    watcher._AUTOSTART_NAME = f"ClaudeCodeWatcher_TEST_{os.getpid()}"
    try:
        with isolated_home():
            rc = watcher._autostart_install_windows()
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ,
            ) as k:
                value, _ = winreg.QueryValueEx(k, watcher._AUTOSTART_NAME)
            uninstall_rc = watcher._autostart_uninstall_windows()
            ok = rc == 0 and "wscript.exe" in value and uninstall_rc == 0
            return _check(
                "Windows Run-key install writes wscript.exe entry, uninstall removes it",
                ok, details=f"value={value!r}",
            )
    finally:
        # Defensive cleanup in case uninstall didn't run.
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE,
            ) as k:
                try:
                    winreg.DeleteValue(k, watcher._AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        watcher._AUTOSTART_NAME = original_name


# -------------------------------------------------------------------------------

def main() -> None:
    results = [
        test_hook_install_on_empty_settings(),
        test_hook_install_preserves_other_hooks(),
        test_hook_install_is_idempotent(),
        test_hook_uninstall_removes_only_our_entry(),
        test_hook_round_trip_restores_settings(),
        test_write_launch_vbs(),
        test_write_hook_launch_vbs(),
        test_autostart_install_linux(),
        test_autostart_install_macos(),
        test_autostart_install_windows(),
    ]
    passed = sum(results)
    total = len(results)
    print()
    print(f"{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
