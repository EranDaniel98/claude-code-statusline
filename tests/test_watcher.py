"""Watcher core-logic tests.

Imports watcher.py as a module and exercises its scanner + classifier
against synthetic session files in a tmpdir. Does not run the main loop.
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import watcher  # noqa: E402


def make_session(d: Path, status: str, session_id: str | None = None) -> str:
    sid = session_id or f"sess-{status}-{int(time.time()*1000)%100000}"
    (d / f"{sid}.json").write_text(json.dumps({
        "pid": 1000 + len(list(d.iterdir())),
        "sessionId": sid,
        "cwd": "C:\\test",
        "status": status,
    }))
    return sid


def main() -> None:
    results: list[bool] = []
    tmp = Path(tempfile.mkdtemp(prefix="ccs-watcher-"))
    try:
        make_session(tmp, "busy")
        make_session(tmp, "idle")
        make_session(tmp, "waiting")

        sessions = watcher.scan_sessions(tmp)
        ok = len(sessions) == 3
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] scan_sessions finds all 3 ({len(sessions)})")

        by_status = {s.status: s for s in sessions}
        for st, expected_severity in [("waiting", "alert"), ("busy", "normal"), ("idle", "normal")]:
            label, color, sev = watcher.classify(by_status[st])
            ok = sev == expected_severity
            results.append(ok)
            print(f"[{'PASS' if ok else 'FAIL'}] classify({st}) → severity={sev} (expected {expected_severity})")

        s = by_status["busy"]
        s.transcript_age = 90.0
        _, _, sev = watcher.classify(s)
        ok = sev == "warn"
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] busy + 90s quiet → severity=warn (got {sev})")

        s.transcript_age = 300.0
        _, _, sev = watcher.classify(s)
        ok = sev == "alert"
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] busy + 300s quiet → severity=alert (got {sev})")

        prev = {"a": "normal", "b": "alert"}
        curr = {"a": "alert", "b": "alert", "c": "alert"}
        escalated = watcher.diff_alerts(prev, curr)
        ok = set(escalated) == {"a", "c"}
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] diff_alerts new escalations: {sorted(escalated)} (expected ['a','c'])")

        # --- overall_severity ---
        def mk(status, age=None):
            return watcher.SessionInfo(pid=0, session_id="x", cwd="", status=status,
                                       project="p", transcript_age=age)
        cases = [
            ([], "idle"),
            ([mk("idle")], "idle"),
            ([mk("busy", 1.0)], "normal"),
            ([mk("busy", 1.0), mk("busy", 90.0)], "warn"),
            ([mk("busy", 90.0), mk("waiting")], "alert"),
            ([mk("busy", 1.0), mk("busy", 300.0)], "alert"),
        ]
        for sessions, expected in cases:
            got = watcher.overall_severity(sessions)
            ok = got == expected
            results.append(ok)
            print(f"[{'PASS' if ok else 'FAIL'}] overall_severity({[s.status+('@'+str(s.transcript_age) if s.transcript_age else '') for s in sessions]}) = {got} (expected {expected})")

        # --- build_tooltip ---
        tip = watcher.build_tooltip([])
        ok = tip == "Claude Code · no sessions"
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] build_tooltip(no sessions) = {tip!r}")

        tip = watcher.build_tooltip([mk("busy", 1.0), mk("waiting")])
        ok = tip.startswith("Claude Code") and "BUSY" in tip and "WAIT" in tip
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] build_tooltip includes BUSY+WAIT: {tip!r}")

        ok = len(tip) <= 120
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] build_tooltip length ≤120 (got {len(tip)})")

        # --- _label_for_single ---
        label_cases = [
            (mk("busy", 1.0), "BUSY"),
            (mk("busy", 90.0), "THINK"),
            (mk("busy", 300.0), "STUCK"),
            (mk("waiting"), "WAIT"),
            (mk("idle"), ""),
        ]
        for s, expected in label_cases:
            got = watcher._label_for_single(s)
            ok = got == expected
            results.append(ok)
            print(f"[{'PASS' if ok else 'FAIL'}] _label_for_single({s.status}@{s.transcript_age}) = {got!r} (expected {expected!r})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(results)
    total = len(results)
    print()
    print(f"{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
