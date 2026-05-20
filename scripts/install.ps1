# Installs claude-code-statusline into ~/.claude/.
#
# - Copies statusline.py and hooks/notify.py into place (backs up existing).
# - Registers the toast AppId (Windows only).
# - Deep-merges settings.example.json into ~/.claude/settings.json (with backup).
#
# Run from the repo root:  .\scripts\install.ps1
# Use -DryRun to preview without writing anything.

[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# --- preflight ---
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "python is not on PATH. Install Python 3.10+ first."
    exit 1
}

$repoRoot   = Split-Path -Parent $PSScriptRoot
$claudeRoot = Join-Path $env:USERPROFILE '.claude'
$hooksDir   = Join-Path $claudeRoot 'hooks'

# --- copy files ---
$files = @(
    @{ src = "$repoRoot\statusline.py";   dst = "$claudeRoot\statusline.py" }
    @{ src = "$repoRoot\hooks\notify.py"; dst = "$hooksDir\notify.py" }
)

if ($DryRun) { Write-Output "[dry-run] would create directories and copy files" }
else {
    if (-not (Test-Path $hooksDir)) {
        New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
    }
}

foreach ($f in $files) {
    if (Test-Path $f.dst) {
        $backup = "$($f.dst).bak"
        if ($DryRun) {
            Write-Output "[dry-run] would back up $($f.dst) -> $backup"
        } else {
            Copy-Item -Path $f.dst -Destination $backup -Force
            Write-Output "Backed up: $($f.dst) -> $backup"
        }
    }
    if ($DryRun) {
        Write-Output "[dry-run] would install $($f.src) -> $($f.dst)"
    } else {
        Copy-Item -Path $f.src -Destination $f.dst -Force
        Write-Output "Installed: $($f.dst)"
    }
}

# --- register AppId ---
if ($DryRun) {
    Write-Output "[dry-run] would register Anthropic.ClaudeCode AppId"
} else {
    & "$PSScriptRoot\register-app-id.ps1"
}

# --- merge settings.json ---
$snippet = Join-Path $repoRoot 'settings.example.json'
$settings = Join-Path $claudeRoot 'settings.json'

if ($DryRun) {
    Write-Output "[dry-run] would merge $snippet into $settings (creating $settings.bak)"
} else {
    & $python.Source "$PSScriptRoot\merge_settings.py" $snippet --settings $settings
    if ($LASTEXITCODE -ne 0) {
        Write-Error "settings.json merge failed (exit $LASTEXITCODE)"
        exit 1
    }
}

Write-Output ""
Write-Output "Done. Restart any open Claude Code window."
Write-Output ""
Write-Output "Verify with:"
Write-Output "  python $repoRoot\tests\test_statusline.py    # state matrix"
Write-Output "  python $repoRoot\tests\test_notify.py        # fires 3 notifications"
