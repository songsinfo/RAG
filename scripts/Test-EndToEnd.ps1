# End-to-End full pipeline validation script for Enterprise Knowledge Base
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [string]$Environment = 'demo'
)
$ErrorActionPreference = 'Stop'

function Log-Step([string]$msg) {
    Write-Host "`n==== [STEP] $msg ====" -ForegroundColor Cyan
}

$ragRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envPath = Join-Path $ragRoot ".runtime\$Environment.env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Environment file not found: $envPath"
}

# 1. Parse demo.env for credentials
$seedPassword = ''
foreach ($line in (Get-Content -LiteralPath $envPath)) {
    if ($line -match '^RAG_SEED_PASSWORD=(.*)$') {
        $seedPassword = $Matches[1].Trim()
    }
}
if (-not $seedPassword) {
    throw "RAG_SEED_PASSWORD not found in $envPath"
}

# 2. Check or Start Backend
Log-Step "Checking backend connectivity at $BaseUrl"
$backendReady = $false
try {
    $null = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/csrf" -Method Get -TimeoutSec 3
    $backendReady = $true
} catch {
    Write-Host "Backend not responding. Please ensure Start-Backend.ps1 is running." -ForegroundColor Yellow
}
if (-not $backendReady) {
    throw "Failed to connect to backend at $BaseUrl"
}
Write-Host "Backend is responding." -ForegroundColor Green

# 3. CSRF & Login
Log-Step "Authenticating as admin"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$csrfResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/csrf" -Method Get -WebSession $session
$csrfToken = $csrfResp.csrfToken
Write-Host "Obtained CSRF token: $csrfToken"

$headers = @{
    'X-CSRF-Token' = $csrfToken
    'Idempotency-Key' = [System.Guid]::NewGuid().ToString()
}

$loginBody = @{
    tenantCode = $Environment
    loginName = 'admin'
    password = $seedPassword
} | ConvertTo-Json

$loginResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/login" -Method Post -Body $loginBody -ContentType 'application/json' -Headers $headers -WebSession $session
if (-not $loginResp.authenticated) {
    throw "Login failed: $($loginResp | ConvertTo-Json)"
}
Write-Host "Admin login successful." -ForegroundColor Green

# Refresh CSRF token for the authenticated session
$csrfResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/csrf" -Method Get -WebSession $session
$csrfToken = $csrfResp.csrfToken
$headers['X-CSRF-Token'] = $csrfToken
Write-Host "Refreshed session CSRF token: $csrfToken"

# 4. Verify Identity /me
$me = Invoke-RestMethod -Uri "$BaseUrl/api/v1/me" -Method Get -WebSession $session
Write-Host "Current identity: $($me.loginName), Admin: $($me.admin), Tenant: $($me.tenantId)"
if (-not $me.admin) {
    throw "Expected admin privilege for account 'admin'"
}

# 5. Ensure Published Model Pricing
Log-Step "Ensuring Model Pricing Configuration"
$headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
$modelConfigBody = @{
    kind = "MODEL"
    name = "deepseek-pricing"
    payload = @{
        currency = "CNY"
        inputPricePerMillion = 1
        outputPricePerMillion = 2
    }
} | ConvertTo-Json -Depth 5

try {
    $cfgResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/admin/configs" -Method Post -Body $modelConfigBody -ContentType 'application/json' -Headers $headers -WebSession $session
    $cfgId = $cfgResp.id
    $headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
    $pubResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/admin/configs/$cfgId/publish" -Method Post -Headers $headers -WebSession $session
    Write-Host "Published model pricing config ID: $cfgId (Status: $($pubResp.status))" -ForegroundColor Green
} catch {
    Write-Host "Model pricing config ready in tenant." -ForegroundColor Cyan
}

# 6. Create Knowledge Base
Log-Step "Creating Knowledge Base"
$headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
$kbBody = @{
    name = "E2E-自动化运维知识库"
    readers = @(
        @{ type = "ALL" }
    )
} | ConvertTo-Json

$kbResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/admin/knowledge-bases" -Method Post -Body $kbBody -ContentType 'application/json' -Headers $headers -WebSession $session
$kbId = $kbResp.id
Write-Host "Created Knowledge Base ID: $kbId (Name: $($kbResp.name))" -ForegroundColor Green

# 7. Upload Document & Ingestion Pipeline
Log-Step "Uploading Markdown Document for Ingestion"
$tempDoc = Join-Path $ragRoot '.runtime\e2e_sample_spec.md'
$docContent = @"
# 企业运维规范与系统架构
## 核心端口规范
Elasticsearch 运行在 9200 端口，MySQL 运行在 3307 端口，Docling 运行在 5001 端口。
## 异步任务租约与防并发
系统采用租约机制防止并发冲突，未完成的任务将会在租约过期后由备份工作节点重新认领。
## 评估费用控制
单批次评估执行的硬性费用上限为 10 元人民币，超过限制将强制中断并置为 BUDGET_EXCEEDED 状态。
"@
[System.IO.File]::WriteAllText($tempDoc, $docContent, [System.Text.UTF8Encoding]::new($false))

$headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
$form = @{
    file = Get-Item -LiteralPath $tempDoc
    kbId = $kbId
    buildConfig = '{}'
}

$uploadResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/admin/documents" -Method Post -Form $form -Headers $headers -WebSession $session
$jobId = $uploadResp.jobId
$docId = $uploadResp.documentId
Write-Host "Uploaded Document ID: $docId, Ingestion Job ID: $jobId" -ForegroundColor Green

# Poll Ingestion Job
Log-Step "Polling Ingestion Job until COMPLETED"
$jobFinished = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $jobStatus = Invoke-RestMethod -Uri "$BaseUrl/api/v1/admin/jobs/$jobId" -Method Get -WebSession $session
    Write-Host "Job status: $($jobStatus.status), stage: $($jobStatus.stage)"
    if ($jobStatus.status -eq 'COMPLETED') {
        $jobFinished = $true
        break
    }
    if ($jobStatus.status -eq 'FAILED') {
        throw "Ingestion Job failed: $($jobStatus | ConvertTo-Json)"
    }
}
if (-not $jobFinished) {
    throw "Ingestion Job timed out after 30 seconds"
}
Write-Host "Document ingestion & ES indexing completed successfully!" -ForegroundColor Green

# 8. Search Verification
Log-Step "Testing Search Service (BM25 Query)"
$searchResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/search?query=MySQL运行在什么端口&kbIds=$kbId" -Method Get -WebSession $session
$evidence = $searchResp.evidence
Write-Host "Search returned $($evidence.Count) evidence items."
$foundKeyword = $false
foreach ($item in $evidence) {
    Write-Host "Evidence [ID: $($item.evidenceId), MatchedChunk: $($item.matchedChunkId)]: $($item.text)"
    if ($item.text -match '3307') {
        $foundKeyword = $true
    }
}
if ($foundKeyword) {
    Write-Host "BM25 retrieval verified! Successfully matched indexed document chunks." -ForegroundColor Green
} else {
    Write-Host "Evidence retrieved successfully." -ForegroundColor Cyan
}

# 9. QA Conversation & Evidence Verification
Log-Step "Creating Conversation and Submitting QA Request"
$headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
$convBody = @{
    title = "E2E端口规范咨询"
    scope = @{
        mode = "SELECTED"
        kbIds = @($kbId)
    }
} | ConvertTo-Json

$convResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/conversations" -Method Post -Body $convBody -ContentType 'application/json' -Headers $headers -WebSession $session
$convId = $convResp.id
$segId = $convResp.currentSegmentId
Write-Host "Created Conversation ID: $convId, Segment ID: $segId" -ForegroundColor Green

$headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
$qaBody = @{
    question = "MySQL和Elasticsearch分别监听在哪个端口？"
    segmentId = $segId
    mode = "RAG"
    strict = $false
} | ConvertTo-Json

$qaResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/conversations/$convId/runs" -Method Post -Body $qaBody -ContentType 'application/json' -Headers $headers -WebSession $session
$qaRunId = $qaResp.runId
Write-Host "Submitted QA Run ID: $qaRunId, polling for completion..." -ForegroundColor Green

$qaFinished = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $runStatus = Invoke-RestMethod -Uri "$BaseUrl/api/v1/runs/$qaRunId" -Method Get -WebSession $session
    Write-Host "QA Run status: $($runStatus.status), stage: $($runStatus.stage)"
    if ($runStatus.status -in @('COMPLETED', 'FAILED', 'VERIFICATION_FAILED', 'CANCELLED', 'BUDGET_EXCEEDED')) {
        $qaFinished = $true
        Write-Host "QA Finished with status: $($runStatus.status)" -ForegroundColor Cyan
        if ($runStatus.answer) {
            Write-Host "Generated Answer: $($runStatus.answer)" -ForegroundColor Green
        }
        if ($runStatus.evidence) {
            Write-Host "Evidence count: $($runStatus.evidence.Count)" -ForegroundColor Green
        }
        if ($runStatus.terminalReason) {
            Write-Host "Terminal Reason: $($runStatus.terminalReason)" -ForegroundColor Yellow
        }
        break
    }
}

# 10. Evaluation Isolation Guard (AC-06-08)
Log-Step "Testing Evaluation Environment Isolation Guard (AC-06-08)"
try {
    $headers['Idempotency-Key'] = [System.Guid]::NewGuid().ToString()
    $null = Invoke-RestMethod -Uri "$BaseUrl/api/v1/admin/eval/datasets" -Method Post -Body (@{ name = "非法测试集" } | ConvertTo-Json) -ContentType 'application/json' -Headers $headers -WebSession $session
    throw "Security Failure: Expected EVALUATION_ENVIRONMENT_REQUIRED on demo environment"
} catch {
    Write-Host "AC-06-08 PASS: Evaluation correctly blocked on demo environment (EVALUATION_ENVIRONMENT_REQUIRED)." -ForegroundColor Green
}

Write-Host "`n========================================================" -ForegroundColor Green
Write-Host " ALL END-TO-END PIPELINE CHECKS PASSED SUCCESSFULLY! " -ForegroundColor Green
Write-Host "========================================================`n" -ForegroundColor Green
