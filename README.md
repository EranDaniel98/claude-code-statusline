# claude-code-statusline

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-blue.svg)](#requirements)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#requirements)
[![tests](https://github.com/EranDaniel98/claude-code-statusline/actions/workflows/test.yml/badge.svg)](https://github.com/EranDaniel98/claude-code-statusline/actions/workflows/test.yml)

Statusline + notification hook + external watcher for [Claude Code](https://claude.com/claude-code).

Tells you at a glance which of your parallel Claude Code windows is busy, idle, or waiting for permission — and surfaces toast notifications when one needs your attention.

---

## Why

When you run several Claude Code sessions in parallel terminals, it's easy to lose track of which one is thinking, which one finished, and which is blocked on a permission prompt. The default TUI doesn't surface that across windows. This adds:

- **A persistent statusline** at the bottom of each Claude Code window with project name, busy indicator, model, context %, rate-limit usage, and elapsed-since-last-activity.
- **A notification hook** that fires distinct sounds and Windows toasts (with project name in the title) when a window wants you.

---

## What it looks like

![Statusline showing project name, green busy dot, model, context %, rate limits, and elapsed time](docs/statusline.png)

| Segment       | Meaning                                                                                     |
|---------------|---------------------------------------------------------------------------------------------|
| `project-name`| `session_name` if set, else `cwd` basename. Truncates with `…` to fit terminal width.       |
| `●` / `WAIT`  | Green `●` = session busy. Red `WAIT` = permission prompt pending. Hidden = idle.            |
| `Opus 4.7 1M` | Model display name; `1M` suffix when 1M context is enabled.                                 |
| `13% (131k)`  | Context used: percent + token count. Dim → yellow ≥60 → bold yellow ≥80 → red ≥90.          |
| `5h:19% 7d:48%`| Rate-limit usage (5-hour / 7-day). Dim → yellow → red as it climbs.                        |
| `41s`         | Seconds since the transcript JSONL was last touched. Dim <30s → yellow <2min → red ≥2min. This is your "is it stuck?" signal. |

The notification hook adds:

- **Permission prompt** → warning beep + toast titled `[project] Permission needed`
- **Idle prompt** (turn ended, awaiting input) → chime + toast titled `[project] Awaiting your input`
- **Elicitation dialog** → asterisk beep + toast titled `[project] Question`

![Windows toast notification with Claude Code title and body](docs/toast.png)

The **external watcher** (`watcher.py`) is the answer to "is anything actually stuck?" — Claude Code refreshes the in-TUI statusline sparsely during silent work, so the dot can lie. Run the watcher in a separate terminal and it polls every session on its own schedule, flips sessions from `● BUSY` → `⌛ THINK` → `⚠ STUCK` as transcripts stay silent, and beeps when a session escalates to STUCK or WAIT:

```
Claude Code Watcher · 2026-05-20 11:45:12
──────────────────────────────────────────────────────────────────────────────
    PID  STATE       PROJECT                           ELAPSED
──────────────────────────────────────────────────────────────────────────────
  25632  ● BUSY      Add progress indicator                 3s
  28104  ⌛ THINK     Other Project                       1m22s
  31840  ⚠ STUCK     Doing Hard Thing                    4m05s
  19200  ▶ WAIT      Yet Another                         12s
──────────────────────────────────────────────────────────────────────────────
```

Run with:
```bash
python watcher.py                 # default 300ms poll
python watcher.py --no-sound      # silent
python watcher.py --interval 1.0  # slower poll
```

---

## Requirements

- **Statusline + watcher**: any OS with Python 3.10+ and Claude Code.
- **Notification hook**: cross-platform via auto-detected backend.
  - **Windows 10/11** — toasts via WinRT (needs the bundled AppId registration); sounds via `winsound.MessageBeep`.
  - **macOS** — toasts via `osascript display notification`; sounds via `afplay` against `/System/Library/Sounds/`.
  - **Linux** — toasts via `notify-send` (install `libnotify`); sounds via `paplay` (PulseAudio) or `aplay` (ALSA), falling back to terminal bell.

---

## Install

### Automated (Windows, recommended)

```powershell
git clone https://github.com/EranDaniel98/claude-code-statusline.git
cd claude-code-statusline
.\scripts\install.ps1
# or .\scripts\install.ps1 -DryRun  to preview without writing anything
```

The script:
1. Copies `statusline.py` and `hooks/notify.py` into `~/.claude/` (backing up any existing files).
2. Registers the `Anthropic.ClaudeCode` AppId so Windows actually displays toasts (Windows silently drops toasts from unregistered AppIds).
3. **Deep-merges `settings.example.json` into `~/.claude/settings.json`** with a `.bak` of your old file. Existing unrelated settings (other hooks, custom rules) are preserved.

Restart any open Claude Code window after install.

### Manual (any platform)

1. Copy files into `~/.claude/`:

   **Windows (PowerShell):**
   ```powershell
   Copy-Item statusline.py "$env:USERPROFILE\.claude\statusline.py"
   New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\hooks" | Out-Null
   Copy-Item hooks\notify.py "$env:USERPROFILE\.claude\hooks\notify.py"
   ```

   **macOS / Linux:**
   ```bash
   mkdir -p ~/.claude/hooks
   cp statusline.py ~/.claude/statusline.py
   cp hooks/notify.py ~/.claude/hooks/notify.py
   ```

2. (Windows only) Register the toast AppId:
   ```powershell
   .\scripts\register-app-id.ps1
   ```

3. Merge `settings.example.json` into `~/.claude/settings.json` (the included helper does a deep-merge with backup, on any OS):
   ```bash
   python scripts/merge_settings.py settings.example.json
   ```

4. Restart any open Claude Code window.

---

## Verify

From the repo root (no install required):

```bash
python tests/test_statusline.py    # state matrix; should print "9/9 passed"
python tests/test_watcher.py       # watcher logic; should print "7/7 passed"
python tests/test_notify.py        # fires 3 toasts + 3 sounds (manual confirm)
python tests/test_colors.py        # renders the dot/WAIT states
```

Then open a Claude Code window and confirm:
- Green `●` appears between project name and model while Claude is responding.
- Dot disappears within ~1s after the response finishes.

See [`tests/CHECKLIST.md`](tests/CHECKLIST.md) for cross-window scenarios.

---

## Environment variables

| Variable                       | Effect                                                                          |
|--------------------------------|---------------------------------------------------------------------------------|
| `CLAUDE_QUIET=1`               | Silence the notification hook (no sounds, no toasts).                           |
| `CLAUDE_STATUSLINE_DEBUG=1`    | Dump the raw statusline payload to `~/.claude/statusline-payload.json`.         |
| `CLAUDE_SESSIONS_DIR=<path>`   | Override the sessions dir read by the statusline + watcher (default `~/.claude/sessions`). Useful for tests and isolated environments. |
| `COLUMNS=N`                    | Force terminal width (otherwise autodetected via `CONOUT$` on Windows).         |

---

## Troubleshooting

**Toasts don't appear (sounds work).** The `Anthropic.ClaudeCode` AppId isn't registered. Run `.\scripts\register-app-id.ps1`. After registration, "Claude Code" appears in **Settings → System → Notifications → Notifications from apps and other senders** — make sure it's toggled on, and that Focus / Do not disturb is off.

**Sounds all sound identical.** Windows' default sound scheme maps Asterisk / Default Beep / Critical Stop to the same `.wav`. Customize the events in **Sound Control Panel → Sounds** if you want distinct audio cues.

**Dot never appears.** Verify Python is on `PATH` and the statusLine command in settings.json points to the right path. Run `python ~/.claude/statusline.py < ~/.claude/statusline-payload.json` (after setting `CLAUDE_STATUSLINE_DEBUG=1` once to generate the payload) to see direct output.

**Project name truncated even on a wide terminal.** Terminal width detection isn't picking up your console. Set `COLUMNS=120` (or whatever your width is) in your shell profile.

---

## Known limitations

- **Sparse refresh during silent work.** Claude Code doesn't fire statusline refreshes on the `refreshInterval` timer reliably during long thinking turns or long tool calls — repaints fire on state events (start/end of turn, permission prompts). The dot accurately reflects state *at the last paint*, not in real time. **Run `watcher.py` in a separate terminal** to get reliable real-time "stuck" detection; it polls on its own schedule and doesn't depend on Claude Code's repaint cadence.
- **Toast latency on Windows.** `hooks/notify.py` spawns a fresh PowerShell per notification (~300ms startup). Fire-and-forget via `Popen` so it doesn't block, but the toast itself appears ~300ms after the event. A persistent helper would be cleaner — future work.

---

## Design notes

- **Statusline reads stdin payload** Claude Code provides on every render: session id, transcript path, cwd, model, context window, rate limits.
- **Busy state comes from `~/.claude/sessions/<PID>.json`** — Claude Code's `status` field there is the authoritative liveness signal (`idle` / `busy` / `waiting`).
- **Elapsed is transcript JSONL mtime**, which only updates on message/tool completion (not during silent thinking). This is what makes it a stuck-detector.
- **Toast AppId is registered in `HKCU:\SOFTWARE\Classes\AppUserModelId\Anthropic.ClaudeCode`** — Microsoft's documented way to register a standalone notifier without needing a Start Menu shortcut.
- **Notification backends are platform-detected.** `hooks/notify.py` defines a `NotificationBackend` ABC with `WindowsBackend` / `MacOSBackend` / `LinuxBackend` implementations selected via `sys.platform`. Adding a new platform is one new class.
- **The watcher polls externally**, free of Claude Code's repaint quirks. It scans `~/.claude/sessions/*.json` plus the matching transcript JSONLs in `~/.claude/projects/<encoded-cwd>/`, classifies each session, and beeps when severity *escalates* (debounced — won't beep continuously while a session sits in WAIT).

---

## License

MIT — see [LICENSE](LICENSE).
