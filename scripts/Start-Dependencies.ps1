[CmdletBinding()]
param([switch]$IncludeDocuments)
$ErrorActionPreference='Stop'
$ragRoot=[System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envFile=Join-Path $ragRoot '.runtime/demo.env'
if(-not (Test-Path -LiteralPath $envFile)){& (Join-Path $PSScriptRoot 'Initialize-Demo.ps1')}
$compose=Join-Path $ragRoot 'deploy/compose.yml'
$arguments=@('compose','--env-file',$envFile,'-f',$compose)
if($IncludeDocuments){$arguments+=@('--profile','documents')}
$arguments+=@('up','-d','--wait','--wait-timeout','240')
& docker.exe @arguments
if($LASTEXITCODE -ne 0){throw "Project dependency startup failed: $LASTEXITCODE"}

