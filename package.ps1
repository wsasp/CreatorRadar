#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
& uv run --no-project --managed-python --python 3.12 python (Join-Path $PSScriptRoot 'packaging/build.py') @args
exit $LASTEXITCODE
