[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [string]$RepoBase = 'D:\git_xyy_repos\product-engineering-accelerator',
    [string]$BindingsPath = (Join-Path $PSScriptRoot '../.runtime/evaluation-bindings.json'),
    [string]$EnvPath = (Join-Path $PSScriptRoot '../.runtime/evaluation.env')
)

$ErrorActionPreference = 'Stop'

function Invoke-Api {
    param(
        [ValidateSet('GET', 'POST', 'PUT', 'DELETE')]
        [string]$Method,
        [string]$Path,
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [hashtable]$Headers = @{},
        [AllowNull()][object]$Body = $null,
        [AllowNull()][hashtable]$Form = $null
    )
    $invokeArgs = @{
        Uri = "$BaseUrl$Path"
        Method = $Method
        WebSession = $Session
        Headers = $Headers
        SkipHttpErrorCheck = $true
        TimeoutSec = 60
    }
    if ($null -ne $Form) {
        $invokeArgs.Form = $Form
    } elseif ($null -ne $Body) {
        $invokeArgs.Body = ($Body | ConvertTo-Json -Depth 30 -Compress)
        $invokeArgs.ContentType = 'application/json'
    }
    $response = Invoke-WebRequest @invokeArgs
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($response.Content)) {
        try {
            $parsed = $response.Content | ConvertFrom-Json -Depth 50
        } catch {
            throw "$Method $Path returned non-JSON content with HTTP $([int]$response.StatusCode): $($response.Content)"
        }
    }
    [pscustomobject]@{ Status = [int]$response.StatusCode; Body = $parsed }
}

function New-Session {
    param([string]$LoginName, [string]$Password)
    $session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $csrf = Invoke-Api 'GET' '/api/v1/auth/csrf' $session
    if ($csrf.Status -ne 200) { throw "Failed to get CSRF token: $($csrf.Status)" }
    $headers = @{ 'X-CSRF-Token' = [string]$csrf.Body.csrfToken }
    $login = Invoke-Api 'POST' '/api/v1/auth/login' $session $headers @{
        tenantCode = 'evaluation'
        loginName = $LoginName
        password = $Password
    }
    if ($login.Status -ne 200 -or -not [bool]$login.Body.authenticated) {
        throw "Failed to login as ${LoginName}: $($login.Status)"
    }
    $fresh = Invoke-Api 'GET' '/api/v1/auth/csrf' $session
    [pscustomobject]@{
        Session = $session
        Headers = @{ 'X-CSRF-Token' = [string]$fresh.Body.csrfToken }
    }
}

function Write-Headers {
    param([object]$Auth)
    @{
        'X-CSRF-Token' = [string]$Auth.Headers['X-CSRF-Token']
        'Idempotency-Key' = [guid]::NewGuid().ToString()
    }
}

Write-Output "=== Starting Import of 18 Mall Knowledge Sources ==="

# Read seed password
$seedPassword = ''
foreach ($line in Get-Content -LiteralPath $EnvPath) {
    if ($line -match '^RAG_SEED_PASSWORD=(.*)$') { $seedPassword = $Matches[1].Trim() }
}
if (-not $seedPassword) { throw "RAG_SEED_PASSWORD not found in $EnvPath" }

# Login admin and ops
$admin = New-Session 'admin' $seedPassword
$ops = New-Session 'ops' $seedPassword
$adminMe = (Invoke-Api 'GET' '/api/v1/me' $admin.Session).Body
$opsMe = (Invoke-Api 'GET' '/api/v1/me' $ops.Session).Body

# Create knowledge base for Mall evaluation
$kbName = "mall-evaluation-kb"
$kbRes = Invoke-Api 'POST' '/api/v1/admin/knowledge-bases' $admin.Session (Write-Headers $admin) @{
    name = $kbName
    readers = @(
        @{ type = 'USER'; userId = [string]$adminMe.userId },
        @{ type = 'USER'; userId = [string]$opsMe.userId }
    )
}
if ($kbRes.Status -ne 200) { throw "Failed to create knowledge base: $($kbRes.Status)" }
$kbId = [string]$kbRes.Body.id
Write-Output "Knowledge base created: $kbId ($kbName)"

# Read bindings file to get 18 sources
$bindings = Get-Content -Raw -LiteralPath $BindingsPath | ConvertFrom-Json -AsHashtable -Depth 30
$sources = $bindings['sources']

$buildConfigJson = @{
    embedding = $true
    summary = $false
    questions = $false
    vision = $false
    childCodePoints = 900
    parentCodePoints = 3000
} | ConvertTo-Json -Compress

$results = [ordered]@{}
$pendingJobs = @()

foreach ($relPath in $sources.Keys) {
    $fullPath = Join-Path $RepoBase ($relPath.Replace('/', [IO.Path]::DirectorySeparatorChar))
    if (-not (Test-Path -LiteralPath $fullPath)) {
        throw "Source file not found: $fullPath"
    }
    
    $fileItem = Get-Item -LiteralPath $fullPath
    Write-Output "Uploading: $relPath ($($fileItem.Length) bytes)..."
    
    $upload = Invoke-Api 'POST' '/api/v1/admin/documents' $admin.Session (Write-Headers $admin) $null @{
        file = $fileItem
        kbId = $kbId
        buildConfig = $buildConfigJson
    }
    
    if ($upload.Status -ne 202 -and $upload.Status -ne 200) {
        throw "Upload failed for $relPath`: HTTP $($upload.Status) - $($upload.Body | ConvertTo-Json -Compress)"
    }
    
    $docId = [string]$upload.Body.documentId
    $verId = [string]$upload.Body.versionId
    $jobId = [string]$upload.Body.jobId
    
    # Grant ops read access to document
    $docAcl = Invoke-Api 'GET' "/api/v1/documents/$docId" $admin.Session
    $rowVersion = [long]$docAcl.Body.rowVersion
    $grantRes = Invoke-Api 'PUT' "/api/v1/admin/documents/$docId/acl" $admin.Session (Write-Headers $admin) @{
        expectedVersion = $rowVersion
        mode = 'RESTRICT'
        readers = @(
            @{ type = 'USER'; userId = [string]$adminMe.userId },
            @{ type = 'USER'; userId = [string]$opsMe.userId }
        )
    }
    
    $results[$relPath] = @{
        documentId = $docId
        versionId = $verId
        jobId = $jobId
    }
    $pendingJobs += @{ relPath = $relPath; jobId = $jobId; docId = $docId }
}

Write-Output "`nAll 18 documents uploaded. Polling ingestion jobs to completion..."
$timeout = [DateTime]::UtcNow.AddMinutes(5)
$completed = @{}

while ($completed.Count -lt $pendingJobs.Count) {
    if ([DateTime]::UtcNow -gt $timeout) {
        throw "Timeout waiting for ingestion jobs to complete."
    }
    Start-Sleep -Seconds 2
    foreach ($item in $pendingJobs) {
        if ($completed.ContainsKey($item.relPath)) { continue }
        $jobRes = Invoke-Api 'GET' "/api/v1/admin/jobs/$($item.jobId)" $admin.Session
        $status = [string]$jobRes.Body.status
        $stage = [string]$jobRes.Body.stage
        if ($status -eq 'COMPLETED') {
            $completed[$item.relPath] = $true
            Write-Output "  [COMPLETED] $($item.relPath)"
        } elseif ($status -eq 'FAILED' -or $status -eq 'CANCELLED') {
            throw "Job $($item.jobId) for $($item.relPath) failed with status ${status}: $($jobRes.Body | ConvertTo-Json -Compress)"
        }
    }
}

Write-Output "`nUpdating bindings file: $BindingsPath"
foreach ($relPath in $results.Keys) {
    $sources[$relPath]['documentId'] = $results[$relPath]['documentId']
    $sources[$relPath]['versionId'] = $results[$relPath]['versionId']
}
$bindings['sources'] = $sources

$newJson = $bindings | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText($BindingsPath, $newJson, [Text.UTF8Encoding]::new($false))
Write-Output "Bindings updated successfully!"

# Now invoke Bind-EvaluationDataset.ps1
$bindScript = Join-Path $PSScriptRoot 'Bind-EvaluationDataset.ps1'
$datasetPath = Join-Path $PSScriptRoot '../datasets/eval_accelerator_300.jsonl'
Write-Output "`nInvoking Bind-EvaluationDataset.ps1 on $datasetPath..."
& $bindScript -InputPath $datasetPath -BindingPath $BindingsPath

Write-Output "`n=== SUCCESS: 18 Sources Ingested and 300 Cases Bound! ==="
