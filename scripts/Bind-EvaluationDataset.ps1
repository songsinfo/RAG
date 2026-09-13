[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$InputPath,
    [string]$BindingPath = (Join-Path $PSScriptRoot '../.runtime/evaluation-bindings.json'),
    [string]$OutputPath = (Join-Path $PSScriptRoot '../.runtime/eval-bound.jsonl')
)

$ErrorActionPreference = 'Stop'
$seededPersonas = @('admin', 'ops', 'product', 'engineer')
$resolvedInput = (Resolve-Path -LiteralPath $InputPath).Path
$lines = @(Get-Content -LiteralPath $resolvedInput)
if ($lines.Count -eq 0) { throw 'Evaluation dataset is empty.' }

if (-not (Test-Path -LiteralPath $BindingPath -PathType Leaf)) {
    $personas = [ordered]@{}
    $sources = [ordered]@{}
    foreach ($line in $lines) {
        $case = $line | ConvertFrom-Json -Depth 50
        $login = [string]$case.persona.loginName
        if ($login -notin $seededPersonas) { $personas[$login] = if ($login -eq 'operator') { 'ops' } else { '' } }
        foreach ($group in @($case.expected.evidenceGroups)) {
            foreach ($anchor in @($group.alternatives)) {
                $sourceRef = [string]$anchor.documentId
                if (-not $sources.Contains($sourceRef)) {
                    $sources[$sourceRef] = [ordered]@{ documentId = ''; versionId = '' }
                }
            }
        }
    }
    $template = [ordered]@{
        personas = $personas
        sources = $sources
    }
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($BindingPath))
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($BindingPath), ($template | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
    throw "Binding template created at $BindingPath. Fill runtime persona, documentId and versionId values, then run again."
}

$bindings = Get-Content -Raw -LiteralPath $BindingPath | ConvertFrom-Json -AsHashtable -Depth 30
if ($null -eq $bindings.personas -or $null -eq $bindings.sources) { throw 'Binding file requires personas and sources objects.' }
$output = [Collections.Generic.List[string]]::new()
foreach ($line in $lines) {
    $case = $line | ConvertFrom-Json -AsHashtable -Depth 50
    $login = [string]$case.persona.loginName
    if ($login -notin $seededPersonas) {
        if (-not $bindings.personas.ContainsKey($login) -or [string]::IsNullOrWhiteSpace([string]$bindings.personas[$login])) {
            throw "Missing persona binding for $login."
        }
        $case.persona.loginName = [string]$bindings.personas[$login]
    }
    if ([string]$case.persona.loginName -notin $seededPersonas) {
        throw "Persona $($case.persona.loginName) is not a seeded evaluation account."
    }
    foreach ($group in @($case.expected.evidenceGroups)) {
        foreach ($anchor in @($group.alternatives)) {
            $sourceRef = [string]$anchor.documentId
            if (-not $bindings.sources.ContainsKey($sourceRef)) { throw "Missing source binding for $sourceRef." }
            $binding = $bindings.sources[$sourceRef]
            $documentId = [string]$binding.documentId
            $versionId = [string]$binding.versionId
            $documentGuid = [guid]::Empty
            $versionGuid = [guid]::Empty
            if (-not [guid]::TryParse($documentId, [ref]$documentGuid)) { throw "documentId for $sourceRef is not a UUID." }
            if (-not [guid]::TryParse($versionId, [ref]$versionGuid)) { throw "versionId for $sourceRef is not a UUID." }
            $anchor.documentId = $documentId
            $anchor.versionId = $versionId
        }
    }
    $output.Add(($case | ConvertTo-Json -Depth 50 -Compress))
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
[IO.Directory]::CreateDirectory((Split-Path -Parent $resolvedOutput)) | Out-Null
[IO.File]::WriteAllText($resolvedOutput, (($output -join [Environment]::NewLine) + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
& (Join-Path $PSScriptRoot 'Test-EvaluationDataset.ps1') -Path $resolvedOutput -ExpectedCount $lines.Count -RequireRuntimeBindings | Out-Null
Write-Output "Bound $($lines.Count) cases to runtime IDs: $resolvedOutput"
