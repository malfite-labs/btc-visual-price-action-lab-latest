$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
# No key is stored here. The existing local .env supplies only the DeepSeek key.
$env:PAPER_AUTOMATION_ENABLED = 'true'
$env:VISION_ENABLED = 'true'
$env:VISION_CALLS_AUTHORIZED = 'true'
$env:VISION_BASE_URL = 'https://api.deepseek.com'
$env:VISION_ENDPOINT_PATH = '/chat/completions'
$env:VISION_MODEL = 'deepseek-flash'
$env:VISION_THINKING_MODE = 'disabled'
$env:VISION_PROMPT_VARIANT = 'wait_example'
$env:VISION_MAX_CONCURRENCY = '1'
$env:VISION_TIMEOUT_SECONDS = '60'
$env:VISION_MAX_OUTPUT_TOKENS = '1000'
$env:VISION_MAX_CALLS_PER_UTC_DAY = '200'
$env:VISION_MAX_CALL_COST_USD = '0.05'
$env:VISION_DAILY_BUDGET_USD = '5'
$env:VISION_TOTAL_BUDGET_USD = '5'
# Verified DeepSeek Flash peak rates at launch; all input is treated as a cache miss.
$env:VISION_INPUT_PRICE_PER_MILLION_USD = '0.30'
$env:VISION_OUTPUT_PRICE_PER_MILLION_USD = '1.20'
$env:VISION_BUDGET_DB = Join-Path $root 'data\vision-ab-paper-budget.sqlite3'
& (Join-Path $PSScriptRoot 'start_lab.ps1')
