param([switch]$SkipDatabase)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serviceRoot = Join-Path $projectRoot '.local-services'
if (-not $SkipDatabase) {
    & (Join-Path $PSScriptRoot 'start-postgres.ps1')
}
$modelFile = Join-Path $serviceRoot 'models\jina-embeddings-v3-Q8_0.gguf'
$serverFiles = @(Get-ChildItem -LiteralPath (Join-Path $serviceRoot 'llama') -Filter llama-server.exe -File -Recurse)
if ($serverFiles.Count -ne 1 -or -not (Test-Path -LiteralPath $modelFile)) {
    throw 'Embedding runtime or model is missing; run deploy/download_embedding.py first.'
}
$alreadyRunning = $false
try {
    $models = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/v1/models' -TimeoutSec 3
    $alreadyRunning = @($models.data | Where-Object id -EQ 'jina-embeddings-v3-Q8_0.gguf').Count -gt 0
    if (-not $alreadyRunning) { throw 'Port 8080 is used by a different model service.' }
} catch {
    if ($_.Exception.Message -eq 'Port 8080 is used by a different model service.') { throw }
}
if ($alreadyRunning) {
    Write-Output 'Jina embedding service is already running on 127.0.0.1:8080.'
    exit 0
}
$logDir = Join-Path $serviceRoot 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$serverArgs = @('--model', ('"' + $modelFile + '"'), '--alias', 'jina-embeddings-v3-Q8_0.gguf',
    '--embedding', '--pooling', 'mean', '--host', '127.0.0.1', '--port', '8080',
    '--ctx-size', '8192', '--batch-size', '8192', '--ubatch-size', '8192',
    '--parallel', '1', '--n-gpu-layers', '99', '--threads', '8')
$serverProcess = Start-Process -FilePath $serverFiles[0].FullName -ArgumentList $serverArgs `
    -WorkingDirectory $serverFiles[0].DirectoryName -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir 'embedding.stdout.log') `
    -RedirectStandardError (Join-Path $logDir 'embedding.stderr.log')
@{pid=$serverProcess.Id; executable=$serverFiles[0].FullName; startedAt=(Get-Date).ToString('o')} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $serviceRoot 'embedding-process.json') -Encoding UTF8
for ($attempt=0; $attempt -lt 50; $attempt++) {
    Start-Sleep -Milliseconds 1000
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) { throw "Embedding process exited. See $logDir\embedding.stderr.log" }
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 2
        if ($health.status -eq 'ok') {
            Write-Output "Jina embedding service ready on 127.0.0.1:8080 (PID $($serverProcess.Id))."
            exit 0
        }
    } catch { }
}
throw "Embedding service is still starting; check $logDir\embedding.stderr.log"
