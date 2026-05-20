# Installs claude-code-statusline into ~/.claude/.
#
# Copies statusline.py and hooks/notify.py into place, registers the toast
# AppId, and prints the JSON snippet to merge into settings.json. Does NOT
# modify settings.json automatically — you merge it yourself so your existing
# settings stay intact.
#
# Run from the repo root:  .\scripts\install.ps1

$ErrorActionPreference = 'Stop'

# --- preflight ---
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "python is not on PATH. Install Python 3.10+ first."
    exit 1
}

$repoRoot   = Split-Path -Parent $PSScriptRoot
$claudeRoot = Join-Path $env:USERPROFILE '.claude'
$hooksDir   = Join-Path $claudeRoot 'hooks'

if (-not (Test-Path $hooksDir)) {
    New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
}

# --- copy files ---
$files = @(
    @{ src = "$repoRoot\statusline.py";       dst = "$claudeRoot\statusline.py" }
    @{ src = "$repoRoot\hooks\notify.py";     dst = "$hooksDir\notify.py" }
)

foreach ($f in $files) {
    if (Test-Path $f.dst) {
        $backup = "$($f.dst).bak"
        Copy-Item -Path $f.dst -Destination $backup -Force
        Write-Output "Backed up existing: $($f.dst) -> $backup"
    }
    Copy-Item -Path $f.src -Destination $f.dst -Force
    Write-Output "Installed: $($f.dst)"
}

# --- register AppId for toast notifications ---
& "$PSScriptRoot\register-app-id.ps1"

# --- show settings snippet ---
Write-Output ""
Write-Output "================================================================"
Write-Output "Next step: merge this snippet into $claudeRoot\settings.json"
Write-Output "================================================================"
Get-Content "$repoRoot\settings.example.json" | Write-Output
Write-Output ""
Write-Output "Then restart any open Claude Code window."
Write-Output ""
Write-Output "Verify with:"
Write-Output "  python $claudeRoot\tests\test_statusline.py   (if tests are copied)"
Write-Output "  python $repoRoot\tests\test_notify.py         (fires 3 toasts)"
