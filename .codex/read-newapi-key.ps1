$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'newapi.env'

if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Missing key file: $configPath"
}

$keyLine = Get-Content -LiteralPath $configPath |
    Where-Object { $_ -match '^\s*NEW_API_KEY=' } |
    Select-Object -Last 1

if (-not $keyLine) {
    throw "NEW_API_KEY is missing from $configPath"
}

$key = ($keyLine -replace '^\s*NEW_API_KEY=', '').Trim()
if (-not $key) {
    throw "NEW_API_KEY is empty in $configPath"
}

[Console]::Out.WriteLine($key)
