param(
    [Parameter(Mandatory = $true)][int]$Port,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid gateway port' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$utf8 = New-Object System.Text.UTF8Encoding($false)
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$configPath = Join-Path $codexHome 'config.toml'
$original = if (Test-Path -LiteralPath $configPath) { [IO.File]::ReadAllText($configPath) } else { '' }
$templatePath = Join-Path $root '.codex-sync\config\template.json'
$catalogPath = Join-Path $root '.codex-sync\catalog\models.json'
New-Item -ItemType Directory -Force (Split-Path $templatePath) | Out-Null
New-Item -ItemType Directory -Force (Split-Path $catalogPath) | Out-Null
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
if (-not (Test-Path -LiteralPath $catalogPath)) {
    [IO.File]::WriteAllText($catalogPath, "{`"models`":[]}`n", $utf8)
}
if ($PrepareOnly) { return }
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
$readKeyScript = Join-Path $PSScriptRoot 'read-codex-api-key.ps1'
if (-not (Test-Path -LiteralPath $readKeyScript -PathType Leaf)) { throw "Missing Codex key reader: $readKeyScript" }
$quotedReadKeyScript = $readKeyScript.Replace('\', '\\').Replace('"', '\"')
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
wire_api = "responses"

[model_providers.new_api_sync.auth]
command = "powershell.exe"
args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "$quotedReadKeyScript"]
$providerEnd
"@ + "`n"
if ($updated -ne $(if (Test-Path -LiteralPath $configPath) { [IO.File]::ReadAllText($configPath) } else { '' })) {
    New-Item -ItemType Directory -Force $codexHome | Out-Null
    if (Test-Path -LiteralPath $configPath) {
        $backup = Join-Path $codexHome 'config.toml.codex-sync-backup'
        if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $configPath -Destination $backup }
    }
    $temporary = Join-Path $codexHome ('.config-' + [guid]::NewGuid().ToString('N'))
    $replacementBackup = Join-Path $codexHome ('.config-replaced-' + [guid]::NewGuid().ToString('N'))
    $replaced = $false
    try {
        [IO.File]::WriteAllText($temporary, $updated, $utf8)
        if (Test-Path -LiteralPath $configPath) {
            [IO.File]::Replace($temporary, $configPath, $replacementBackup)
            $replaced = $true
        } else {
            [IO.File]::Move($temporary, $configPath)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        if ($replaced -and (Test-Path -LiteralPath $replacementBackup)) {
            Remove-Item -LiteralPath $replacementBackup -Force
        }
    }
}
