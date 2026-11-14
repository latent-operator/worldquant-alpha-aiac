param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ToolCommand
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    if ($ToolCommand.Count -eq 0 -or $ToolCommand[0] -eq 'resume') {
        Get-Content (Join-Path $root 'openspec/specs/aiac-research-goal/spec.md')
        & $PSCommandPath bd prime
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $PSCommandPath bd ready
        exit $LASTEXITCODE
    }
    if ($ToolCommand[0] -eq 'close-goal') {
        $python = Join-Path $root '.venv/Scripts/python.exe'
        if (-not (Test-Path $python)) { $python = 'python' }
        & $python -m scripts.close_research_goal @($ToolCommand | Select-Object -Skip 1)
        exit $LASTEXITCODE
    }
    if ($ToolCommand[0] -eq 'bd' -and $ToolCommand -contains 'aiac-r1e' -and
        ($ToolCommand -contains 'close' -or $ToolCommand -contains 'update' -or $ToolCommand -contains 'delete')) {
        throw 'Use close-goal <report> --receipt <path> --session-id <id> for research goal acceptance. Ordinary child tasks remain editable.'
    }
    $gitCommon = (git rev-parse --git-common-dir).Trim()
    $gitDir = (git rev-parse --absolute-git-dir).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve Git worktree metadata' }
    $env:AIAC_GIT_COMMON_DIR = (Resolve-Path $gitCommon).Path
    $resolvedGitDir = (Resolve-Path $gitDir).Path
    if ($resolvedGitDir -eq $env:AIAC_GIT_COMMON_DIR) {
        $env:AIAC_CONTAINER_GIT_DIR = '/git'
    } else {
        $env:AIAC_CONTAINER_GIT_DIR = '/git/worktrees/' + (Split-Path $resolvedGitDir -Leaf)
    }
    if ($ToolCommand.Count -eq 0) {
        docker compose -f compose.goal-tools.yaml run --rm goal-tools
    } else {
        docker compose -f compose.goal-tools.yaml run --rm goal-tools @ToolCommand
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
