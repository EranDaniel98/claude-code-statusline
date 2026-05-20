$ErrorActionPreference = 'Stop'
try {
    $null = [Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
    $null = [Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]

    $title = if ($env:TOAST_TITLE) { $env:TOAST_TITLE } else { 'Test toast' }
    $body  = if ($env:TOAST_BODY)  { $env:TOAST_BODY }  else { 'If you see this, toasts work' }

    $xml = @"
<toast>
  <visual>
    <binding template="ToastText02">
      <text id="1">$title</text>
      <text id="2">$body</text>
    </binding>
  </visual>
</toast>
"@

    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc

    $appId = 'Anthropic.ClaudeCode'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    'TOAST OK'
} catch {
    'TOAST FAIL: ' + $_.Exception.Message
}
