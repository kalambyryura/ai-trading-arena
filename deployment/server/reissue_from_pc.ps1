$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$priorArchive = Join-Path $taskRoot 'JevArena-state.private.zip'
if (-not (Test-Path -LiteralPath $priorArchive -PathType Leaf)) {
    throw 'Prior private archive missing. Keep the handoff marker and consult START_HERE_LIVE_UA.md.'
}
Write-Host 'This prepares a NEW private archive. It does not start or stop trading.'
Write-Host 'Continue ONLY if this PC has the authoritative history and ALL previous target installations are stopped.'
Write-Host 'Close the source panel first. Do not delete any handoff marker, old ZIP or ledger.'
$ownerStatement = Read-Host 'Type SOURCE-ONLY to state both facts; anything else cancels'
if ($ownerStatement -cne 'SOURCE-ONLY') { throw 'Cancelled; no archive or marker changed.' }
$exportStamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$nextArchive = Join-Path $taskRoot ('JevArena-state-' + $exportStamp + '.private.zip')
& py -3.12 -B -X utf8 (Join-Path $taskRoot 'tools\server_transfer.py') export --source $taskRoot --output $nextArchive --include-credentials --reissue-from $priorArchive --source-authoritative --all-targets-stopped
if ($LASTEXITCODE -ne 0) { throw 'Export refused. Preserve all files and the exact error; do not start the target.' }
Write-Host 'Copy the NEW private ZIP with the matching application ZIP to your own computer. Never send the private ZIP to chat.'
Write-Host $nextArchive
