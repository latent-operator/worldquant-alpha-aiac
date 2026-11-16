param([int]$TaskId,[int]$RunId,[int]$Results,[int]$Passes,[ValidateSet('COMPLETED','FAILED')][string]$ReviewState)
$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotifier,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.NotificationSetting,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] | Out-Null
$appId='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$notifier=[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
try {
    if($notifier.get_Setting().ToString() -ne 'Enabled'){'DISABLED';exit 0}
} catch {
    # Windows can have no settings entry for this shell AppID before its first toast.
    # Show below remains authoritative for submitting the notification.
}
$xml=[Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml("<toast activationType='protocol' launch='http://127.0.0.1:5174/tasks/$TaskId'><visual><binding template='ToastGeneric'><text>AIAC run $RunId completed</text><text>Results $Results | Quality PASS $Passes | Review $ReviewState. PPA unproven.</text></binding></visual></toast>")
$toast=[Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag="run-$RunId"; $toast.Group='AIAC'
$notifier.Show($toast)
'SUBMITTED'
