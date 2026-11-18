$ErrorActionPreference = 'Stop'
$environment = Join-Path $PSScriptRoot '../../.phase1/asr-env'
$python = Join-Path $environment 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    uv venv --python 3.12 $environment
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
uv pip sync --python $python --extra-index-url https://download.pytorch.org/whl/cpu `
    (Join-Path $PSScriptRoot 'processing-windows.lock')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -X utf8 -B (Join-Path $PSScriptRoot 'processing.py') environment
exit $LASTEXITCODE
