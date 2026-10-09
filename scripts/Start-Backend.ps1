[CmdletBinding()]
param([ValidateSet('demo','evaluation','test')][string]$Environment='demo',[int]$Port=8080)
$ErrorActionPreference='Stop'
$ragRoot=[System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envFile=Join-Path $ragRoot ".runtime/$Environment.env"
if(-not(Test-Path -LiteralPath $envFile)){throw 'Initialize the selected isolated environment first.'}
& (Join-Path $PSScriptRoot 'Import-Environment.ps1') -Path $envFile
$env:PORT=$Port.ToString()
$env:RAG_ARTIFACT_ROOT=Join-Path $ragRoot ".runtime/$Environment/artifacts"
$env:RAG_IMPORT_ROOT=Join-Path $ragRoot ".runtime/$Environment/import"
$env:RAG_LOG_FILE=Join-Path $ragRoot ".runtime/$Environment/logs/backend.log"
$backendDir=Join-Path $ragRoot 'backend'
$env:PYTHONPATH=$backendDir
$venvPython=Join-Path $backendDir '.venv/Scripts/python.exe'
if(-not(Test-Path -LiteralPath $venvPython)){throw "Python venv not found at $venvPython. Run: python -m venv backend/.venv && backend/.venv/Scripts/pip install -e backend"}
& $venvPython -m uvicorn app.main:app --host 0.0.0.0 --port $Port
if($LASTEXITCODE -ne 0){throw "Backend failed with exit code $LASTEXITCODE"}
