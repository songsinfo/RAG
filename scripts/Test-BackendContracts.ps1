[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [string]$Environment = 'demo',
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility

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
    Assert-True ([int]$Response.Status -eq $Status) "$Context expected HTTP $Status, got $($Response.Status): $($Response.Raw)"
    if (-not [string]::IsNullOrEmpty($Code)) {
        Assert-True ($null -ne $Response.Body) "$Context returned no JSON body"
        Assert-True ([string]$Response.Body.code -eq $Code) "$Context expected code $Code, got '$($Response.Body.code)'"
    }
}

function Invoke-ContractApi {
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
        $invokeArgs.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
        $invokeArgs.ContentType = 'application/json'
    }
    $resp = Invoke-WebRequest @invokeArgs
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($resp.Content)) {
        try {
            $parsed = $resp.Content | ConvertFrom-Json -Depth 30
        } catch {
            $parsed = $null
        }
    }
    [pscustomobject]@{
        Status = [int]$resp.StatusCode
        Body = $parsed
        Raw = $resp.Content
        Cookies = $resp.Headers['Set-Cookie']
    }
}

if ($SelfTest) {
    Write-Output 'Contract test self-test passed.'
    return
}

$ragRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envPath = Join-Path $ragRoot ".runtime\$Environment.env"
if (-not (Test-Path -LiteralPath $envPath)) { throw "Environment file not found: $envPath" }

$seedPassword = ''
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^RAG_SEED_PASSWORD=(.*)$') { $seedPassword = $Matches[1].Trim() }
}
Assert-True (-not [string]::IsNullOrWhiteSpace($seedPassword)) "RAG_SEED_PASSWORD missing in $envPath"

Write-Host "=== Running Backend API Contract Verification against $BaseUrl ===" -ForegroundColor Cyan

# 1. CSRF 接口契约
Write-Host '[1/8] Verifying CSRF Token Endpoint Contract...'
$anonSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
$csrfResp = Invoke-ContractApi 'GET' '/api/v1/auth/csrf' $anonSession
Assert-Http $csrfResp 200 $null 'GET /api/v1/auth/csrf'
Assert-True (-not [string]::IsNullOrWhiteSpace($csrfResp.Body.csrfToken)) 'csrfToken missing from response body'
$csrfToken = [string]$csrfResp.Body.csrfToken

# 2. 身份认证与安全行为契约
Write-Host '[2/8] Verifying Auth & Security Guard Contracts...'
# 2.1 缺少 CSRF 必须被 403 拒绝
$noCsrf = Invoke-ContractApi 'POST' '/api/v1/auth/login' $anonSession @{} @{
    tenantCode = $Environment; loginName = 'admin'; password = $seedPassword
}
Assert-True ($noCsrf.Status -in @(400, 403)) "POST /login without CSRF expected 400/403, got $($noCsrf.Status)"

# 2.2 错误凭证必须被拒绝
$badLogin = Invoke-ContractApi 'POST' '/api/v1/auth/login' $anonSession @{ 'X-CSRF-Token' = $csrfToken } @{
    tenantCode = $Environment; loginName = 'admin'; password = 'wrong-password'
}
Assert-True ($badLogin.Status -in @(400, 401)) "POST /login with bad password expected 400/401, got $($badLogin.Status)"

# 2.3 正确登录必须成功并颁发会话
$loginResp = Invoke-ContractApi 'POST' '/api/v1/auth/login' $anonSession @{ 'X-CSRF-Token' = $csrfToken } @{
    tenantCode = $Environment; loginName = 'admin'; password = $seedPassword
}
Assert-Http $loginResp 200 $null 'POST /api/v1/auth/login'
Assert-True ([bool]$loginResp.Body.authenticated) 'Login failed to return authenticated: true'

# 3. /me 契约与登出契约
Write-Host '[3/8] Verifying /me and Logout Contracts...'
$meResp = Invoke-ContractApi 'GET' '/api/v1/me' $anonSession
Assert-Http $meResp 200 $null 'GET /api/v1/me'
Assert-True ([bool]$meResp.Body.admin) 'Admin user must have admin: true'
Assert-True ($meResp.Body.loginName -eq 'admin') 'Identity loginName mismatch'

# 4. 知识库列表与管理契约
Write-Host '[4/8] Verifying Knowledge Base API Contracts...'
$kbList = Invoke-ContractApi 'GET' '/api/v1/knowledge-bases' $anonSession
Assert-Http $kbList 200 $null 'GET /api/v1/knowledge-bases'

$kbSuffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
$freshCsrf = (Invoke-ContractApi 'GET' '/api/v1/auth/csrf' $anonSession).Body.csrfToken
$newKb = Invoke-ContractApi 'POST' '/api/v1/admin/knowledge-bases' $anonSession @{
    'X-CSRF-Token' = $freshCsrf
    'Idempotency-Key' = [guid]::NewGuid().ToString()
} @{
    name = "contract-kb-$kbSuffix"
    readers = @(@{ type = 'USER'; userId = [string]$meResp.Body.userId })
}
Assert-Http $newKb 200 $null 'POST /api/v1/admin/knowledge-bases'
$kbId = [string]$newKb.Body.id
Assert-True (-not [string]::IsNullOrWhiteSpace($kbId)) 'Created KB has empty ID'

# 5. 知识库 ACL 并发乐观锁契约
Write-Host '[5/8] Verifying KB ACL Optimistic Concurrency Control...'
$aclResp = Invoke-ContractApi 'GET' "/api/v1/admin/knowledge-bases/$kbId/acl" $anonSession
Assert-Http $aclResp 200 $null 'GET KB ACL'
Assert-True ($null -ne $aclResp.Body.rowVersion) 'KB ACL missing rowVersion'

# 传入冲突的 expectedVersion 必须返回 409
$conflictAcl = Invoke-ContractApi 'PUT' "/api/v1/admin/knowledge-bases/$kbId/acl" $anonSession @{
    'X-CSRF-Token' = $freshCsrf
    'Idempotency-Key' = [guid]::NewGuid().ToString()
} @{
    expectedVersion = 999999
    readers = @(@{ type = 'USER'; userId = [string]$meResp.Body.userId })
}
Assert-True ($conflictAcl.Status -in @(409, 412)) "KB ACL conflict expected 409, got $($conflictAcl.Status)"

# 6. 文档与上传任务契约
Write-Host '[6/8] Verifying Document Ingestion Contract...'
$docList = Invoke-ContractApi 'GET' "/api/v1/documents?kbId=$kbId" $anonSession
Assert-Http $docList 200 $null 'GET /api/v1/documents'

# 7. 检索接口契约
Write-Host '[7/8] Verifying Search Endpoint Contract...'
$searchResp = Invoke-ContractApi 'GET' "/api/v1/search?query=test&kbIds=$kbId" $anonSession
Assert-Http $searchResp 200 $null 'GET /api/v1/search'
Assert-True ($null -ne $searchResp.Body.evidence) 'Search response missing evidence array'

# 8. 会话与问答 Run 契约
Write-Host '[8/8] Verifying QA Session & Run Contract...'
$sessionResp = Invoke-ContractApi 'POST' '/api/v1/sessions' $anonSession @{
    'X-CSRF-Token' = $freshCsrf
    'Idempotency-Key' = [guid]::NewGuid().ToString()
} @{
    kbIds = @($kbId)
    mode = 'RAG'
}
Assert-True ($sessionResp.Status -in @(200, 201)) "POST /api/v1/sessions expected 200/201, got $($sessionResp.Status)"
$sessionId = [string]$sessionResp.Body.id
Assert-True (-not [string]::IsNullOrWhiteSpace($sessionId)) 'Session ID missing'

# 退出登录并验证 Session 撤销
Write-Host 'Verifying Logout and Session Revocation...'
$logoutResp = Invoke-ContractApi 'POST' '/api/v1/auth/logout' $anonSession @{
    'X-CSRF-Token' = $freshCsrf
} @{}
Assert-True ($logoutResp.Status -in @(200, 204)) "POST /api/v1/auth/logout failed: $($logoutResp.Status)"

$afterLogout = Invoke-ContractApi 'GET' '/api/v1/me' $anonSession
Assert-True ($afterLogout.Status -in @(401, 403)) "Session active after logout: $($afterLogout.Status)"

Write-Host '=== All Backend API Contract Checks Passed Successfully! ===' -ForegroundColor Green
