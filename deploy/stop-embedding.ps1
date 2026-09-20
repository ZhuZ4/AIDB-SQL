$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$recordPath = Join-Path $projectRoot '.local-services\embedding-process.json'
if (-not (Test-Path -LiteralPath $recordPath)) {
    Write-Output 'No embedding process record exists.'
    exit 0
}
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
$serviceProcess = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
if ($serviceProcess) {
    if ($serviceProcess.Path -ne $record.executable) { throw 'PID belongs to a different executable; leaving it running.' }
    Stop-Process -Id $serviceProcess.Id
    Write-Output 'Project embedding process stopped.'
} else {
    Write-Output 'Project embedding process is already stopped.'
}
