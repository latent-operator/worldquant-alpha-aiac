[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Start', 'Stop', 'Status')][string]$Action,
    [string]$Source,
    [string]$Commit,
    [switch]$AllowMaintenance,
    [ValidateSet('api', 'ui', 'worker')][string[]]$Services = @('api', 'ui', 'worker')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $root '..\..\runtime\aiac-ops'))
$pidRoot = Join-Path $runtimeRoot 'pids'
$logRoot = Join-Path $runtimeRoot 'logs'
$envFile = Join-Path $root '.env'
$currentFile = Join-Path $runtimeRoot 'current.json'
$maintenanceFile = Join-Path $runtimeRoot 'maintenance.lock'

function Get-Managed([string]$service) {
    $file = Join-Path $pidRoot "$service.json"
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $saved = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($saved.pid)" -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.CommandLine -or
        -not $process.CommandLine.Contains('scripts.launch_runtime') -or
        -not $process.CommandLine.Contains("--service $service")) { return $null }
    return [pscustomobject]@{ Saved = $saved; Process = $process; File = $file }
}

function Assert-NoOtherLauncher([string]$service, $managed) {
    $others = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and
        $_.CommandLine.Contains('scripts.launch_runtime') -and
        $_.CommandLine.Contains("--service $service") -and
        (-not $managed -or $_.ProcessId -ne $managed.Saved.pid)
    }
    if ($others) { throw "Unmanaged $service process exists; refusing a duplicate." }
}

New-Item -ItemType Directory -Path $pidRoot, $logRoot -Force | Out-Null
if ($Action -eq 'Start') {
    if ((Test-Path -LiteralPath $maintenanceFile) -and -not $AllowMaintenance) {
        Write-Output '{"skipped":"deployment maintenance"}'
        return
    }
    if (-not $Source -or -not $Commit) {
        if (-not (Test-Path -LiteralPath $currentFile)) { throw 'No released version is recorded.' }
        $current = Get-Content -LiteralPath $currentFile -Raw | ConvertFrom-Json
        $Source = $current.source
        $Commit = $current.commit
    }
    & docker compose -p aiac-local -f (Join-Path $root 'compose.local.yaml') up -d --no-recreate db redis | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Existing AIAC database or Redis could not start.' }
}
$result = @()
foreach ($service in $Services) {
    $managed = Get-Managed $service
    if ($Action -eq 'Status') {
        $result += [pscustomobject]@{
            service = $service; running = [bool]$managed
            pid = if ($managed) { $managed.Saved.pid } else { $null }
            commit = if ($managed) { $managed.Saved.commit } else { $null }
        }
        continue
    }

    if ($Action -eq 'Stop') {
        if ($managed) {
            Stop-Process -Id $managed.Saved.pid -ErrorAction Stop
            for ($i = 0; $i -lt 30; $i++) {
                if (-not (Get-Process -Id $managed.Saved.pid -ErrorAction SilentlyContinue)) { break }
                Start-Sleep -Milliseconds 200
            }
            if (Get-Process -Id $managed.Saved.pid -ErrorAction SilentlyContinue) {
                throw "Could not stop $service process."
            }
        }
        $file = Join-Path $pidRoot "$service.json"
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
        $result += [pscustomobject]@{ service = $service; stopped = $true }
        continue
    }

    if (-not $Source -or -not $Commit) { throw 'Start requires -Source and -Commit.' }
    if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment is missing.' }
    if (-not (Test-Path -LiteralPath $envFile)) { throw 'Private .env is missing.' }
    $resolvedSource = (Resolve-Path -LiteralPath $Source).Path
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedSource 'frontend\dist\index.html'))) {
        throw 'Release source has no built UI.'
    }
    Assert-NoOtherLauncher $service $managed
    if ($managed) {
        if ($managed.Saved.commit -ne $Commit -or $managed.Saved.source -ne $resolvedSource) {
            throw "Different $service version is already running."
        }
        $result += [pscustomobject]@{ service = $service; running = $true; pid = $managed.Saved.pid }
        continue
    }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $stdout = Join-Path $logRoot "$service-$stamp.out.log"
    $stderr = Join-Path $logRoot "$service-$stamp.err.log"
    $arguments = @(
        '-m', 'scripts.launch_runtime', '--service', $service,
        '--source', ('"' + $resolvedSource + '"'),
        '--env-file', ('"' + $envFile + '"'), '--commit', $Commit
    )
    $started = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Start-Sleep -Seconds 2
    if ($started.HasExited) { throw "$service exited during startup; inspect $stderr" }
    [ordered]@{
        service = $service; pid = $started.Id; commit = $Commit
        source = $resolvedSource; started_at = (Get-Date).ToUniversalTime().ToString('o')
        stdout = $stdout; stderr = $stderr
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $pidRoot "$service.json") -Encoding utf8
    $result += [pscustomobject]@{ service = $service; running = $true; pid = $started.Id }
}
$result | ConvertTo-Json -Depth 4
