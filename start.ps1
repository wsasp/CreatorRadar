#Requires -Version 5.1
param(
    [ValidateSet('production', 'acceptance')][string]$Workspace = 'production',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [ValidateRange(1, 65535)][int]$DevPort = 5173,
    [string]$DataDir = '',
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'
$backend = Join-Path $PSScriptRoot 'backend'
$web = Join-Path $PSScriptRoot 'web'
$services = [System.Collections.Generic.List[object]]::new()
$previousEnvironment = @{
    CREATOR_RADAR_WORKSPACE = $env:CREATOR_RADAR_WORKSPACE
    CREATOR_RADAR_PORT = $env:CREATOR_RADAR_PORT
    CREATOR_RADAR_DATA_DIR = $env:CREATOR_RADAR_DATA_DIR
    CREATOR_RADAR_DEV_PORT = $env:CREATOR_RADAR_DEV_PORT
    CREATOR_RADAR_CRAWLER_RUNTIME = $env:CREATOR_RADAR_CRAWLER_RUNTIME
    CREATOR_RADAR_CRAWLER_SETUP_FAILED = $env:CREATOR_RADAR_CRAWLER_SETUP_FAILED
    CREATOR_RADAR_ASR_PYTHON = $env:CREATOR_RADAR_ASR_PYTHON
}
function Assert-PortAvailable([int]$LocalPort, [string]$Role) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $LocalPort)
    $listener.ExclusiveAddressUse = $true
    try {
        $listener.Start()
    } catch [Net.Sockets.SocketException] {
        if ($_.Exception.SocketErrorCode -eq [Net.Sockets.SocketError]::AddressAlreadyInUse) {
            throw "$Role port $LocalPort is already in use. Stop the existing service or choose a different port."
        }
        throw "Cannot bind the $Role port $LocalPort on 127.0.0.1. $($_.Exception.Message)"
    } finally {
        $listener.Stop()
    }
}

function Start-LocalService([string]$Role, [string]$Executable, [string]$Directory, [string[]]$Arguments) {
    $errorLog = Join-Path $logs "$runId-$Role.err.log"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
        -WorkingDirectory $Directory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "$runId-$Role.out.log") `
        -RedirectStandardError $errorLog
    $services.Add([pscustomobject]@{ Role = $Role; Process = $process; ErrorLog = $errorLog })
    try {
        # Retain the process handle so .NET Framework can read its exit code after it exits.
        $null = $process.Handle
    } catch {
        if (-not $process.HasExited) { throw }
    }
}

function Invoke-StartupSetup([string]$Name, [string]$Description, [string[]]$Arguments) {
    $outputLog = Join-Path $logs "$runId-$Name-setup.log"
    $errorLog = Join-Path $logs "$runId-$Name-setup.err.log"
    Write-Host "$Description Setup log: $outputLog"
    $setup = Start-Process -FilePath $python -WorkingDirectory $backend -PassThru -WindowStyle Hidden -Wait `
        -ArgumentList (@('-X', 'utf8', '-u', '-m', 'creator_radar') + $Arguments) `
        -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
    try {
        if ($setup.ExitCode -eq 0) { return $true }
        Write-Warning "$Name setup failed. See $outputLog and $errorLog. Restart to retry."
        return $false
    } finally {
        $setup.Dispose()
    }
}

try {
    if ($Dev -and $Port -eq $DevPort) { throw 'The API and Vite ports must differ.' }
    Assert-PortAvailable $Port 'API'
    if ($Dev) { Assert-PortAvailable $DevPort 'Vite' }
    $env:CREATOR_RADAR_WORKSPACE = $Workspace
    $env:CREATOR_RADAR_PORT = "$Port"
    $env:CREATOR_RADAR_DEV_PORT = if ($Dev) { "$DevPort" } else { $null }
    if (-not $DataDir) { $DataDir = $env:CREATOR_RADAR_DATA_DIR }
    if ($DataDir) {
        $env:CREATOR_RADAR_DATA_DIR = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DataDir)
    }
    if ($env:CREATOR_RADAR_CRAWLER_RUNTIME) {
        $env:CREATOR_RADAR_CRAWLER_RUNTIME = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($env:CREATOR_RADAR_CRAWLER_RUNTIME)
    }
    if ($env:CREATOR_RADAR_ASR_PYTHON) {
        $env:CREATOR_RADAR_ASR_PYTHON = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($env:CREATOR_RADAR_ASR_PYTHON)
    }
    $env:CREATOR_RADAR_CRAWLER_SETUP_FAILED = $null

    Push-Location $backend
    try {
        uv sync --locked
        if ($LASTEXITCODE) { throw 'Backend dependency installation failed.' }
        $python = Join-Path $backend '.venv/Scripts/python.exe'
        & $python -X utf8 -m creator_radar migrate
        if ($LASTEXITCODE) { throw 'Database migration failed.' }
        $workspaceInfo = & $python -X utf8 -m creator_radar workspace-path --json
        if ($LASTEXITCODE) { throw 'Could not resolve the workspace directory.' }
        $workspacePath = ($workspaceInfo | ConvertFrom-Json).path
    } finally {
        Pop-Location
    }

    $logs = Join-Path $workspacePath 'service'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $runId = "$PID-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    if (-not (Invoke-StartupSetup 'crawler' 'Checking MediaCrawler (first startup may download Chromium).' @('setup-crawler', '--startup'))) {
        $env:CREATOR_RADAR_CRAWLER_SETUP_FAILED = 'true'
    }
    $null = Invoke-StartupSetup 'asr' 'Preparing faster-whisper small (first startup may download the model).' @('setup-asr')

    Push-Location $web
    try {
        npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE) { throw 'Frontend dependency installation failed.' }
        if (-not $Dev) {
            npm.cmd run build
            if ($LASTEXITCODE) { throw 'Frontend build failed.' }
        }
    } finally {
        Pop-Location
    }

    foreach ($role in @('api', 'worker')) {
        Start-LocalService $role $python $backend @('-X', 'utf8', '-u', '-m', 'creator_radar', $role)
    }
    if ($Dev) {
        $node = (Get-Command node.exe).Source
        Start-LocalService 'vite' $node $web @('node_modules/vite/bin/vite.js')
    }
    $browserPort = if ($Dev) { $DevPort } else { $Port }
    Write-Host "Workspace: http://127.0.0.1:$browserPort ($Workspace)"
    Write-Host "Workspace data: $workspacePath"
    Write-Host "Service logs: $logs"
    Write-Host 'Press Ctrl+C to stop the local services.'
    while ($true) {
        foreach ($service in $services) {
            $process = $service.Process
            if ($process.HasExited) {
                $exitCode = $process.ExitCode
                if ($null -eq $exitCode) { $exitCode = 'unavailable' }
                throw "$($service.Role) process $($process.Id) exited (code $exitCode). See $($service.ErrorLog)."
            }
        }
        Start-Sleep -Milliseconds 500
    }
} finally {
    foreach ($service in $services) {
        $process = $service.Process
        if (-not $process.HasExited) {
            # Windows PowerShell 5.1 only has Process.Kill() for a single process.
            Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
                -ArgumentList @('/PID', $process.Id, '/T', '/F') -WindowStyle Hidden -Wait
        }
        $process.Dispose()
    }
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
