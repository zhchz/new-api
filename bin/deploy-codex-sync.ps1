param(
    [int]$Port,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exePath = Join-Path $root 'newapi.exe'
if (Get-Process -Name newapi -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exePath }) {
    throw 'Stop the existing newapi.exe from this directory before rebuilding'
}

$keyPath = Join-Path $root '.codex-sync\config\api-key'
New-Item -ItemType Directory -Force (Split-Path $keyPath) | Out-Null
if (-not (Test-Path -LiteralPath $keyPath)) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($keyPath, "REPLACE_WITH_NEW_API_KEY`n", $utf8)
} elseif (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw "Gateway key path is not a file: $keyPath"
}

if ($PSBoundParameters.ContainsKey('Port')) {
    if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid port' }
    $env:PORT = "$Port"
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
if (-not $PSBoundParameters.ContainsKey('Port')) {
    Push-Location $root
    try {
        $portText = (& $exePath --print-listen-port)
        if ($LASTEXITCODE -ne 0 -or -not [int]::TryParse($portText, [ref]$Port) -or $Port -lt 1 -or $Port -gt 65535) {
            throw 'Could not determine the gateway listening port from newapi.exe'
        }
    } finally { Pop-Location }
}
$portPath = Join-Path $root '.codex-sync\config\gateway-port'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($portPath, "$Port`n", $utf8)
$enabledPath = Join-Path $root '.codex-sync\config\auto-sync-enabled'
if (Test-Path -LiteralPath $enabledPath) { Remove-Item -LiteralPath $enabledPath }

if (-not $NoStart) {
    $process = Start-Process -FilePath $exePath -WorkingDirectory $root -WindowStyle Normal -PassThru
    $deadline = (Get-Date).AddSeconds(90)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) { throw 'Gateway exited after launch; check its startup configuration and logs' }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/status" -UseBasicParsing -TimeoutSec 2
            $status = $response.Content | ConvertFrom-Json
            if ($response.StatusCode -eq 200 -and $status.success -eq $true) {
                $ready = $true
                break
            }
        } catch {
            # The gateway may still be starting.
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Gateway did not become ready within 90 seconds; check its startup logs' }
}
Write-Host "Gateway address: http://127.0.0.1:$Port"
Write-Host "Create an API key and usable model channels, replace the placeholder in $keyPath, then restart Codex through bin\start-codex-with-new-api-key.ps1. Keep the gateway running."
