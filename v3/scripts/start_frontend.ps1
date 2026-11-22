$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service_lifecycle.ps1')
Start-V3Service 'frontend'
