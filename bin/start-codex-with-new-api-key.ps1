$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$keyPath = Join-Path $root '.codex-sync\config\api-key'
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) { throw "Missing gateway key: $keyPath" }
$key = [IO.File]::ReadAllText($keyPath).Trim()
if (-not $key -or $key.Contains("`n") -or $key.Contains("`r")) { throw 'Invalid gateway key' }
$env:NEW_API_KEY = $key
& codex @args
exit $LASTEXITCODE
