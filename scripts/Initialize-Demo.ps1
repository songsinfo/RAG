[CmdletBinding()]
param([ValidateSet('demo','evaluation','test')][string]$Environment='demo')
$ErrorActionPreference='Stop'
$ragRoot=[System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtime=Join-Path $ragRoot '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$envPath=Join-Path $runtime "$Environment.env"
if(Test-Path -LiteralPath $envPath){ Write-Output "Already initialized: $envPath"; return }
function New-Secret { [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24)).ToLowerInvariant() }
$content=Get-Content -LiteralPath (Join-Path $ragRoot '.env.example') -Raw
$values=@{RAG_ENVIRONMENT=$Environment;DB_PASSWORD=(New-Secret);MYSQL_ROOT_PASSWORD=(New-Secret);RAG_SEED_PASSWORD=(New-Secret);RAG_CSRF_SECRET=(New-Secret);DOCLING_API_KEY=(New-Secret);RAG_INDEX_PREFIX="rag_$Environment"}
foreach($key in $values.Keys){ $content=[regex]::Replace($content,"(?m)^$key=.*$", "$key=$($values[$key])") }
$content=$content.Replace('/rag_demo?',"/rag_$Environment?")
[System.IO.File]::WriteAllText($envPath,$content,[System.Text.UTF8Encoding]::new($false))
Write-Output "Created private configuration: $envPath"
Write-Output 'Demo account password is RAG_SEED_PASSWORD in that file; no credentials were printed.'

