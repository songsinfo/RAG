[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [string]$DatasetVersionId = '',
    [string]$SnapshotId = '',
    [string]$DatasetPath = '',
    [string]$DatasetName = 'enterprise-golden-dataset',
    [switch]$ApprovedSyntheticOrRedacted,
    [ValidateSet('DEV', 'TEST')]
    [string]$Split = 'TEST',
    [double]$MaxCostCny = 10,
    [int]$TimeoutSeconds = 7200,
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../experiments/retrieval-ablation.json'),
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Read-AblationConfigs {
    param([string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $configs = @(Get-Content -Raw -LiteralPath $resolved | ConvertFrom-Json -Depth 30)
    Assert-True ($configs.Count -eq 4) 'Ablation matrix must contain exactly B0-B3.'
    Assert-True (($configs.id -join ',') -eq 'B0,B1,B2,B3') 'Ablation IDs must be ordered B0,B1,B2,B3.'
    Assert-True ($configs[0].payload.retrieval.mode -eq 'BM25') 'B0 must use BM25.'
    Assert-True ($configs[1].payload.retrieval.mode -eq 'DENSE') 'B1 must use Dense retrieval.'
    Assert-True ($configs[2].payload.retrieval.mode -eq 'HYBRID') 'B2 must use Hybrid retrieval.'
    Assert-True ($configs[3].payload.retrieval.mode -eq 'HYBRID') 'B3 must use Hybrid retrieval.'
    Assert-True (-not [bool]$configs[2].payload.post.rerank) 'B2 must keep reranking disabled.'
    Assert-True ([bool]$configs[3].payload.post.rerank) 'B3 must enable reranking.'
    foreach ($config in $configs) {
        Assert-True (-not [bool]$config.payload.query.rewrite) "$($config.id) unexpectedly enables rewrite."
        Assert-True (-not [bool]$config.payload.query.hyde) "$($config.id) unexpectedly enables HyDE."
        Assert-True (-not [bool]$config.payload.query.multiQuery) "$($config.id) unexpectedly enables multi-query."
        Assert-True (($config.payload.retrieval.representations -join ',') -eq 'ORIGINAL') "$($config.id) must use only ORIGINAL vectors."
        Assert-True ([int]$config.payload.retrieval.perRouteK -eq 50) "$($config.id) must use perRouteK=50."
        Assert-True ([int]$config.payload.retrieval.numCandidates -eq 200) "$($config.id) must use numCandidates=200."
        Assert-True ([int]$config.payload.fusion.rrfK -eq 60) "$($config.id) must use rrfK=60."
        Assert-True ([int]$config.payload.post.rerankTopN -eq 30) "$($config.id) must use rerankTopN=30."
        Assert-True (-not [bool]$config.payload.post.mmr) "$($config.id) unexpectedly enables MMR."
        Assert-True (-not [bool]$config.payload.post.parent) "$($config.id) unexpectedly enables parent expansion."
    }
    $configs
}

$configs = Read-AblationConfigs $ConfigPath
if ($SelfTest) {
    Write-Output 'Self-test passed: B0-B3 are complete and hold non-ablation parameters fixed.'
    return
}

Assert-True ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) 'Use Windows native PowerShell.'
$provisionDataset = -not [string]::IsNullOrWhiteSpace($DatasetPath)
if ($provisionDataset) {
    Assert-True ([string]::IsNullOrWhiteSpace($DatasetVersionId) -and [string]::IsNullOrWhiteSpace($SnapshotId)) 'Use DatasetPath or existing DatasetVersionId/SnapshotId, not both.'
    Assert-True $ApprovedSyntheticOrRedacted 'DatasetPath requires -ApprovedSyntheticOrRedacted.'
} else {
    Assert-True (-not [string]::IsNullOrWhiteSpace($DatasetVersionId)) 'DatasetVersionId is required.'
    Assert-True (-not [string]::IsNullOrWhiteSpace($SnapshotId)) 'SnapshotId is required.'
}
Assert-True ($MaxCostCny -gt 0 -and $MaxCostCny -le 10) 'MaxCostCny must be greater than 0 and at most 10.'
Assert-True ($TimeoutSeconds -ge 60) 'TimeoutSeconds must be at least 60.'

$ragRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envPath = Join-Path $ragRoot '.runtime/evaluation.env'
Assert-True (Test-Path -LiteralPath $envPath -PathType Leaf) "Environment file not found: $envPath"
$seedPassword = ''
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^RAG_SEED_PASSWORD=(.*)$') { $seedPassword = $Matches[1].Trim() }
}
Assert-True (-not [string]::IsNullOrWhiteSpace($seedPassword)) "RAG_SEED_PASSWORD is missing from $envPath"

function Invoke-Api {
    param(
        [ValidateSet('GET', 'POST')]
        [string]$Method,
        [string]$Path,
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [hashtable]$Headers = @{},
        [AllowNull()][object]$Body = $null
    )
    $arguments = @{
        Uri = "$BaseUrl$Path"
        Method = $Method
        WebSession = $Session
        Headers = $Headers
        SkipHttpErrorCheck = $true
        TimeoutSec = 60
    }
    if ($null -ne $Body) {
        $arguments.Body = $Body | ConvertTo-Json -Depth 30 -Compress
        $arguments.ContentType = 'application/json'
    }
    $response = Invoke-WebRequest @arguments
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($response.Content)) {
        try { $parsed = $response.Content | ConvertFrom-Json -Depth 50 }
        catch { throw "$Method $Path returned non-JSON content with HTTP $([int]$response.StatusCode)." }
    }
    if ([int]$response.StatusCode -lt 200 -or [int]$response.StatusCode -ge 300) {
        $code = if ($null -ne $parsed -and $null -ne $parsed.code) { [string]$parsed.code } else { 'UNKNOWN' }
        throw "$Method $Path failed with HTTP $([int]$response.StatusCode), code $code."
    }
    $parsed
}

$session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
$csrf = Invoke-Api 'GET' '/api/v1/auth/csrf' $session
$headers = @{ 'X-CSRF-Token' = [string]$csrf.csrfToken }
$login = Invoke-Api 'POST' '/api/v1/auth/login' $session $headers @{
    tenantCode = 'evaluation'
    loginName = 'admin'
    password = $seedPassword
}
Assert-True ([bool]$login.authenticated) 'Evaluation admin login failed.'
$csrf = Invoke-Api 'GET' '/api/v1/auth/csrf' $session
$headers = @{ 'X-CSRF-Token' = [string]$csrf.csrfToken }

$executionId = [guid]::NewGuid().ToString('N')
$startedAt = [DateTimeOffset]::UtcNow
if ($provisionDataset) {
    $resolvedDatasetPath = (Resolve-Path -LiteralPath $DatasetPath).Path
    & (Join-Path $PSScriptRoot 'Test-EvaluationDataset.ps1') -Path $resolvedDatasetPath -RequireRuntimeBindings | Out-Null
    $cases = @(Get-Content -LiteralPath $resolvedDatasetPath | ForEach-Object { $_ | ConvertFrom-Json -Depth 50 })
    $createdDataset = Invoke-Api 'POST' '/api/v1/admin/eval/datasets' $session $headers @{ name = $DatasetName }
    $DatasetVersionId = [string]$createdDataset.id
    $null = Invoke-Api 'POST' "/api/v1/admin/eval/dataset-versions/$DatasetVersionId/cases" $session $headers @{ cases = $cases }
    $null = Invoke-Api 'POST' "/api/v1/admin/eval/dataset-versions/$DatasetVersionId/freeze" $session $headers @{}
    $documentIds = @($cases | ForEach-Object { $_.expected.evidenceGroups } | ForEach-Object { $_.alternatives } | ForEach-Object { $_.documentId } | Sort-Object -Unique)
    Assert-True ($documentIds.Count -gt 0) 'The bound dataset has no evidence documents for a snapshot.'
    $snapshot = Invoke-Api 'POST' '/api/v1/admin/eval/snapshots' $session $headers @{
        name = "$DatasetName-$executionId"
        documentIds = $documentIds
        approvedSyntheticOrRedacted = $true
    }
    $SnapshotId = [string]$snapshot.snapshotId
}
$runs = @()
foreach ($experiment in $configs) {
    Write-Output "Starting $($experiment.id): $($experiment.name)"
    $created = Invoke-Api 'POST' '/api/v1/admin/configs' $session $headers @{
        kind = 'RETRIEVAL'
        name = [string]$experiment.name
        payload = $experiment.payload
    }
    $configId = [string]$created.id
    $null = Invoke-Api 'POST' "/api/v1/admin/configs/$configId/publish" $session $headers @{}
    $request = @{
        datasetVersionId = $DatasetVersionId
        snapshotId = $SnapshotId
        configId = $configId
        split = $Split
        repeats = 1
        mode = 'RAG'
        strict = $true
    }
    $estimate = Invoke-Api 'POST' '/api/v1/admin/eval/estimates' $session $headers $request
    Assert-True ($null -ne $estimate.estimatedCostCny) "$($experiment.id) has unknown model pricing."
    Assert-True ([double]$estimate.estimatedCostCny -le $MaxCostCny) "$($experiment.id) estimate exceeds MaxCostCny."
    $request.estimateId = [string]$estimate.estimateId
    $request.maxCostCny = $MaxCostCny
    $runHeaders = @{
        'X-CSRF-Token' = [string]$headers['X-CSRF-Token']
        'Idempotency-Key' = "ablation-$executionId-$($experiment.id)"
    }
    $started = Invoke-Api 'POST' '/api/v1/admin/eval/runs' $session $runHeaders $request
    $runId = [string]$started.runId
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 2
        $run = Invoke-Api 'GET' "/api/v1/admin/eval/runs/$runId" $session
        if ([DateTimeOffset]::UtcNow -ge $deadline) { throw "$($experiment.id) timed out after $TimeoutSeconds seconds." }
    } while ($run.status -in @('QUEUED', 'RUNNING'))
    $redacted = Invoke-Api 'POST' "/api/v1/admin/eval/runs/$runId/exports" $session $headers @{ mode = 'REDACTED' }
    $runs += [pscustomobject][ordered]@{
        experimentId = [string]$experiment.id
        configId = $configId
        runId = $runId
        status = [string]$run.status
        estimatedCostCny = $estimate.estimatedCostCny
        metrics = $run.metrics
        redactedExport = $redacted
    }
}

$runIds = $runs.runId -join ','
$comparison = Invoke-Api 'GET' "/api/v1/admin/eval/comparisons?runIds=$runIds" $session
$baseline = $runs[0].metrics
$summary = foreach ($run in $runs) {
    [pscustomobject][ordered]@{
        experimentId = $run.experimentId
        runId = $run.runId
        status = $run.status
        hitAt5 = $run.metrics.hitAt5
        recallAt5 = $run.metrics.recallAt5
        mrr = $run.metrics.mrr
        ndcgAt5 = $run.metrics.ndcgAt5
        deltaRecallAt5FromB0 = if ($null -ne $run.metrics.recallAt5 -and $null -ne $baseline.recallAt5) { [double]$run.metrics.recallAt5 - [double]$baseline.recallAt5 } else { $null }
        deltaNdcgAt5FromB0 = if ($null -ne $run.metrics.ndcgAt5 -and $null -ne $baseline.ndcgAt5) { [double]$run.metrics.ndcgAt5 - [double]$baseline.ndcgAt5 } else { $null }
        taskSuccessRate = $run.metrics.taskSuccessRate
        knownCostCny = $run.metrics.knownCostCny
        observedP95Ms = $run.metrics.observedP95Ms
    }
}
$eligible = @($summary | Where-Object { $_.status -eq 'COMPLETED' -and $null -ne $_.ndcgAt5 })
$selected = if ($eligible.Count -gt 0) {
    $eligible | Sort-Object @{ Expression = { [double]$_.ndcgAt5 }; Descending = $true }, @{ Expression = { [double]$_.recallAt5 }; Descending = $true }, @{ Expression = { [double]$_.knownCostCny }; Descending = $false } | Select-Object -First 1
} else { $null }

$report = [ordered]@{
    schemaVersion = 1
    executionId = $executionId
    startedAt = $startedAt.ToString('o')
    finishedAt = [DateTimeOffset]::UtcNow.ToString('o')
    datasetVersionId = $DatasetVersionId
    snapshotId = $SnapshotId
    split = $Split
    mode = 'RAG'
    comparable = [bool]$comparison.comparable
    comparisonReason = [string]$comparison.reason
    selectionRule = 'highest ndcgAt5, then recallAt5, then lower knownCostCny'
    selectedExperimentId = if ($null -ne $selected) { $selected.experimentId } else { $null }
    summary = @($summary)
    runs = @($runs)
}
$reportDir = Join-Path $ragRoot '.runtime/reports'
[IO.Directory]::CreateDirectory($reportDir) | Out-Null
$reportPath = Join-Path $reportDir "retrieval-ablation-$executionId.json"
[IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json -Depth 50), [Text.UTF8Encoding]::new($false))

if (-not [bool]$comparison.comparable) { throw "Runs are not comparable. Diagnostic report: $reportPath" }
$incomplete = @($runs | Where-Object { $_.status -ne 'COMPLETED' })
if ($incomplete.Count -gt 0) { throw "At least one run was incomplete. Diagnostic report: $reportPath" }
Write-Output "Ablation completed. Selected $($selected.experimentId). Report: $reportPath"
