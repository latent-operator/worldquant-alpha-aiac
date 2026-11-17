[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Start','Stop','Status','Update','Rollback')][string]$Action,
    [string]$RuntimeHome='G:\安装\claude\runtime\aiac-clean',
    [string]$Commit='HEAD',
    [switch]$EnableWorker
)
$ErrorActionPreference='Stop'
$root=(Resolve-Path -LiteralPath $RuntimeHome).Path
$code=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pointer=Join-Path $root 'current.json'
$mutex=[Threading.Mutex]::new($false,'Local\AIAC-Clean-Operations')
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); throw 'Another local operation is running' }
function Native-OK { if ($LASTEXITCODE -ne 0) { throw 'Local command failed; current release preserved' } }
function Read-Release { Get-Content -LiteralPath $pointer -Raw | ConvertFrom-Json }
function Stop-Services {
    foreach ($service in @('worker','api','ui')) {
        $pidPath=Join-Path $root "$service.pid.json"
        if (-not (Test-Path -LiteralPath $pidPath)) { continue }
        $saved=Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
        $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)" -ErrorAction SilentlyContinue
        if ($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($root) -and $proc.CommandLine.Contains("--service $service") -and ($proc.CommandLine.Contains('ops.launch') -or $proc.CommandLine.Contains('scripts.launch_clean'))) {
            Stop-Process -Id $saved.pid -ErrorAction Stop
            Wait-Process -Id $saved.pid -Timeout 10 -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $pidPath
    }
}
function Start-Services {
    $release=Read-Release
    if (Test-Path -LiteralPath (Join-Path $root 'maintenance.lock')) { throw 'Maintenance hold active' }
    $services=@('api','ui')
    if ($release.worker_enabled) {$services += 'worker'}
    foreach ($service in $services) {
        $pidPath=Join-Path $root "$service.pid.json"
        if (Test-Path -LiteralPath $pidPath) {
            $saved=Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
            $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($root) -and $proc.CommandLine.Contains("--service $service") -and $proc.CommandLine.Contains('ops.launch')) { continue }
        }
        $port=if ($service -eq 'api') { $release.api_port } elseif($service -eq 'ui') { $release.ui_port } else { 0 }
        if ($port -and (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) { throw "Port $port already occupied" }
        if($service -eq 'worker'){
            $others=@(Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -and $_.CommandLine.Contains('ops.launch') -and $_.CommandLine.Contains('--service worker') -and $_.CommandLine.Contains($root)})
            if($others){throw 'A worker already exists outside the saved PID'}
        }
        $arguments='-m ops.launch --runtime "'+$root+'" --service '+$service
        $child=Start-Process -FilePath $release.python_executable -ArgumentList $arguments -WorkingDirectory $code -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root "$service.out.log") -RedirectStandardError (Join-Path $root "$service.err.log")
        @{pid=$child.Id;service=$service;commit=$release.commit} | ConvertTo-Json | Set-Content -LiteralPath $pidPath
    }
}
function Boundary {
    $counts=& docker exec aiac-clean-db psql -U aiac_clean -d aiac_clean -Atc "SELECT count(*) FROM mining_tasks WHERE status='RUNNING'; SELECT count(*) FROM experiment_runs WHERE status='RUNNING'; SELECT count(*) FROM trace_steps WHERE output_data->>'request_status' IN ('REQUESTED','RESERVED','ACCEPTED','UNKNOWN','NOT_REQUESTED');"
    Native-OK
    if (($counts -join ',') -ne '0,0,0') { throw 'Active or unresolved research prevents code switch; no state was rewritten' }
    $queue=& docker exec aiac-clean-redis redis-cli LLEN celery
    Native-OK
    if ($queue -ne '0') { throw 'Pending queue prevents code switch' }
}
function Wait-Health {
    $release=Read-Release
    for ($i=0;$i -lt 20;$i++) {
        try {
            $api=Invoke-RestMethod "http://127.0.0.1:$($release.api_port)/health" -TimeoutSec 2
            $ui=Invoke-RestMethod "http://127.0.0.1:$($release.ui_port)/__ui_health" -TimeoutSec 2
            if ($api.status -eq 'ok' -and $ui.commit -eq $release.commit) {
                if($release.worker_enabled){
                    $worker=Invoke-RestMethod "http://127.0.0.1:$($release.api_port)/api/v1/runs/bounded/health" -TimeoutSec 5
                    if(-not $worker.worker_online){continue}
                }
                return
            }
        } catch {}
        Start-Sleep -Seconds 2
    }
    throw 'Local health acceptance failed'
}
try {
    if ($Action -eq 'Status') {
        $release=Read-Release
        $release | Select-Object source,commit,worker_enabled
        foreach ($service in @('api','ui','worker')) {
            $pidPath=Join-Path $root "$service.pid.json"
            $running=$false
            if (Test-Path -LiteralPath $pidPath) {
                $saved=Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
                $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)" -ErrorAction SilentlyContinue
                $running=[bool]($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($root) -and $proc.CommandLine.Contains("--service $service"))
            }
            [pscustomobject]@{service=$service;running=$running}
        }
    } elseif ($Action -eq 'Stop') { Stop-Services
    } elseif ($Action -eq 'Start') { Start-Services; Wait-Health
    } else {
        Boundary
        $before=Read-Release
        $old=$before
        # One-time compatibility with the already deployed original installation.
        if (-not $old.commit) {
            $old=[pscustomobject]@{source=$before.source;commit=$before.upstream_commit;python_executable=(Join-Path $before.source '.venv\Scripts\python.exe');ui_dist=(Join-Path $root 'ui-dist');api_port=8003;ui_port=5174;worker_enabled=$false;research_hold=$true}
        }
        if ($Action -eq 'Update') {
            $sha=(& git -C $code rev-parse $Commit).Trim(); Native-OK
            if (& git -C $code status --porcelain --untracked-files=no) { throw 'Commit changes before local delivery' }
            $source=Join-Path $root ('source-'+$sha)
            if (-not (Test-Path -LiteralPath $source)) { & git -C $code worktree add --detach $source $sha; Native-OK }
            if ((& git -C $source rev-parse HEAD).Trim() -ne $sha) { throw 'Release source mismatch' }
            if (-not (Test-Path -LiteralPath (Join-Path $source 'frontend/dist/index.html'))) {
                Push-Location (Join-Path $source 'frontend')
                try { & npm.cmd ci --ignore-scripts --no-audit --no-fund; Native-OK; & npm.cmd run build; Native-OK } finally { Pop-Location }
            }
            $next=[pscustomobject]@{source=$source;commit=$sha;python_executable=$old.python_executable;ui_dist=(Join-Path $source 'frontend/dist');api_port=$old.api_port;ui_port=$old.ui_port;worker_enabled=$false;research_hold=$true;delivery='local-verified-ci-asynchronous'}
            if($EnableWorker -or $old.worker_enabled){
                $next.worker_enabled=$true
                $next | Add-Member -NotePropertyMembers @{execution_mode='native'}
            }
        } else { $next=Get-Content -LiteralPath (Join-Path $root 'previous.json') -Raw | ConvertFrom-Json }
        $record=[ordered]@{at=(Get-Date).ToUniversalTime().ToString('o');action=$Action;before=$old;after=$next;database_restore=$false;status='PREPARING'}
        $recordPath=Join-Path $root ('change-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.json')
        $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath
        try {
            Stop-Services; Boundary
            $next | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $pointer
            Start-Services; Wait-Health
            $old | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $root 'previous.json')
            if(Get-ScheduledTask -TaskName 'AIAC-Clean-Ensure' -ErrorAction SilentlyContinue){
                $manager=Join-Path $next.source 'ops/local.ps1'
                $ensureAction=New-ScheduledTaskAction -Execute (Get-Command pwsh).Source -Argument ('-NoProfile -WindowStyle Hidden -File "'+$manager+'" -Action Start -RuntimeHome "'+$root+'"')
                Set-ScheduledTask -TaskName 'AIAC-Clean-Ensure' -Action $ensureAction | Out-Null
            }
            $record.status='RELEASED'
        } catch {
            Stop-Services
            $old | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $pointer
            Start-Services; Wait-Health
            $record.status='ROLLED_BACK'
            throw
        } finally { $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath }
    }
} finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
