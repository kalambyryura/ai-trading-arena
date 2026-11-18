param([string]$PythonExecutable = '')
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$systemInfo = Get-CimInstance Win32_OperatingSystem
if ($systemInfo.ProductType -notin @(1, 2, 3) -or
    ($systemInfo.ProductType -eq 1 -and ([Version]$systemInfo.Version).Major -lt 10)) {
    throw 'Use Windows 10/11 or Windows Server.'
}
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipalCheck = New-Object Security.Principal.WindowsPrincipal($taskIdentity)
if (-not $taskPrincipalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run INSTALL_WINDOWS_SERVER.cmd as administrator.'
}
if (-not $PythonExecutable) {
    $PythonExecutable = (& py -3.12 -c 'import sys; print(sys.executable)').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 for all users first.' }
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
$entryPoint = Join-Path $taskRoot 'deployment\server\run_server.py'
& $PythonExecutable $entryPoint check
if ($LASTEXITCODE -ne 0) { throw 'Import the complete state before installing the task.' }
$taskName = 'Jev Trading Arena'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) { throw 'A Jev task already exists. Do not overwrite a running installation.' }
$taskArguments = '-u "' + $entryPoint + '" serve --port 8765'
$taskAction = New-ScheduledTaskAction -Execute $PythonExecutable -Argument $taskArguments -WorkingDirectory $taskRoot
$taskTrigger = New-ScheduledTaskTrigger -AtStartup
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# SYSTEM and administrators retain access to the private state and credentials.
& icacls $taskRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*' + $taskIdentity.User.Value + ':(OI)(CI)F') | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the server folder.' }
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Local owner UI; never starts a trading battle automatically.' | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host 'Panel task installed. Open http://127.0.0.1:8765/arena on this Windows computer.'
Write-Host 'Keep this computer awake. Start each battle yourself in the panel; RDP can be disconnected.'
