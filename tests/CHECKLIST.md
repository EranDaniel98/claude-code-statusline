# Cross-window manual checks

The scripted tests can't observe two real Claude Code windows simultaneously.
Run these by hand after `test_statusline.py` / `test_notify.py` pass.

## ● during a turn
- [ ] Open a Claude Code window. Send a prompt.
- [ ] Green `●` appears within ~1s of submit.
- [ ] `●` stays visible during silent extended thinking (no streaming yet).
- [ ] `●` disappears within ~1s after Claude finishes responding.

## WAIT — cross-window
- [ ] Open windows A and B simultaneously.
- [ ] In A, run a command that triggers a permission prompt.
- [ ] A's statusline shows red `WAIT`.
- [ ] B's statusline is unaffected (still shows its own state).
- [ ] Approve / deny in A → `WAIT` clears in A.

## Long quiet thinking → stale signal
- [ ] Send a prompt likely to trigger >2min thinking.
- [ ] During: `●` visible (status=busy), elapsed climbs.
- [ ] Elapsed turns yellow at 30s, red at 2min — useful "is it stuck?" signal.

## Width adaptation
- [ ] Resize terminal to ~60 cols. Statusline fits one line, project truncated with `…`.
- [ ] Resize to 120+ cols. Full project / session_name visible, no `…`.

## Notifications
- [ ] Trigger a permission prompt → warning beep + toast `[<project>] Permission needed`.
- [ ] Let a turn end while you're in another window → chime + `[<project>] Awaiting your input`.
- [ ] Set `CLAUDE_QUIET=1` env var, repeat → silent (no sound, no toast).

## Fail-safe
- [ ] Rename `~/.claude/sessions/` away (or empty it) and submit a prompt.
- [ ] Statusline still renders (mtime-fresh fallback gives `●`).
- [ ] Rename it back when done.
