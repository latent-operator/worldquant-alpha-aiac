[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$Commit,
    [Parameter(Mandatory)][ValidateRange(1, [int]::MaxValue)][int]$CiRun,
    [string]$BootstrapPreviousSource
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $root '..\..\runtime\aiac-ops'))
$envFile = Join-Path $root '.env'
$manager = Join-Path $root 'scripts\manage-local.ps1'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$source = Join-Path $runtimeRoot "source-$Commit"
$snapshot = Join-Path $runtimeRoot "tasks-before-$stamp.json"
$dumpPath = Join-Path $runtimeRoot "db-before-$stamp.dump"
$recordPath = Join-Path $runtimeRoot "release-$stamp.json"
$currentFile = Join-Path $runtimeRoot 'current.json'
$maintenanceFile = Join-Path $runtimeRoot 'maintenance.lock'
$previousSource = $null
$previousCommit = $null
$before = $null
$ci = $null
$paused = $false
$schemaChanged = $false
$settled = $false
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

function Assert-Native([string]$label) {
    if ($LASTEXITCODE -ne 0) { throw "$label failed with exit code $LASTEXITCODE" }
}

function Wait-Http([string]$url) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 2
    }
    throw "Health check failed at $url"
}

function Write-ReleaseRecord([string]$outcome, [string]$detail) {
    [ordered]@{
        at_utc = (Get-Date).ToUniversalTime().ToString('o')
        outcome = $outcome
        commit = $Commit
        source = $source
        previous_commit = $previousCommit
        previous_source = $previousSource
        ci_run = $CiRun
        ci_url = if ($ci) { $ci.ci_url } else { $null }
        db_revision_before = if ($before) { $before.db_revision } else { $null }
        backup = if (Test-Path -LiteralPath $dumpPath) { $dumpPath } else { $null }
        task_snapshot = if (Test-Path -LiteralPath $snapshot) { $snapshot } else { $null }
        detail = $detail
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $recordPath -Encoding utf8
}

function Verify-Services([string]$expectedCommit) {
    Wait-Http 'http://127.0.0.1:8003/health'
    $ui = Invoke-RestMethod 'http://127.0.0.1:5174/__ui_health' -TimeoutSec 5
    if ($ui.status -ne 'ok' -or $ui.commit -ne $expectedCommit) { throw 'UI release identity does not match.' }
    Wait-Http 'http://127.0.0.1:5174/tasks'
    Wait-Http 'http://127.0.0.1:5174/api/v1/tasks'
    $services = & $manager -Action Status | ConvertFrom-Json
    if (($services | Where-Object { $_.service -eq 'worker' -and $_.running }).Count -ne 1) {
        throw 'Exactly one managed worker must be running.'
    }
}

Push-Location $root
try {
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $envFile)) {
        throw 'Python environment or private .env is missing.'
    }
    $ciText = & $python scripts/verify_release.py --commit $Commit --ci-run $CiRun
    Assert-Native 'Release gate'
    $ci = $ciText | ConvertFrom-Json
    & $python scripts/preflight.py
    Assert-Native 'Offline preflight'

    $volumeMarker = Join-Path $runtimeRoot 'db-volume-id.txt'
    if (-not (Test-Path -LiteralPath $volumeMarker)) { throw 'Expected database volume ID is not recorded.' }
    $expectedVolume = (Get-Content -LiteralPath $volumeMarker -Raw).Trim()
    $actualVolume = (& docker inspect aiac-local-db-1 --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}').Trim()
    Assert-Native 'Database volume inspection'
    if ($actualVolume -ne $expectedVolume) { throw 'Live database volume differs from the preserved volume.' }

    $last = Get-ChildItem -LiteralPath $runtimeRoot -Filter 'release-*.json' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($last) {
        $prior = Get-Content -LiteralPath $last.FullName -Raw | ConvertFrom-Json
        if ($prior.outcome -ne 'SUCCESS') { throw 'Latest release needs operator review before another deployment.' }
        if ($prior.commit -eq $Commit) {
            Verify-Services $Commit
            Write-Output "ALREADY_DEPLOYED commit=$Commit record=$($last.FullName)"
            return
        }
        $previousSource = $prior.source
        $previousCommit = $prior.commit
    } else {
        if (-not $BootstrapPreviousSource) { throw 'First release requires the preserved previous source and built UI.' }
        $previousSource = (Resolve-Path -LiteralPath $BootstrapPreviousSource).Path
        $previousCommit = 'preops-20261022'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $previousSource 'frontend\dist\index.html'))) {
        throw 'Previous code has no tested web bundle for rollback.'
    }

    $managed = & $manager -Action Status | ConvertFrom-Json
    $unmanagedWorker = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -and
        $_.CommandLine.Contains('backend.celery_app') -and
        -not $_.CommandLine.Contains('scripts.launch_runtime')
    }
    if ($unmanagedWorker) { throw 'An unmanaged AIAC worker is running; refusing a duplicate.' }
    foreach ($port in @(8003, 5174)) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        $service = if ($port -eq 8003) { 'api' } else { 'ui' }
        if ($listener -and -not ($managed | Where-Object { $_.service -eq $service -and $_.running })) {
            throw "Port $port is held by an unmanaged process."
        }
    }

    if (-not (Test-Path -LiteralPath $source)) {
        & git worktree add --detach $source $Commit
        Assert-Native 'Release worktree checkout'
    }
    if ((& git -C $source rev-parse HEAD).Trim() -ne $Commit -or (& git -C $source status --porcelain=v1)) {
        throw 'Release source is not the exact clean commit.'
    }
    Push-Location (Join-Path $source 'frontend')
    try {
        & npm.cmd ci
        Assert-Native 'Frontend release install'
        & npm.cmd run build
        Assert-Native 'Frontend release build'
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath (Join-Path $source 'frontend\dist\index.html'))) {
        throw 'Released frontend build is missing.'
    }

    if (Test-Path -LiteralPath $maintenanceFile) { throw 'Another deployment is in maintenance.' }
    $stamp | Set-Content -LiteralPath $maintenanceFile -Encoding ascii
    & $python -m scripts.release_tasks pause --snapshot $snapshot
    Assert-Native 'Pause active tasks'
    $paused = $true
    $before = Get-Content -LiteralPath $snapshot -Raw | ConvertFrom-Json
    if ($managed | Where-Object { $_.service -eq 'worker' -and $_.running }) {
        & $python -m scripts.wait_worker_idle --timeout-seconds 7200
        Assert-Native 'Safe worker boundary'
    }
    & $manager -Action Stop -Services @('worker', 'api', 'ui') | Out-Null
    $quiescentAlphaCount = (& docker exec aiac-local-db-1 psql -U aiac_local -d alpha_gpt_local -tAc 'SELECT count(*) FROM alphas').Trim()
    Assert-Native 'Quiescent Alpha count'
    if ([int]$quiescentAlphaCount -lt [int]$before.alpha_count) {
        throw 'Alpha result count fell before backup.'
    }

    $dumpName = "aiac-release-$stamp.dump"
    & docker exec aiac-local-db-1 pg_dump -U aiac_local -d alpha_gpt_local -Fc -f "/tmp/$dumpName"
    Assert-Native 'Database backup'
    & docker cp "aiac-local-db-1:/tmp/$dumpName" $dumpPath
    Assert-Native 'Database backup copy'
    if ((Get-Item -LiteralPath $dumpPath).Length -lt 1024) { throw 'Database backup is unexpectedly small.' }
    $verifyDb = "aiac_verify_$($stamp -replace '[^0-9]', '')"
    & docker exec aiac-local-db-1 createdb -U aiac_local $verifyDb
    Assert-Native 'Isolated restore database creation'
    try {
        & docker exec aiac-local-db-1 pg_restore -U aiac_local --no-owner --no-privileges -d $verifyDb "/tmp/$dumpName"
        Assert-Native 'Isolated database restore'
        $restored = (& docker exec aiac-local-db-1 psql -U aiac_local -d $verifyDb -tAc 'SELECT count(*) FROM alphas').Trim()
        Assert-Native 'Restored Alpha count'
        if ([int]$restored -ne [int]$quiescentAlphaCount) { throw 'Restored Alpha count differs from the quiescent database.' }
    } finally { & docker exec aiac-local-db-1 dropdb -U aiac_local $verifyDb | Out-Null }

    & $python -m scripts.run_migrations --source $source --env-file $envFile
    Assert-Native 'Committed database migrations'
    $revisionAfter = (& docker exec aiac-local-db-1 psql -U aiac_local -d alpha_gpt_local -tAc 'SELECT version_num FROM alembic_version').Trim()
    Assert-Native 'Database revision check'
    $schemaChanged = $revisionAfter -ne $before.db_revision
    & $manager -Action Start -Source $source -Commit $Commit -AllowMaintenance | Out-Null
    Verify-Services $Commit
    & $python -m scripts.release_tasks verify --snapshot $snapshot | Out-Null
    Assert-Native 'Persistent state verification'
    & $python -m scripts.release_tasks resume --snapshot $snapshot
    Assert-Native 'Original run resume'
    & $python -m scripts.release_tasks verify --snapshot $snapshot | Out-Null
    Assert-Native 'Post-resume identity verification'
    Write-ReleaseRecord 'SUCCESS' 'Backup restored in isolation; health and original run identities verified.'
    [ordered]@{ commit = $Commit; source = $source; ci_run = $CiRun; record = $recordPath } |
        ConvertTo-Json | Set-Content -LiteralPath $currentFile -Encoding utf8
    $settled = $true
    Write-Output "DEPLOYED commit=$Commit ci_run=$CiRun record=$recordPath"
} catch {
    $detail = $_.Exception.Message
    if (-not $paused -and (Test-Path -LiteralPath $snapshot)) {
        $paused = $true
        $before = Get-Content -LiteralPath $snapshot -Raw | ConvertFrom-Json
    }
    if (-not $paused) {
        $settled = $true
        throw "Release blocked before task pause: $detail"
    }
    try {
        $currentRevision = (& docker exec aiac-local-db-1 psql -U aiac_local -d alpha_gpt_local -tAc 'SELECT version_num FROM alembic_version').Trim()
        if ($LASTEXITCODE -ne 0 -or $currentRevision -ne $before.db_revision) { $schemaChanged = $true }
    } catch { $schemaChanged = $true }
    if (-not $schemaChanged -and $previousSource) {
        try {
            & $manager -Action Stop -Services @('worker', 'api', 'ui') | Out-Null
            & $manager -Action Start -Source $previousSource -Commit $previousCommit -AllowMaintenance | Out-Null
            Verify-Services $previousCommit
            & $python -m scripts.release_tasks resume --snapshot $snapshot | Out-Null
            Assert-Native 'Rollback original run resume'
            Write-ReleaseRecord 'ROLLED_BACK' $detail
            [ordered]@{ commit = $previousCommit; source = $previousSource; record = $recordPath } |
                ConvertTo-Json | Set-Content -LiteralPath $currentFile -Encoding utf8
            $settled = $true
        } catch {
            Write-ReleaseRecord 'FAILED_NEEDS_OPERATOR' "$detail; rollback failed: $($_.Exception.Message)"
        }
    } else {
        & $manager -Action Stop -Services @('worker', 'api', 'ui') | Out-Null
        Write-ReleaseRecord 'FAILED_NEEDS_OPERATOR' "$detail; schema revision changed or previous source missing"
    }
    throw "Release failed: $detail. See $recordPath"
} finally {
    if ($settled -and (Test-Path -LiteralPath $maintenanceFile)) {
        Remove-Item -LiteralPath $maintenanceFile -Force
    }
    Pop-Location
}
