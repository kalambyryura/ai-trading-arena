$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $panelSnapshot = Invoke-RestMethod 'http://127.0.0.1:8765/api/state' -TimeoutSec 20
    $exchangeSnapshot = Invoke-RestMethod 'http://127.0.0.1:8765/api/exchange/state' -TimeoutSec 20
    if ($null -eq $panelSnapshot -or $null -eq $panelSnapshot.running -or $null -eq $panelSnapshot.action -or
        $null -eq $panelSnapshot.arena_unfinished -or $null -eq $panelSnapshot.exchange_unfinished -or
        $null -eq $exchangeSnapshot -or $null -eq $exchangeSnapshot.active) {
        throw 'Panel state is incomplete; no process was stopped.'
    }
    if ($panelSnapshot.running -or $panelSnapshot.action -ne 'new' -or $panelSnapshot.arena_unfinished -or $panelSnapshot.exchange_unfinished -or
        @($exchangeSnapshot.active.psobject.Properties | Where-Object { $null -ne $_.Value }).Count -gt 0) {
        throw 'A battle or recovery is still active. Finish it in the panel before moving to the server.'
    }
    $ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ownerIds.Count -ne 1) { throw 'Cannot identify the idle local panel.' }
    $panelProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $ownerIds[0])
    $expectedScript = [Regex]::Escape((Join-Path $taskRoot 'web_panel.py'))
    $serverScript = [Regex]::Escape((Join-Path $taskRoot 'deployment\server\run_server.py'))
    # Match only the exact workspace entrypoints and flags emitted by our launchers.
    # START_WINDOWS_PANEL uses -B -X utf8; the scheduled task uses -u.
    $pythonPrefix = '^("[^"\r\n]+\\pythonw?\.exe"|[^\s"\r\n]+\\pythonw?\.exe)\s+(?:(?:-B|-u|-X\s+utf8)\s+)*'
    $expectedCommand = $pythonPrefix + '(?:"?' + $expectedScript + '"?(\s+--(open|port\s+8765))*|"?' + $serverScript + '"?\s+serve(?:\s+--port\s+8765)?)\s*$'
    if ($panelProcess.Name -notin @('python.exe', 'pythonw.exe') -or $panelProcess.CommandLine -notmatch $expectedCommand) {
        throw 'The listener is not this workspace panel; no process was stopped.'
    }
    # Only the owner's explicit export action reaches this idle, identified process.
    Stop-Process -Id $ownerIds[0] -ErrorAction Stop
    Wait-Process -Id $ownerIds[0] -Timeout 5 -ErrorAction SilentlyContinue
}
& py -3.12 (Join-Path $taskRoot 'tools\server_transfer.py') export --source $taskRoot --output (Join-Path $taskRoot 'JevArena-state.private.zip') --include-credentials
if ($LASTEXITCODE -ne 0) { throw 'State export failed; no server may start from an incomplete transfer.' }
Write-Host 'Copy JevArena-state.private.zip to your Windows Server. It contains private API files; do not send it to chat.'
