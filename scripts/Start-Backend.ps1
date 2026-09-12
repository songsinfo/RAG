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
& (Join-Path $PSScriptRoot 'Invoke-Maven.ps1') -Goals @('spring-boot:run')

