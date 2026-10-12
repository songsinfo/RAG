param([Parameter(Mandatory=$true)][ValidateRange(0.01,5.0)][double]$MaxCostCny)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'backend/.venv/Scripts/python.exe'
$env:RAG_REAL_ASSERTION_ACCEPTANCE = '1'
$env:RAG_REAL_ASSERTION_MAX_COST_CNY = $MaxCostCny.ToString([Globalization.CultureInfo]::InvariantCulture)
Push-Location -LiteralPath $repositoryRoot
try {
    & $python -m pytest backend/tests/test_real_assertion_acceptance.py -q -p no:cacheprovider --tb=short --basetemp .local/pytest-real-assertion
    if ($LASTEXITCODE -ne 0) { throw 'Independent assertion experiment failed. Inspect accounting before another paid run.' }
} finally {
    Pop-Location
}
