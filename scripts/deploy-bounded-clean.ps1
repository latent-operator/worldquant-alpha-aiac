[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RuntimeHome,
    [Parameter(Mandatory)][string]$Commit,
    [Parameter(Mandatory)][long]$OfflineCI,
    [Parameter(Mandatory)][long]$NativeCI
)
$ErrorActionPreference='Stop'
$root=(Resolve-Path -LiteralPath $RuntimeHome).Path
$recipe=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
function Native-OK([string]$label) { if ($LASTEXITCODE -ne 0) { throw "$label failed" } }
if ((& git -C $recipe rev-parse HEAD).Trim() -ne $Commit) { throw 'Wrong release commit' }
if (& git -C $recipe status --porcelain --untracked-files=no) { throw 'Modified release recipe' }
foreach ($runId in @($OfflineCI,$NativeCI)) {
    $ci=(& gh api "repos/latent-operator/worldquant-alpha-aiac/actions/runs/$runId") | ConvertFrom-Json
    Native-OK 'Read CI'
    if ($ci.conclusion -ne 'success' -or $ci.head_sha -ne $Commit -or $ci.event -ne 'push' -or $ci.head_branch -ne 'master') { throw 'Exact master-push CI required' }
}
$currentPath=Join-Path $root 'current.json'
$previous=Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json
$python=if ($previous.python_executable) { $previous.python_executable } else { Join-Path $previous.source '.venv\Scripts\python.exe' }
$stamp=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$archive=Join-Path $root ('bounded-release-'+$stamp)
New-Item -ItemType Directory -Path $archive | Out-Null
$source=Join-Path $root ('source-'+$Commit)
$journal=[ordered]@{status='PREPARING';commit=$Commit;timestamp=$stamp;ci_runs=@($OfflineCI,$NativeCI);previous=$previous;archive=$archive;source=$source;legacy_frozen=$true}
$journalPath=Join-Path $archive 'release.json'
function Save-Journal { $journal | ConvertTo-Json -Depth 14 | Set-Content -LiteralPath $journalPath }
function Boundary {
    $active=& docker exec aiac-clean-db psql -U aiac_clean -d aiac_clean -Atc "SELECT count(*) FROM mining_tasks WHERE status='RUNNING'; SELECT count(*) FROM experiment_runs WHERE status='RUNNING'; SELECT count(*) FROM trace_steps WHERE output_data->>'request_status' IN ('REQUESTED','RESERVED','ACCEPTED','UNKNOWN','NOT_REQUESTED');"
    Native-OK 'Research boundary'
    if (($active -join ',') -ne '0,0,0') { throw 'Incomplete active research boundary; preserve it and stop release' }
    $queue=& docker exec aiac-clean-redis redis-cli LLEN celery
    Native-OK 'Queue check'
    if ($queue -ne '0') { throw 'Pending Celery work prevents release' }
}
Save-Journal
Boundary
if (-not (Test-Path -LiteralPath $source)) {
    & $python -m scripts.build_clean_runtime --source (Join-Path (Split-Path $root -Parent) '..\work\aiac-clean') --target $source
    Native-OK 'Build pinned native source'
    Push-Location (Join-Path $source 'frontend')
    try { & npm.cmd ci --ignore-scripts --no-audit --no-fund; Native-OK 'UI dependencies'; & npm.cmd run build; Native-OK 'UI build' } finally { Pop-Location }
    & $python -c 'from pathlib import Path; import sys; from scripts.build_clean_runtime import seal_ui,validate_build; p=Path(sys.argv[1]); seal_ui(p); validate_build(p)' $source
    Native-OK 'Seal build'
}
$ensure=Get-ScheduledTask -TaskName 'AIAC-Clean-Ensure'
Export-ScheduledTask -TaskName 'AIAC-Clean-Ensure' | Set-Content -LiteralPath (Join-Path $archive 'ensure-before.xml')
Disable-ScheduledTask -TaskName 'AIAC-Clean-Ensure' | Out-Null
'Audited native worker release in progress' | Set-Content -LiteralPath (Join-Path $root 'maintenance.lock')
$oldManager=Join-Path $previous.recipe_source 'scripts\manage-clean.ps1'
try {
    Boundary
    & $oldManager -Action Stop -RuntimeHome $root
    Boundary
    $dump=Join-Path $archive 'active.dump'
    & docker exec aiac-clean-db pg_dump -U aiac_clean -d aiac_clean -Fc -f /tmp/aiac-bounded-release.dump
    Native-OK 'Backup'
    & docker cp aiac-clean-db:/tmp/aiac-bounded-release.dump $dump
    Native-OK 'Copy backup'
    $restore='aiac_bounded_restore_'+$stamp.ToLower()
    & docker exec aiac-clean-db createdb -U aiac_clean $restore
    Native-OK 'Create restore validation database'
    & docker exec aiac-clean-db pg_restore -U aiac_clean -d $restore --exit-on-error /tmp/aiac-bounded-release.dump
    Native-OK 'Validate restore'
    $sql="SELECT json_build_object('tasks',(SELECT count(*) FROM mining_tasks),'runs',(SELECT count(*) FROM experiment_runs),'traces',(SELECT count(*) FROM trace_steps),'alphas',(SELECT count(*) FROM alphas),'configs',(SELECT count(*) FROM system_configs));"
    $before=& docker exec aiac-clean-db psql -U aiac_clean -d aiac_clean -Atc $sql
    $restored=& docker exec aiac-clean-db psql -U aiac_clean -d $restore -Atc $sql
    if ($before -ne $restored) { throw 'Restored count verification failed' }
    & docker exec aiac-clean-db psql -U aiac_clean -d aiac_clean -c "ALTER DATABASE $restore SET default_transaction_read_only = on;"
    Native-OK 'Protect restored backup'
    $journal.backup=@{path=$dump;sha256=(Get-FileHash -LiteralPath $dump -Algorithm SHA256).Hash.ToLower();restore_database=$restore;restore_verified=$true;counts=($before|ConvertFrom-Json)}
    $history=Join-Path $root 'research-20261116-fundamental72\history-exclusions.json'
    $next=@{source=$source;upstream_commit='8ffda431d08703b7f6ae13c8287806fd76eb63e0';recipe_source=$recipe;recipe_commit=$Commit;python_executable=$python;api_port=8003;ui_port=5174;stage='production';research_hold=$true;worker_enabled=$true;execution_mode='bounded';history_exclusions=$history;history_exclusions_sha256=(Get-FileHash -LiteralPath $history -Algorithm SHA256).Hash.ToLower();release_record=$journalPath;ci_runs=@($OfflineCI,$NativeCI)}
    $next | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $currentPath
    Remove-Item -LiteralPath (Join-Path $root 'maintenance.lock')
    $manager=Join-Path $recipe 'scripts\manage-clean.ps1'
    & $manager -Action Start -RuntimeHome $root
    $ready=$false
    for ($i=0;$i -lt 40;$i++) {
        try {
            $api=Invoke-RestMethod 'http://127.0.0.1:8003/health' -TimeoutSec 3
            $worker=Invoke-RestMethod 'http://127.0.0.1:8003/api/v1/runs/bounded/health' -TimeoutSec 5
            $ui=Invoke-RestMethod 'http://127.0.0.1:5174/__ui_health' -TimeoutSec 3
            if ($api.status -eq 'ok' -and $worker.worker_online -and $worker.workers.Count -eq 1 -and $ui.commit -eq $Commit) { $ready=$true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $ready) { throw 'Native runtime health acceptance failed' }
    Boundary
    $action=New-ScheduledTaskAction -Execute (Get-Command pwsh).Source -Argument ('-NoProfile -WindowStyle Hidden -File "'+$manager+'" -Action Start -RuntimeHome "'+$root+'"')
    Set-ScheduledTask -TaskName 'AIAC-Clean-Ensure' -Action $action | Out-Null
    Enable-ScheduledTask -TaskName 'AIAC-Clean-Ensure' | Out-Null
    $journal.status='RELEASED_BOUNDED_WORKER'; $journal.worker_health=$worker; $journal.completed_batch_gate=$true; $journal.zero_pending_gate=$true
    Save-Journal
    Write-Output "Released bounded native worker: $journalPath"
} catch {
    $journal.status='FAILED_ROLLED_BACK'; $journal.error_type=$_.Exception.GetType().Name
    $current=Get-Content -LiteralPath $currentPath -Raw | ConvertFrom-Json
    & (Join-Path $current.recipe_source 'scripts\manage-clean.ps1') -Action Stop -RuntimeHome $root
    $previous | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $currentPath
    if (Test-Path -LiteralPath (Join-Path $root 'maintenance.lock')) { Remove-Item -LiteralPath (Join-Path $root 'maintenance.lock') }
    & $oldManager -Action Start -RuntimeHome $root
    Set-ScheduledTask -TaskName 'AIAC-Clean-Ensure' -Action $ensure.Actions | Out-Null
    Enable-ScheduledTask -TaskName 'AIAC-Clean-Ensure' | Out-Null
    Save-Journal
    throw 'Release failed; previous source restored, database never automatically restored. See private journal.'
}
