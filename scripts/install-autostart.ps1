[CmdletBinding()]
param([string]$TaskName = 'AIAC-Local-Ensure')

$ErrorActionPreference = 'Stop'
$manager = (Resolve-Path (Join-Path $PSScriptRoot 'manage-local.ps1')).Path
$pwsh = Join-Path $PSHOME 'pwsh.exe'
if (-not (Test-Path -LiteralPath $pwsh)) { throw 'PowerShell 7 is required.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pwsh -Argument (
    '-NoProfile -WindowStyle Hidden -File "' + $manager + '" -Action Start'
)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Output "Installed $TaskName for $identity. It starts only the version in runtime/aiac-ops/current.json."
