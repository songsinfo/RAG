param([switch]$Outage)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$secretFile = Join-Path $repositoryRoot '.local/langfuse-eval.env'
$python = Join-Path $repositoryRoot 'backend/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $secretFile)) { throw 'Run scripts/init_langfuse_local.ps1 first.' }
if (-not (Test-Path -LiteralPath $python)) { throw 'Install backend requirements in backend/.venv first.' }

# Invoke with pwsh -File: acceptance credentials stay in this child process.
Get-Content -LiteralPath $secretFile | ForEach-Object {
    $parts = $_ -split '=', 2
    if ($parts[0] -match '^(LANGFUSE_|RAG_LANGFUSE_)') {
        [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
    }
}
$env:RAG_LANGFUSE_PRIVATE_ACCEPTANCE = '1'
$env:RAG_LANGFUSE_FAULT_ACCEPTANCE = if ($Outage) { '1' } else { '0' }
Push-Location -LiteralPath $repositoryRoot
try {
    if ($Outage) {
        & docker compose --env-file .local/langfuse-eval.env -f deploy/langfuse.compose.yml stop web worker
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop isolated Langfuse services.' }
    }
    try {
        & $python -m pytest backend/tests/test_langfuse_private.py -q -p no:cacheprovider --tb=short --basetemp .local/pytest-langfuse-acceptance
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic Langfuse acceptance failed.' }
    } finally {
        if ($Outage) {
            & docker compose --env-file .local/langfuse-eval.env -f deploy/langfuse.compose.yml start web worker
            if ($LASTEXITCODE -ne 0) { throw 'Could not restore isolated Langfuse services.' }
        }
    }
} finally {
    Pop-Location
}
