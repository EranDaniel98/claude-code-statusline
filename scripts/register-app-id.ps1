# Registers the Anthropic.ClaudeCode AppId in the Windows registry so the
# notification hook's toast notifications display correctly.
#
# Run once: .\scripts\register-app-id.ps1
#
# Windows silently drops toasts from unregistered AppIds. After this runs,
# "Claude Code" will appear in Settings > System > Notifications > "Notifications
# from apps and other senders" and toasts from notify.py will display.

$appId = 'Anthropic.ClaudeCode'
$regPath = "HKCU:\SOFTWARE\Classes\AppUserModelId\$appId"

if (-not (Test-Path $regPath)) {
    New-Item -Path $regPath -Force | Out-Null
}
Set-ItemProperty -Path $regPath -Name 'DisplayName' -Value 'Claude Code'
Set-ItemProperty -Path $regPath -Name 'ShowInSettings' -Value 1 -Type DWord

Write-Output "Registered AppId: $appId"
Write-Output "DisplayName: Claude Code"
Write-Output ""
Write-Output "Fire a test toast to confirm:"
Write-Output "  python ~/.claude/tests/test_notify.py"
