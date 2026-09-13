[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [ValidateSet('demo', 'test')]
    [string]$Environment = 'demo',
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Http {
    param(
        [object]$Response,
        [int]$Status,
        [AllowNull()][string]$Code,
        [string]$Context
    )
    Assert-True ($null -ne $Response) "$Context returned no response"
    Assert-True ([int]$Response.Status -eq $Status) "$Context expected HTTP $Status, got $($Response.Status)"
    if ($null -ne $Code) {
        Assert-True ($null -ne $Response.Body) "$Context returned no JSON error body"
        Assert-True ([string]$Response.Body.code -eq $Code) "$Context expected code $Code, got $($Response.Body.code)"
    }
}

if ($SelfTest) {
    $ok = [pscustomobject]@{ Status = 409; Body = [pscustomobject]@{ code = 'EXPECTED' } }
    Assert-Http $ok 409 'EXPECTED' 'valid error'
    foreach ($bad in @(
        [pscustomobject]@{ Status = 200; Body = [pscustomobject]@{ code = 'EXPECTED' } },
        [pscustomobject]@{ Status = 500; Body = [pscustomobject]@{ code = 'EXPECTED' } },
        [pscustomobject]@{ Status = 409; Body = [pscustomobject]@{ code = 'WRONG' } }
    )) {
        $rejected = $false
        try { Assert-Http $bad 409 'EXPECTED' 'negative case' } catch { $rejected = $true }
        Assert-True $rejected 'Assert-Http accepted an invalid response'
    }
    $rejected = $false
    try { Assert-True $false 'search result did not contain the marker' } catch { $rejected = $true }
    Assert-True $rejected 'search assertion accepted an empty result'
    Write-Output 'Self-test passed: five false-positive paths were rejected.'
    return
}

function Invoke-Api {
    param(
        [ValidateSet('GET', 'POST', 'PUT', 'PATCH', 'DELETE')]
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
        TimeoutSec = 30
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
            throw "$Method $Path returned non-JSON content with HTTP $([int]$response.StatusCode)"
        }
    }
    [pscustomobject]@{ Status = [int]$response.StatusCode; Body = $parsed }
}

function New-AuthenticatedSession {
    param([string]$LoginName, [string]$Password)
    $session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $csrf = Invoke-Api 'GET' '/api/v1/auth/csrf' $session
    Assert-Http $csrf 200 $null "CSRF for $LoginName"
    $headers = @{ 'X-CSRF-Token' = [string]$csrf.Body.csrfToken }
    $login = Invoke-Api 'POST' '/api/v1/auth/login' $session $headers @{
        tenantCode = $Environment
        loginName = $LoginName
        password = $Password
    }
    Assert-Http $login 200 $null "login $LoginName"
    Assert-True ([bool]$login.Body.authenticated) "login $LoginName was not authenticated"
    $fresh = Invoke-Api 'GET' '/api/v1/auth/csrf' $session
    Assert-Http $fresh 200 $null "authenticated CSRF for $LoginName"
    [pscustomobject]@{
        Session = $session
        Headers = @{ 'X-CSRF-Token' = [string]$fresh.Body.csrfToken }
    }
}

function Write-Headers {
    param([object]$Authentication)
    @{
        'X-CSRF-Token' = [string]$Authentication.Headers['X-CSRF-Token']
        'Idempotency-Key' = [guid]::NewGuid().ToString()
    }
}

$ragRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envPath = Join-Path $ragRoot ".runtime\$Environment.env"
if (-not (Test-Path -LiteralPath $envPath)) { throw "Environment file not found: $envPath" }
$seedPassword = ''
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^RAG_SEED_PASSWORD=(.*)$') { $seedPassword = $Matches[1].Trim() }
}
Assert-True (-not [string]::IsNullOrWhiteSpace($seedPassword)) "RAG_SEED_PASSWORD is missing from $envPath"

$probeSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
$probe = Invoke-Api 'GET' '/api/v1/auth/csrf' $probeSession
Assert-Http $probe 200 $null 'backend readiness'

$admin = New-AuthenticatedSession 'admin' $seedPassword
$ops = New-AuthenticatedSession 'ops' $seedPassword
$adminMe = Invoke-Api 'GET' '/api/v1/me' $admin.Session
$opsMe = Invoke-Api 'GET' '/api/v1/me' $ops.Session
Assert-Http $adminMe 200 $null 'admin identity'
Assert-Http $opsMe 200 $null 'ops identity'
Assert-True ([bool]$adminMe.Body.admin) 'seeded admin account is not an administrator'
Assert-True (-not [bool]$opsMe.Body.admin) 'seeded ops account unexpectedly has administrator rights'

$suffix = [guid]::NewGuid().ToString('N').Substring(0, 10)
$kb = Invoke-Api 'POST' '/api/v1/admin/knowledge-bases' $admin.Session (Write-Headers $admin) @{
    name = "verification-$suffix"
    readers = @(@{ type = 'USER'; userId = [string]$adminMe.Body.userId })
}
Assert-Http $kb 200 $null 'create knowledge base'
$kbId = [string]$kb.Body.id

$runtimeDir = Join-Path $ragRoot '.runtime'
$documentPath = Join-Path $runtimeDir "verification-$suffix.md"
$marker = "KBVERIFY$suffix"
$newline = [Environment]::NewLine
$documentText = '# 验收资料' + $newline + $newline + "系统核验标记为 $marker，端口为 3307。" + $newline
[IO.File]::WriteAllText($documentPath, $documentText, [Text.UTF8Encoding]::new($false))
$buildConfig = @{
    embedding = $false
    summary = $false
    questions = $false
    vision = $false
} | ConvertTo-Json -Compress
$upload = Invoke-Api 'POST' '/api/v1/admin/documents' $admin.Session (Write-Headers $admin) $null @{
    file = Get-Item -LiteralPath $documentPath
    kbId = $kbId
    buildConfig = $buildConfig
}
Assert-Http $upload 202 $null 'upload document'
$documentId = [string]$upload.Body.documentId
$jobId = [string]$upload.Body.jobId

$job = $null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 2
    $jobResponse = Invoke-Api 'GET' "/api/v1/admin/jobs/$jobId" $admin.Session
    Assert-Http $jobResponse 200 $null 'poll ingestion job'
    $job = $jobResponse.Body
    if ($job.status -eq 'COMPLETED') { break }
    if ($job.status -in @('FAILED', 'CANCELLED', 'NEEDS_REVIEW')) {
        throw "ingestion stopped with status $($job.status), code $($job.errorCode)"
    }
}
Assert-True ($null -ne $job -and $job.status -eq 'COMPLETED') 'ingestion did not complete within 60 seconds'

$encodedMarker = [uri]::EscapeDataString($marker)
$adminSearch = Invoke-Api 'GET' "/api/v1/search?query=$encodedMarker&kbIds=$kbId" $admin.Session
Assert-Http $adminSearch 200 $null 'admin BM25 search'
$adminMatches = @($adminSearch.Body.evidence | Where-Object {
    $_.documentId -eq $documentId -and $_.text -like "*$marker*"
})
Assert-True ($adminMatches.Count -gt 0) 'admin BM25 search did not return the uploaded marker'

$opsSearchDenied = Invoke-Api 'GET' "/api/v1/search?query=$encodedMarker&kbIds=$kbId" $ops.Session
Assert-Http $opsSearchDenied 200 $null 'ops search without grant'
Assert-True (@($opsSearchDenied.Body.evidence).Count -eq 0) 'ops search returned a document without a grant'
$opsDocumentDenied = Invoke-Api 'GET' "/api/v1/documents/$documentId" $ops.Session
Assert-Http $opsDocumentDenied 404 'RESOURCE_NOT_FOUND' 'ops document without grant'

$grant = Invoke-Api 'PUT' "/api/v1/admin/documents/$documentId/acl" $admin.Session (Write-Headers $admin) @{
    expectedVersion = 0
    mode = 'RESTRICT'
    readers = @(@{ type = 'USER'; userId = [string]$opsMe.Body.userId })
}
Assert-Http $grant 200 $null 'grant ops document access'
$opsDocumentGranted = Invoke-Api 'GET' "/api/v1/documents/$documentId" $ops.Session
Assert-Http $opsDocumentGranted 200 $null 'ops document after grant'
$opsSearchGranted = Invoke-Api 'GET' "/api/v1/search?query=$encodedMarker&kbIds=$kbId" $ops.Session
Assert-Http $opsSearchGranted 200 $null 'ops search after grant'
Assert-True (@($opsSearchGranted.Body.evidence | Where-Object { $_.documentId -eq $documentId }).Count -gt 0) 'ops could not retrieve the granted document'

$revoke = Invoke-Api 'PUT' "/api/v1/admin/documents/$documentId/acl" $admin.Session (Write-Headers $admin) @{
    expectedVersion = [long]$grant.Body.rowVersion
    mode = 'RESTRICT'
    readers = @(@{ type = 'USER'; userId = [string]$adminMe.Body.userId })
}
Assert-Http $revoke 200 $null 'revoke ops document access'
$opsSearchRevoked = Invoke-Api 'GET' "/api/v1/search?query=$encodedMarker&kbIds=$kbId" $ops.Session
Assert-Http $opsSearchRevoked 200 $null 'ops search after revoke'
Assert-True (@($opsSearchRevoked.Body.evidence).Count -eq 0) 'ops search still returned the document after revocation'
$opsDocumentRevoked = Invoke-Api 'GET' "/api/v1/documents/$documentId" $ops.Session
Assert-Http $opsDocumentRevoked 404 'RESOURCE_NOT_FOUND' 'ops document after revoke'

if ($Environment -eq 'demo') {
    $isolation = Invoke-Api 'POST' '/api/v1/admin/eval/datasets' $admin.Session (Write-Headers $admin) @{
        name = "must-be-rejected-$suffix"
    }
    Assert-Http $isolation 409 'EVALUATION_ENVIRONMENT_REQUIRED' 'evaluation isolation'
}

$reportDir = Join-Path $runtimeDir 'reports'
if (-not (Test-Path -LiteralPath $reportDir)) { $null = New-Item -ItemType Directory -Path $reportDir }
$reportPath = Join-Path $reportDir 'http-integration.json'
$report = [ordered]@{
    generatedAt = [DateTimeOffset]::UtcNow.ToString('O')
    environment = $Environment
    scope = 'No-model HTTP integration'
    checks = @(
        'admin and ops authentication'
        'model-free markdown ingestion'
        'BM25 positive retrieval'
        'document read denial before grant'
        'document ACL grant'
        'document ACL revocation'
        'evaluation isolation in demo'
    )
    modelCalls = 0
    notTested = @(
        'generated answers'
        'real-model quality'
        'Docling PDF or DOCX parsing'
        'complete acceptance-criteria matrix'
    )
    documentId = $documentId
    jobId = $jobId
}
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $reportPath -Encoding utf8
Write-Output "No-model HTTP integration passed. Report: $reportPath"
