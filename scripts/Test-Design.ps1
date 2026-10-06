# 这里只做文档/结构静态检查，不会真正连接或执行 MySQL、Elasticsearch；用于快速发现设计文件缺失和契约漂移。
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This workspace verification entry point expects Windows native PowerShell.'
}
$ragRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$designRoot = Join-Path $ragRoot 'docs/design'
$prdRoot = Join-Path $ragRoot 'docs/prd'
$issues = [System.Collections.Generic.List[string]]::new()
$markdownFiles = @(Get-ChildItem -LiteralPath $ragRoot -Recurse -File -Filter '*.md' | Where-Object { $_.FullName -notmatch '[\\/](node_modules|\.runtime|\.git|\.worktrees)[\\/]' })
$linkCount = 0
foreach ($file in $markdownFiles) {
    $content = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8
    $fences = [regex]::Matches($content, '(?m)^' + [char]96 + '{3}').Count
    if ($fences % 2 -ne 0) { $issues.Add("Unbalanced fences: $($file.FullName)") }
    foreach ($match in [regex]::Matches($content, '\[[^\]]+\]\(([^)]+)\)')) {
        $target = $match.Groups[1].Value.Trim('<', '>')
        if ($target -match '^[a-zA-Z][a-zA-Z0-9+.-]*://' -or $target.StartsWith('#')) { continue }
        $targetPath = ($target -split '#')[0]
        $resolved = [System.IO.Path]::GetFullPath((Join-Path $file.DirectoryName $targetPath))
        if (-not (Test-Path -LiteralPath $resolved)) { $issues.Add("Broken link: $($file.Name) -> $target") }
        $linkCount++
    }
}
$prdIds = @(
    Get-ChildItem -LiteralPath $prdRoot -File -Filter '*.md' | ForEach-Object {
        $content = Get-Content -LiteralPath $_.FullName -Raw -Encoding utf8
        [regex]::Matches($content, '(?m)^\| (AC-\d{2}-\d{2}) \|') |
            ForEach-Object { $_.Groups[1].Value }
    }
)
$trace = Get-Content -LiteralPath (Join-Path $designRoot '08-traceability.md') -Raw -Encoding utf8
$traceIds = @([regex]::Matches($trace, '(?m)^\| (AC-\d{2}-\d{2}) \|') | ForEach-Object { $_.Groups[1].Value })
if (@($prdIds | Sort-Object -Unique).Count -ne $prdIds.Count) { $issues.Add('Duplicate PRD AC IDs') }
if (@($traceIds | Sort-Object -Unique).Count -ne $traceIds.Count) { $issues.Add('Duplicate trace AC IDs') }
if ($prdIds.Count -eq 0 -or $traceIds.Count -eq 0) {
    $issues.Add('Missing acceptance criteria')
} elseif (Compare-Object ($prdIds | Sort-Object) ($traceIds | Sort-Object)) {
    $issues.Add('PRD and trace IDs differ')
}
$jsonFiles = @(Get-ChildItem -LiteralPath $designRoot -File -Filter '*.json')
foreach ($file in $jsonFiles) {
    $mapping = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8 | ConvertFrom-Json
    if ($mapping.mappings.dynamic -ne 'strict') { $issues.Add("Mapping not strict: $($file.Name)") }
    foreach ($field in @('tenant_id', 'build_id', 'canonical_chunk_id')) {
        if ($mapping.mappings.properties.$field.type -ne 'keyword') {
            $issues.Add("Missing keyword $field in $($file.Name)")
        }
    }
}
$sql = Get-Content -LiteralPath (Join-Path $designRoot 'schema.sql') -Raw -Encoding utf8
$tableMatches = [regex]::Matches($sql, '(?s)CREATE TABLE (\w+) \((.*?)\) ENGINE=InnoDB')
$tables = @{}
foreach ($match in $tableMatches) {
    $tableName = $match.Groups[1].Value
    if ($tables.ContainsKey($tableName)) { $issues.Add("Duplicate table: $tableName") }
    $body = $match.Groups[2].Value
    $columns = @{}
    foreach ($columnMatch in [regex]::Matches($body, '(?m)^  (\w+) ([^\r\n]+)')) {
        $columnName = $columnMatch.Groups[1].Value
        if ($columnName -in @('PRIMARY','UNIQUE','KEY','CONSTRAINT','CHECK')) { continue }
        if ($columns.ContainsKey($columnName)) { $issues.Add("Duplicate column: $tableName.$columnName") }
        $columns[$columnName] = $columnMatch.Groups[2].Value.TrimEnd(',')
    }
    $keys = @([regex]::Matches($body, '(?:PRIMARY KEY|UNIQUE KEY \w+)\s*\(([^)]+)\)') |
        ForEach-Object { ($_.Groups[1].Value -replace '\s','') })
    $tables[$tableName] = @{ Columns = $columns; Body = $body; UniqueKeys = $keys }
    if ($tableName -ne 'tenant' -and -not $columns.ContainsKey('tenant_id')) {
        $issues.Add("No tenant ownership: $tableName")
    }
}
$foreignKeys = [System.Collections.Generic.List[object]]::new()
foreach ($tableName in $tables.Keys) {
    foreach ($match in [regex]::Matches($tables[$tableName].Body, 'CONSTRAINT (\w+) FOREIGN KEY \(([^)]+)\) REFERENCES (\w+) \(([^)]+)\)')) {
        $foreignKeys.Add(@{Table=$tableName; Name=$match.Groups[1].Value; Local=$match.Groups[2].Value; Target=$match.Groups[3].Value; Remote=$match.Groups[4].Value})
    }
}
foreach ($match in [regex]::Matches($sql, '(?s)ALTER TABLE (\w+) ADD CONSTRAINT (\w+)\s+FOREIGN KEY \(([^)]+)\)\s+REFERENCES (\w+) \(([^)]+)\)')) {
    $foreignKeys.Add(@{Table=$match.Groups[1].Value; Name=$match.Groups[2].Value; Local=$match.Groups[3].Value; Target=$match.Groups[4].Value; Remote=$match.Groups[5].Value})
}
$constraintNames = @{}
foreach ($fk in $foreignKeys) {
    if ($constraintNames.ContainsKey($fk.Name)) { $issues.Add("Duplicate FK constraint: $($fk.Name)") }
    $constraintNames[$fk.Name] = $true
    if (-not $tables.ContainsKey($fk.Target)) { $issues.Add("Unknown FK target: $($fk.Target)"); continue }
    $localColumns = @(($fk.Local -replace '\s','').Split(','))
    $remoteColumns = @(($fk.Remote -replace '\s','').Split(','))
    if ($localColumns.Count -ne $remoteColumns.Count) { $issues.Add("FK arity mismatch: $($fk.Name)"); continue }
    for ($i = 0; $i -lt $localColumns.Count; $i++) {
        if (-not $tables[$fk.Table].Columns.ContainsKey($localColumns[$i])) { $issues.Add("Missing local FK column: $($fk.Table).$($localColumns[$i])") }
        if (-not $tables[$fk.Target].Columns.ContainsKey($remoteColumns[$i])) { $issues.Add("Missing remote FK column: $($fk.Target).$($remoteColumns[$i])") }
    }
    if (($remoteColumns -join ',') -notin $tables[$fk.Target].UniqueKeys) {
        $issues.Add("FK target is not an explicit unique key: $($fk.Name)")
    }
}
$report = [ordered]@{
    scope = 'Static documentation structure only; no service execution'
    markdownFiles = $markdownFiles.Count
    localLinks = $linkCount
    acceptanceCriteria = $prdIds.Count
    tracedCriteria = $traceIds.Count
    jsonMappings = $jsonFiles.Count
    sqlTables = $tables.Count
    foreignKeys = $foreignKeys.Count
    issues = @($issues)
}
$report | ConvertTo-Json -Depth 5
if ($issues.Count -gt 0) { throw 'Design structural checks failed.' }

