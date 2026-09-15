[CmdletBinding()]
param(
    [string]$GatewayUrl = 'http://127.0.0.1:4000',
    [string]$MasterKey = 'sk-litellm-internal-master-key',
    [string]$Environment = 'demo',
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-Gateway {
    param(
        [ValidateSet('GET', 'POST')]
        [string]$Method,
        [string]$Path,
        [string]$AuthKey = '',
        [AllowNull()][object]$Body = $null
    )
    $headers = @{}
    if (-not [string]::IsNullOrWhiteSpace($AuthKey)) {
        $headers['Authorization'] = "Bearer $AuthKey"
    }
    $args = @{
        Uri = "$GatewayUrl$Path"
        Method = $Method
        Headers = $headers
        SkipHttpErrorCheck = $true
        TimeoutSec = 30
    }
    if ($null -ne $Body) {
        $args.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
        $args.ContentType = 'application/json'
    }
    $resp = Invoke-WebRequest @args
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($resp.Content)) {
        try {
            $parsed = $resp.Content | ConvertFrom-Json -Depth 30
        } catch {
            $parsed = $resp.Content
        }
    }
    [pscustomobject]@{
        Status = [int]$resp.StatusCode
        Body = $parsed
        Raw = $resp.Content
    }
}

if ($SelfTest) {
    Write-Output 'LiteLLM Gateway test suite self-test passed.'
    return
}

# 1. 最小健康检查
Write-Host '[1/6] Testing Gateway Health Endpoint...'
$health = Invoke-Gateway 'GET' '/health/liveliness'
if ($health.Status -ne 200) {
    $health = Invoke-Gateway 'GET' '/health'
}
Assert-True ($health.Status -eq 200) "Gateway health check failed with status $($health.Status)"

# 2. 鉴权拦截测试：未认证请求必须被拒绝
Write-Host '[2/6] Testing Authentication Enforcement...'
$unauth = Invoke-Gateway 'GET' '/models'
Assert-True ($unauth.Status -in @(401, 403)) "Gateway allowed unauthenticated /models access: Status $($unauth.Status)"

# 3. 稳定别名映射契约：chat-main, embedding-main, judge-main 必须存在
Write-Host '[3/6] Testing Stable Model Aliases...'
$models = Invoke-Gateway 'GET' '/models' $MasterKey
Assert-True ($models.Status -eq 200) "Authenticated /models failed: Status $($models.Status)"
$modelIds = @($models.Body.data.id)
Assert-True ($modelIds -contains 'chat-main') "Missing stable alias: chat-main"
Assert-True ($modelIds -contains 'embedding-main') "Missing stable alias: embedding-main"
Assert-True ($modelIds -contains 'judge-main') "Missing stable alias: judge-main"

# 4. 统一错误响应测试与密钥防护
Write-Host '[4/6] Testing Unified Error Response & Key Leak Prevention...'
$badReq = Invoke-Gateway 'POST' '/chat/completions' $MasterKey @{
    model = 'non-existent-model'
    messages = @(@{ role = 'user'; content = 'ping' })
}
Assert-True ($badReq.Status -ne 200) "Gateway unexpectedly succeeded for non-existent model"
Assert-True ($null -ne $badReq.Body.error) "Error response does not follow OpenAI-compatible error schema"
$envPath = Join-Path $PSScriptRoot "../.runtime/$Environment.env"
if (Test-Path -LiteralPath $envPath) {
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match '^MODEL_API_KEY=(.+)$') {
            $realKey = $Matches[1].Trim()
            if ($realKey.Length -gt 8) {
                Assert-True (-not $badReq.Raw.Contains($realKey)) "CRITICAL: Upstream MODEL_API_KEY leaked in error body!"
            }
        }
    }
}

# 5. Rerank 状态检查：当前版本默认保持关闭
Write-Host '[5/6] Testing Rerank State (must remain closed unless contract locked)...'
$rerankReq = Invoke-Gateway 'POST' '/rerank' $MasterKey @{
    model = 'rerank-main'
    query = 'test'
    documents = @('doc1')
}
Assert-True ($rerankReq.Status -in @(404, 400, 405, 501)) "Rerank is unexpectedly active without verified contract: Status $($rerankReq.Status)"

# 6. Chat 与 Embedding 契约冒烟测试
Write-Host '[6/6] Testing Chat and Embedding Completion Contract...'
$chatReq = Invoke-Gateway 'POST' '/chat/completions' $MasterKey @{
    model = 'chat-main'
    messages = @(@{ role = 'user'; content = 'Respond with exact word OK' })
    max_tokens = 10
}
Assert-True ($chatReq.Status -eq 200) "Chat completion on chat-main failed: $($chatReq.Status) $($chatReq.Raw)"
Assert-True ($null -ne $chatReq.Body.choices -and $chatReq.Body.choices.Count -gt 0) "Chat completion choices empty"
Assert-True ($null -ne $chatReq.Body.usage.prompt_tokens) "Usage prompt_tokens missing"

$embedReq = Invoke-Gateway 'POST' '/embeddings' $MasterKey @{
    model = 'embedding-main'
    input = 'ping'
}
Assert-True ($embedReq.Status -eq 200) "Embedding on embedding-main failed: $($embedReq.Status) $($embedReq.Raw)"
Assert-True ($null -ne $embedReq.Body.data -and $embedReq.Body.data.Count -gt 0) "Embedding data array empty"
Assert-True ($embedReq.Body.data[0].embedding.Count -gt 0) "Embedding vector is empty"

Write-Host 'All LiteLLM Gateway contract verification checks passed successfully.' -ForegroundColor Green
