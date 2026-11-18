param([string]$Runtime)

$ErrorActionPreference = 'Stop'
$backend = Join-Path $PSScriptRoot '../../backend'
$runtimeArguments = @()
if (-not $Runtime) { $Runtime = $env:CREATOR_RADAR_CRAWLER_RUNTIME }
if ($Runtime) {
    $runtimeArguments = @('--runtime', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Runtime))
}
Push-Location $backend
try {
    uv run --locked python -X utf8 -m creator_radar setup-crawler @runtimeArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
