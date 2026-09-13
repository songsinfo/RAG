[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Path,
    [int]$ExpectedCount = 0,
    [string]$SourceRoot = '',
    [switch]$RequireRuntimeBindings
)

$ErrorActionPreference = 'Stop'
$resolvedPath = (Resolve-Path -LiteralPath $Path).Path
$lines = @(Get-Content -LiteralPath $resolvedPath)
if ($lines.Count -eq 0) { throw 'Evaluation dataset is empty.' }
if ($ExpectedCount -gt 0 -and $lines.Count -ne $ExpectedCount) {
    throw "Expected $ExpectedCount cases, got $($lines.Count)."
}

$allowedSplits = @('DEV', 'TEST')
$allowedStatuses = @('ANSWER', 'PARTIAL', 'ABSTAIN', 'CONFLICT')
$allowedAnnotations = @('DRAFT', 'AUTO_REVIEWED', 'HUMAN_REVIEWED')
$seededPersonas = @('admin', 'ops', 'product', 'engineer')
$caseKeys = @{}
$groupSplits = @{}
$categories = @{}
$splits = @{}
$personas = @{}
$sourceRefs = @{}
$runtimeBindings = $true
$sourceCache = @{}
$sourceRootFull = ''
if (-not [string]::IsNullOrWhiteSpace($SourceRoot)) {
    $sourceRootFull = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $SourceRoot).Path)
}

for ($index = 0; $index -lt $lines.Count; $index++) {
    $lineNumber = $index + 1
    try {
        $case = $lines[$index] | ConvertFrom-Json -Depth 50
    } catch {
        throw "Line $lineNumber is not valid JSON: $($_.Exception.Message)"
    }
    foreach ($required in @('caseKey', 'groupKey', 'split', 'category', 'question', 'persona', 'expected', 'annotationStatus')) {
        if ($null -eq $case.$required) { throw "Line $lineNumber is missing $required." }
    }
    $caseKey = [string]$case.caseKey
    if ([string]::IsNullOrWhiteSpace($caseKey) -or $caseKeys.ContainsKey($caseKey)) {
        throw "Line $lineNumber has an empty or duplicate caseKey: $caseKey"
    }
    $caseKeys[$caseKey] = $true

    $split = ([string]$case.split).ToUpperInvariant()
    if ($split -notin $allowedSplits) { throw "Case $caseKey has invalid split $split." }
    $splits[$split] = 1 + [int]$splits[$split]
    $groupKey = [string]$case.groupKey
    if ($groupSplits.ContainsKey($groupKey) -and $groupSplits[$groupKey] -ne $split) {
        throw "Group $groupKey leaks across DEV and TEST."
    }
    $groupSplits[$groupKey] = $split

    $category = [string]$case.category
    if ([string]::IsNullOrWhiteSpace($category)) { throw "Case $caseKey has no category." }
    $categories[$category] = 1 + [int]$categories[$category]
    $questionText = [string]$case.question.text
    $turns = @($case.question.turns)
    if ([string]::IsNullOrWhiteSpace($questionText) -and $turns.Count -eq 0) {
        throw "Case $caseKey has no question text or turns."
    }

    $persona = [string]$case.persona.loginName
    if ([string]::IsNullOrWhiteSpace($persona)) { throw "Case $caseKey has no persona loginName." }
    $personas[$persona] = 1 + [int]$personas[$persona]
    if ($persona -notin $seededPersonas) { $runtimeBindings = $false }

    $status = ([string]$case.expected.status).ToUpperInvariant()
    if ($status -notin $allowedStatuses) { throw "Case $caseKey has invalid expected status $status." }
    $annotation = ([string]$case.annotationStatus).ToUpperInvariant()
    if ($annotation -notin $allowedAnnotations) { throw "Case $caseKey has invalid annotationStatus $annotation." }
    $groups = @($case.expected.evidenceGroups)
    if ($status -ne 'ABSTAIN' -and $groups.Count -eq 0) {
        throw "Answerable case $caseKey has no evidence group."
    }

    foreach ($group in $groups) {
        if ([string]::IsNullOrWhiteSpace([string]$group.fact)) {
            throw "Case $caseKey has an evidence group without a fact."
        }
        $alternatives = @($group.alternatives)
        if ($alternatives.Count -eq 0) { throw "Case $caseKey has an empty evidence group." }
        foreach ($anchor in $alternatives) {
            $documentId = [string]$anchor.documentId
            $versionId = [string]$anchor.versionId
            $quote = [string]$anchor.quote
            if ([string]::IsNullOrWhiteSpace($documentId) -or
                [string]::IsNullOrWhiteSpace($versionId) -or
                [string]::IsNullOrWhiteSpace($quote)) {
                throw "Case $caseKey has an incomplete evidence anchor."
            }
            $grade = [int]$anchor.relevanceGrade
            if ($grade -lt 0 -or $grade -gt 3) { throw "Case $caseKey has relevanceGrade outside 0..3." }
            $sourceRefs[$documentId] = $true
            $documentGuid = [guid]::Empty
            $versionGuid = [guid]::Empty
            if (-not [guid]::TryParse($documentId, [ref]$documentGuid) -or
                -not [guid]::TryParse($versionId, [ref]$versionGuid)) {
                $runtimeBindings = $false
            }
            if (-not [string]::IsNullOrWhiteSpace($sourceRootFull)) {
                $sourcePath = [IO.Path]::GetFullPath((Join-Path $sourceRootFull $documentId))
                $rootPrefix = $sourceRootFull.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
                if (-not $sourcePath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "Case $caseKey resolves outside SourceRoot: $documentId"
                }
                if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
                    throw "Case $caseKey source file does not exist: $documentId"
                }
                if (-not $sourceCache.ContainsKey($sourcePath)) {
                    $sourceCache[$sourcePath] = [IO.File]::ReadAllText($sourcePath, [Text.Encoding]::UTF8)
                }
                if (-not $sourceCache[$sourcePath].Contains($quote)) {
                    throw "Case $caseKey quote cannot be located in source: $documentId"
                }
            }
        }
    }
}

$result = [ordered]@{
    path = $resolvedPath
    cases = $lines.Count
    splits = $splits
    categories = $categories
    personas = $personas
    uniqueSourceRefs = $sourceRefs.Count
    sourceQuotesChecked = -not [string]::IsNullOrWhiteSpace($sourceRootFull)
    runtimeBindingsReady = $runtimeBindings
    runtimeBindingNote = if ($runtimeBindings) {
        'All personas and evidence IDs use seeded accounts and UUID-shaped runtime IDs.'
    } else {
        'Template is structurally valid but must map source paths/version placeholders and persona aliases to runtime records before API import.'
    }
}
$result | ConvertTo-Json -Depth 10
if ($RequireRuntimeBindings -and -not $runtimeBindings) {
    throw 'Dataset is structurally valid but runtime bindings are unresolved.'
}
