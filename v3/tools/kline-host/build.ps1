param([Parameter(Mandatory=$true)][string]$PackageZip)
$ErrorActionPreference='Stop'
$expected='1C0DACAC84B70109B3FD42F90C53A0BA942ABC3A87590D7A4E0CCD86E4A06CF4'
if((Get-FileHash -Algorithm SHA256 -LiteralPath $PackageZip).Hash -ne $expected){throw 'WEBVIEW2_SDK_HASH_MISMATCH'}
$root=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$out=Join-Path $root '.run\kline-host';$sdk=Join-Path $out 'sdk'
New-Item -ItemType Directory -Force -Path $out | Out-Null
Expand-Archive -LiteralPath $PackageZip -DestinationPath $sdk -Force
Copy-Item -LiteralPath (Join-Path $sdk 'lib\net462\Microsoft.Web.WebView2.Core.dll') -Destination $out
Copy-Item -LiteralPath (Join-Path $sdk 'lib\net462\Microsoft.Web.WebView2.WinForms.dll') -Destination $out
Copy-Item -LiteralPath (Join-Path $sdk 'build\native\x64\WebView2Loader.dll') -Destination $out
$csc=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $csc /nologo /target:exe /platform:x64 ('/out:'+ (Join-Path $out 'KlineHost.exe')) /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ('/reference:'+ (Join-Path $out 'Microsoft.Web.WebView2.Core.dll')) ('/reference:'+ (Join-Path $out 'Microsoft.Web.WebView2.WinForms.dll')) (Join-Path $PSScriptRoot 'KlineHost.cs')
if($LASTEXITCODE -ne 0){throw 'KLINE_HOST_BUILD_FAILED'}
Write-Output (Join-Path $out 'KlineHost.exe')
