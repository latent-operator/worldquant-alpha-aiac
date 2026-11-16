[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Start','Stop','Status')][string]$Action,
    [Parameter(Mandatory)][string]$RuntimeHome
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $RuntimeHome).Path
$release = Get-Content -LiteralPath (Join-Path $root 'current.json') -Raw | ConvertFrom-Json
$recipe = $release.recipe_source
if ((& git -C $recipe rev-parse HEAD).Trim() -ne $release.recipe_commit) {
    throw 'Release recipe commit mismatch'
}
$source = $release.source
$python = Join-Path $source '.venv\Scripts\python.exe'
$envFile = Join-Path $root '.env'
foreach ($service in @('api','ui')) {
    $pidFile = Join-Path $root "$service.pid.json"
    $saved = if (Test-Path -LiteralPath $pidFile) { Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json } else { $null }
    $process = if ($saved) { Get-CimInstance Win32_Process -Filter "ProcessId = $($saved.pid)" -ErrorAction SilentlyContinue } else { $null }
    $managed = $process -and $process.CommandLine -and $process.CommandLine.Contains('scripts.launch_clean') -and $process.CommandLine.Contains("--service $service") -and $process.CommandLine.Contains($root)
    if ($Action -eq 'Status') { [pscustomobject]@{service=$service; running=[bool]$managed}; continue }
    if ($Action -eq 'Stop') {
        if ($managed) { Stop-Process -Id $saved.pid -ErrorAction Stop }
        if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
        continue
    }
    if ($managed) { continue }
    if (Test-Path -LiteralPath (Join-Path $root 'maintenance.lock')) { throw 'Clean runtime is under maintenance' }
    $port = if ($service -eq 'api') { $release.api_port } else { $release.ui_port }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "Port $port is already in use" }
    $arguments = '-m scripts.launch_clean --service ' + $service + ' --source "' + $source + '" --env-file "' + $envFile + '" --port ' + $port + ' --api-port ' + $release.api_port
    $child = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $recipe -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root "$service.out.log") -RedirectStandardError (Join-Path $root "$service.err.log")
    @{pid=$child.Id;service=$service;commit=$release.recipe_commit} | ConvertTo-Json | Set-Content -LiteralPath $pidFile
}
