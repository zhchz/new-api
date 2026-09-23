param(
    [int]$Port = 9900,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exePath = Join-Path $root 'newapi.exe'
if (Get-Process -Name newapi -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exePath }) {
    throw 'Stop the existing newapi.exe from this directory before rebuilding'
}
New-Item -ItemType Directory -Force (Join-Path $root '.codex-sync\config') | Out-Null
$keyPath = Join-Path $root '.codex-sync\config\api-key'
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw "Create the gateway key file first: $keyPath"
}
$key = [IO.File]::ReadAllText($keyPath).Trim()
if (-not $key -or $key.Contains("`n") -or $key.Contains("`r")) {
    throw 'Gateway key must contain one nonempty line'
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid port' }
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$configPath = Join-Path $codexHome 'config.toml'
$original = if (Test-Path -LiteralPath $configPath) { [IO.File]::ReadAllText($configPath) } else { '' }
$start = '# BEGIN new-api model sync'
$end = '# END new-api model sync'
$providerStart = '# BEGIN new-api model sync provider'
$providerEnd = '# END new-api model sync provider'
foreach ($pair in @(@($start, $end), @($providerStart, $providerEnd))) {
    $startCount = [regex]::Matches($original, '(?m)^' + [regex]::Escape($pair[0]) + '\r?$').Count
    $endCount = [regex]::Matches($original, '(?m)^' + [regex]::Escape($pair[1]) + '\r?$').Count
    if ($startCount -ne $endCount -or $startCount -gt 1) { throw 'Invalid managed block in Codex config' }
}
if ($original -match '(?m)^\[model_providers\.new_api_sync\]' -and -not $original.Contains($providerStart)) {
    throw 'Existing model provider new_api_sync is not managed by this script'
}
$templatePath = Join-Path $root '.codex-sync\config\template.json'
$catalogPath = Join-Path $root '.codex-sync\catalog\models.json'
New-Item -ItemType Directory -Force (Split-Path $templatePath) | Out-Null
New-Item -ItemType Directory -Force (Split-Path $catalogPath) | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path -LiteralPath $templatePath)) {
    $existingCatalog = $null
    if ($original -match '(?m)^model_catalog_json\s*=\s*"([^"\r\n]+)"') {
        $existingCatalog = ('"' + $matches[1] + '"') | ConvertFrom-Json
    }
    $template = '{"models":[]}'
    if ($existingCatalog -and (Test-Path -LiteralPath $existingCatalog -PathType Leaf)) {
        $template = [IO.File]::ReadAllText($existingCatalog)
        $value = $template | ConvertFrom-Json
        if ($value.models -isnot [array]) { throw 'Existing catalog cannot be used as template' }
    }
    [IO.File]::WriteAllText($templatePath, $template + "`n", $utf8)
} else {
    $value = [IO.File]::ReadAllText($templatePath) | ConvertFrom-Json
    if ($value.models -isnot [array]) { throw 'Invalid existing model template' }
}
foreach ($pair in @(@($start, $end), @($providerStart, $providerEnd))) {
    $pattern = '(?s)(?m)^' + [regex]::Escape($pair[0]) + '\r?\n.*?^' + [regex]::Escape($pair[1]) + '\r?\n?'
    $original = [regex]::Replace($original, $pattern, '')
}
$lines = New-Object 'System.Collections.Generic.List[string]'
$topLevel = $true
foreach ($line in ($original -split "`r?`n")) {
    if ($line -match '^\s*\[') { $topLevel = $false }
    if ($topLevel -and $line -match '^\s*(model_provider|model_catalog_json)\s*=') { continue }
    $lines.Add($line)
}
$remainder = ($lines -join "`n").Trim("`n", "`r")
$quotedCatalog = $catalogPath.Replace('\', '\\').Replace('"', '\"')
$baseUrl = "http://127.0.0.1:$Port/v1"
$updated = @"
$start
model_provider = "new_api_sync"
model_catalog_json = "$quotedCatalog"
$end
$remainder

$providerStart
[model_providers.new_api_sync]
name = "New API"
base_url = "$baseUrl"
env_key = "NEW_API_KEY"
wire_api = "responses"
$providerEnd
"@ + "`n"
if ($updated -ne $(if (Test-Path -LiteralPath $configPath) { [IO.File]::ReadAllText($configPath) } else { '' })) {
    New-Item -ItemType Directory -Force $codexHome | Out-Null
    if (Test-Path -LiteralPath $configPath) {
        $backup = Join-Path $codexHome 'config.toml.codex-sync-backup'
        if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $configPath -Destination $backup }
    }
    $temporary = Join-Path $codexHome ('.config-' + [guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($temporary, $updated, $utf8)
        if (Test-Path -LiteralPath $configPath) {
            [IO.File]::Replace($temporary, $configPath, $null)
        } else {
            [IO.File]::Move($temporary, $configPath)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}
if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { throw 'Install Bun before building the frontend' }
if (-not (Get-Command go -ErrorAction SilentlyContinue)) { throw 'Install Go before building the exe' }
Push-Location (Join-Path $root 'web')
try {
    bun install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'bun install failed' }
    bun run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
} finally { Pop-Location }
Push-Location $root
try {
    go build -o newapi.exe .
    if ($LASTEXITCODE -ne 0) { throw 'Go build failed' }
} finally { Pop-Location }
if (-not $NoStart) {
    $env:PORT = "$Port"
    $startedAt = Get-Date
    Start-Process -FilePath (Join-Path $root 'newapi.exe') -WorkingDirectory $root
    $markerPath = Join-Path $root '.codex-sync\catalog\models.last-success'
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and
        (-not (Test-Path -LiteralPath $markerPath) -or (Get-Item -LiteralPath $markerPath).LastWriteTime -lt $startedAt)) {
        Start-Sleep -Seconds 2
    }
    if (-not (Test-Path -LiteralPath $catalogPath) -or -not (Test-Path -LiteralPath $markerPath) -or
        (Get-Item -LiteralPath $markerPath).LastWriteTime -lt $startedAt) {
        throw 'Gateway started, but model catalog was not generated within 90 seconds'
    }
}
Write-Host "Catalog: $catalogPath"
Write-Host 'Restart Codex through bin\start-codex-with-new-api-key.ps1 to load its key and model menu.'
