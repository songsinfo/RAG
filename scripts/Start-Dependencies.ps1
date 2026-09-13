[CmdletBinding()]
param(
 [ValidateSet('demo','evaluation','test')][string]$Environment='demo',
 [switch]$IncludeDocuments
)
$ErrorActionPreference='Stop'
$ragRoot=[System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envFile=Join-Path $ragRoot ".runtime/$Environment.env"
if(-not (Test-Path -LiteralPath $envFile)){& (Join-Path $PSScriptRoot 'Initialize-Demo.ps1') -Environment $Environment}
$compose=Join-Path $ragRoot 'deploy/compose.yml'
$arguments=@('compose','--project-name',"rag-$Environment",'--env-file',$envFile,'-f',$compose)
if($IncludeDocuments){$arguments+=@('--profile','documents')}
$arguments+=@('up','-d','--wait','--wait-timeout','240')
& docker.exe @arguments
if($LASTEXITCODE -ne 0){throw "Project dependency startup failed: $LASTEXITCODE"}
