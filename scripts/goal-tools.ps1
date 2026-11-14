param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ToolCommand
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
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
