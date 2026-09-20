$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$serviceRoot = Join-Path $projectRoot '.local-services'
$stateFile = Join-Path $serviceRoot 'postgres-wsl-process.json'

# A systemd service alone does not keep a WSL distribution running. Keep one
# hidden WSL client attached so PostgreSQL remains available after this exits.
$running = $false
if (Test-Path -LiteralPath $stateFile) {
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $existing = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$state.pid) -ErrorAction SilentlyContinue
    $running = $null -ne $existing -and $existing.Name -eq 'wsl.exe' -and
        $existing.CommandLine -like '*Ubuntu-22.04*' -and $existing.CommandLine -like '*/bin/sleep infinity*'
}
if (-not $running) {
    $keeper = Start-Process -FilePath 'wsl.exe' `
        -ArgumentList @('-d', 'Ubuntu-22.04', '-u', 'root', '--', '/bin/sleep', 'infinity') `
        -WindowStyle Hidden -PassThru
    @{pid=$keeper.Id; distro='Ubuntu-22.04'; startedAt=(Get-Date).ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
}
& wsl.exe -d Ubuntu-22.04 -u root -- systemctl start postgresql@16-main
if ($LASTEXITCODE -ne 0) { throw 'Could not start PostgreSQL in WSL.' }
for ($attempt=0; $attempt -lt 15; $attempt++) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', 55432)
        Write-Output 'PostgreSQL is ready on 127.0.0.1:55432; hidden WSL keep-alive is active.'
        return
    } catch {
        Start-Sleep -Milliseconds 1000
    } finally {
        $client.Dispose()
    }
}
throw 'PostgreSQL did not become reachable on localhost:55432.'
