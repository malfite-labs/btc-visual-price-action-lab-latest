$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service_lifecycle.ps1')
# Stop the UI entry point before its API. Each service is identified and stopped independently.
Stop-V3Service 'frontend'
Stop-V3Service 'backend'
Write-Host 'BTC Paper Lab V3 paused. Paper positions and ledgers remain on disk.'
