$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$keyPath = Join-Path $root '.codex-sync\config\api-key'
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) { throw 'Gateway API key file is missing' }
$keyFile = [IO.File]::ReadAllText($keyPath)
if ($keyFile.Length -gt 4096) { throw 'Gateway API key file is too large' }
$key = $keyFile.TrimStart([char]0xFEFF).Trim()
if (-not $key -or $key -eq 'REPLACE_WITH_NEW_API_KEY' -or $key.Contains([char]10) -or $key.Contains([char]13)) {
    throw 'Gateway API key is invalid'
}
[Console]::Out.Write($key)
