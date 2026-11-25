$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service_lifecycle.ps1')
Start-V3Service 'backend'
Start-V3Service 'frontend'
Write-Host 'BTC Paper Lab V3 services are available at http://127.0.0.1:5175'
