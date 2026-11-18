# User-operated local HTTP-server update. Never starts/stops a trading worker.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serverScript = Join-Path $projectRoot 'web_panel.py'
$pythonExe = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Python 3.12 pythonw.exe was not found.' }
$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 1) { throw 'Unexpected panel listeners. No process was stopped.' }
if ($listeners.Count -eq 1) {
    try {
        $panelState = Invoke-RestMethod 'http://127.0.0.1:8765/api/state' -TimeoutSec 5
    } catch {
        throw 'Panel state could not be read. The server may still have an active Demo/Live worker. No process was stopped. Closing the browser does not stop the server. Use owner recovery; do not start another bot.'
    }
    if ($null -eq $panelState -or $null -eq $panelState.running -or $null -eq $panelState.action -or
        $null -eq $panelState.arena_unfinished -or $null -eq $panelState.exchange_unfinished) {
        throw 'Panel state is incomplete. No process was stopped; an unreadable state is not proof that trading is inactive.'
    }
    if ($panelState.running -or $panelState.action -ne 'new') {
        throw 'Trading is active or a cycle is unfinished. Finish/close that cycle first. No process was stopped.'
    }
    if ($panelState.arena_unfinished) {
        throw 'A PAPER arena season is unfinished. Stop or recover it in the arena first. No process was stopped.'
    }
    if ($panelState.exchange_unfinished) {
        throw 'An OKX Demo/Live run is unfinished. Close/reconcile it in the panel first. No process was stopped.'
    }
    $serverId = $listeners[0].OwningProcess
    $serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $serverId"
    $escapedScript = [regex]::Escape($serverScript)
    # Windows launchers may append whitespace after the final argument.
    $expected = '^("[^"\r\n]+\\pythonw?\.exe"|[^\s"\r\n]+\\pythonw?\.exe)\s+"?' + $escapedScript + '"?(\s+--(open|port\s+8765))*\s*$'
    if ($serverProcess.Name -notin @('python.exe', 'pythonw.exe') -or $serverProcess.CommandLine -notmatch $expected) {
        throw 'Port 8765 belongs to an unexpected process. No process was stopped.'
    }
    Stop-Process -Id $serverId
    Wait-Process -Id $serverId -Timeout 5 -ErrorAction SilentlyContinue
}
Start-Process -FilePath $pythonExe -ArgumentList ('"' + $serverScript + '" --open') -WorkingDirectory $projectRoot -WindowStyle Hidden
Write-Output 'Local panel opened. Trading starts only when you press Play.'
