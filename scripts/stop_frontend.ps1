$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'service_lifecycle.ps1')
Stop-V3Service 'frontend'
