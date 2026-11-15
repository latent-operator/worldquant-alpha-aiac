param([Parameter(ValueFromRemainingArguments = $true)][string[]] $ToolCommand)
$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot '../work/worldquant-alpha-aiac-goal-loop/scripts/goal-tools.ps1'
if (-not (Test-Path $entry)) { throw 'AIAC goal tooling worktree is unavailable; restore it before continuing.' }
& $entry @ToolCommand
exit $LASTEXITCODE
