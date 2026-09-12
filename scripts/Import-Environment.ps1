param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference='Stop'
foreach($line in Get-Content -LiteralPath $Path){
 if($line -match '^\s*(#|$)'){continue}
 if($line -notmatch '^([A-Z][A-Z0-9_]*)=(.*)$'){throw 'Invalid environment-file line'}
 [Environment]::SetEnvironmentVariable($Matches[1],$Matches[2],'Process')
}

