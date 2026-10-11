#requires -Version 7.2
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$directory = Join-Path $repositoryRoot '.local'
$target = Join-Path $directory 'langfuse-eval.env'
if (Test-Path -LiteralPath $target) { throw 'Local Langfuse secrets already exist; reuse that file.' }
New-Item -ItemType Directory -Path $directory -Force | Out-Null
function New-Secret {
    [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
}
$values = [ordered]@{
    LFD_POSTGRES_PASSWORD = New-Secret
    LFD_CLICKHOUSE_PASSWORD = New-Secret
    LFD_MINIO_PASSWORD = New-Secret
    LFD_REDIS_PASSWORD = New-Secret
    LFD_AUTH_SECRET = New-Secret
    LFD_SALT = New-Secret
    LFD_ENCRYPTION_KEY = New-Secret
    LFD_USER_PASSWORD = New-Secret
    LANGFUSE_PUBLIC_KEY = 'pk-lf-' + [Guid]::NewGuid().ToString()
    LANGFUSE_SECRET_KEY = 'sk-lf-' + (New-Secret)
    LANGFUSE_BASE_URL = 'http://127.0.0.1:3031'
    RAG_LANGFUSE_ID_HMAC_KEY = New-Secret
    RAG_LANGFUSE_ENABLED = 'true'
    RAG_LANGFUSE_SAMPLE_RATE = '1'
    RAG_LANGFUSE_TARGET_ID = 'local-synthetic'
}
$lines = foreach ($key in $values.Keys) { '{0}={1}' -f $key, $values[$key] }
[IO.File]::WriteAllLines($target, $lines, [Text.UTF8Encoding]::new($false))
Write-Output ('Generated local-only secrets: ' + $target)
