$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$modePath = Join-Path $root 'data\formal-mode.json'
if (-not (Test-Path -LiteralPath $modePath) -or (Get-Content -LiteralPath $modePath -Raw | ConvertFrom-Json).mode -ne 'RULE_DRIVEN') { throw 'FORMAL_MODE_NOT_RULE_DRIVEN' }
$env:PAPER_AUTOMATION_ENABLED = 'true'
$env:VISION_ENABLED = 'false'
$env:VISION_CALLS_AUTHORIZED = 'false'
$env:VISION_MAX_CALLS_PER_UTC_DAY = '0'
$env:VISION_MAX_CALL_COST_USD = '0'
$env:VISION_DAILY_BUDGET_USD = '0'
& (Join-Path $PSScriptRoot 'start_lab.ps1')
