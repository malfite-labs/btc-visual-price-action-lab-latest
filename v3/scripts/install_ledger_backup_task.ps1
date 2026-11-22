$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Runner = Join-Path $ProjectRoot 'scripts\ledger_backup.py'
if (-not (Test-Path -LiteralPath $Python)) { throw "Project Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Runner)) { throw "Backup runner not found: $Runner" }
$TaskName = 'BTC Visual Price Action Lab - Verified Ledger Backup'
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
  $ExistingAction = $Existing.Actions | Select-Object -First 1
  if ($ExistingAction.Execute -ne $Python -or $ExistingAction.Arguments -notlike "*$Runner*") {
    throw 'BACKUP_TASK_NAME_CONFLICT'
  }
  Start-ScheduledTask -TaskName $TaskName
  $Existing | Select-Object TaskName, State
  Write-Output "LOGON_TYPE=$($Existing.Principal.LogonType)"
  exit 0
}
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Runner`" --once"
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$User = "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
try {
  Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description 'SQLite Online Backup with integrity and SHA256 verification; 15m rolling, daily and weekly retention.' -Force | Out-Null
} catch {
  # Some Windows policy profiles deny S4U registration.  Keep a working
  # interactive-user task and make the logon limitation visible in the result.
  $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description 'SQLite Online Backup with integrity and SHA256 verification; 15m rolling, daily and weekly retention.' -Force | Out-Null
  Write-Warning 'S4U was denied; task runs only while this user is logged on.'
}
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
