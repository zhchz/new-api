$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$keyPath = Join-Path $root '.codex-sync\config\api-key'
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) { throw "Missing gateway key: $keyPath" }
$key = [IO.File]::ReadAllText($keyPath).TrimStart([char]0xFEFF).Trim()
if (-not $key -or $key -eq 'REPLACE_WITH_NEW_API_KEY' -or $key.Contains("`n") -or $key.Contains("`r")) { throw 'Invalid gateway key' }
$portPath = Join-Path $root '.codex-sync\config\gateway-port'
$portText = if (Test-Path -LiteralPath $portPath -PathType Leaf) { [IO.File]::ReadAllText($portPath).Trim() } else { '' }
$port = 0
if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    throw "Invalid gateway port: $portPath. Run the gateway deployment script first."
}
$env:PORT = "$port"
$exePath = Join-Path $root 'newapi.exe'
& $exePath --check-codex-models
if ($LASTEXITCODE -ne 0) { throw 'The gateway key or model channels are not ready; Codex was not restarted' }
& (Join-Path $PSScriptRoot 'configure-codex-sync.ps1') -Port $port -PrepareOnly
& $exePath --sync-codex-models
if ($LASTEXITCODE -ne 0) { throw 'Model sync failed; Codex was not restarted' }
& (Join-Path $PSScriptRoot 'configure-codex-sync.ps1') -Port $port
& codex @args
exit $LASTEXITCODE
