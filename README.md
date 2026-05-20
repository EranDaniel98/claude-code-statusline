# claude-code-statusline

Statusline + notification hook for [Claude Code](https://claude.com/claude-code) on Windows.

Tells you at a glance which of your parallel Claude Code windows is busy, idle, or waiting for permission — and surfaces toast notifications when a window needs your attention.

## What you get

**Statusline** (renders at the bottom of the Claude Code TUI):

```
project-name | ● | Opus 4.7 1M | 13% (131k) | 5h:19% 7d:48% | 41s
```

- **project** — `session_name` if set, else `cwd` basename. Truncates with `…` to fit terminal width.
- **●** — green dot when the session is actively working; `WAIT` (red) when a permission prompt is pending; hidden when idle.
- **model** — Claude model display name, `1M` suffix if 1M context.
- **context** — used percentage + token count. Colors: dim → yellow ≥60% → bold yellow ≥80% → red ≥90%.
- **rate limits** — 5-hour and 7-day usage. Colors graduate from dim → yellow → red.
- **elapsed** — seconds since the transcript JSONL was last touched. Dim <30s → yellow <2min → red ≥2min. This is your "is it stuck?" signal.

**Notification hook** (fires on permission prompts and idle prompts):

- Distinct system sounds per event type (`MessageBeep` mapping).
- Windows toast via WinRT with a registered `Anthropic.ClaudeCode` AppId.
- Project name in the toast title so you know *which* window needs you.

## Requirements

- Windows 10/11
- Python 3.10+
- Claude Code installed

## Install

1. Copy files into `~/.claude/`:
   ```powershell
   Copy-Item statusline.py "$env:USERPROFILE\.claude\statusline.py"
   Copy-Item hooks\notify.py "$env:USERPROFILE\.claude\hooks\notify.py"
   ```

2. Register the toast AppId (one-time, required for toasts to display):
   ```powershell
   .\scripts\register-app-id.ps1
   ```

3. Merge `settings.example.json` into `~/.claude/settings.json`:
   - Add the `statusLine` block.
   - Add the `Notification` hook entry.

4. Restart Claude Code (any active window).

## Configuration

See `settings.example.json` for the exact JSON to merge. Key entries:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python ~/.claude/statusline.py",
    "refreshInterval": 300
  },
  "hooks": {
    "Notification": [
      { "hooks": [{ "type": "command", "command": "python ~/.claude/hooks/notify.py" }] }
    ]
  }
}
```

## Testing

After install:

```powershell
python ~/.claude/tests/test_statusline.py   # scripted state matrix
python ~/.claude/tests/test_notify.py       # fires 3 toasts + 3 sounds
python ~/.claude/tests/test_colors.py       # renders all status colors
```

See `tests/CHECKLIST.md` for cross-window scenarios that can't be scripted.

## Environment variables

- `CLAUDE_QUIET=1` — silence the notification hook (no sounds, no toasts).
- `CLAUDE_STATUSLINE_DEBUG=1` — dump the raw statusline payload to `~/.claude/statusline-payload.json` for inspection.
- `COLUMNS=N` — force terminal width (otherwise autodetected via `CONOUT$` on Windows).

## Known limitations

- **Claude Code refreshes the statusline sparsely during silent work.** During a long thinking turn with no tool calls, the statusline may not repaint — the last drawn frame stays on screen. The dot color reflects whatever was true at the last refresh, not real-time state.
- **Sound differentiation requires custom Sound Scheme.** Windows' default sound scheme maps the Asterisk/Default Beep/Critical Stop events to the same wave file, so the three notification types may sound identical. Customize via Sound Control Panel if you want distinct sounds.
- **Windows-only.** `notify.py` uses `winsound` + PowerShell WinRT for toasts. `statusline.py` is mostly cross-platform but the terminal width detection prefers `CONOUT$` on Windows.

## License

MIT
