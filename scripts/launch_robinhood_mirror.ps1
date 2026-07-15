[CmdletBinding()]
param([switch]$NoBrowser, [ValidateRange(1, 65535)][int]$Port = 8502)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appFile = "robinhood_mirror_dashboard.py"
$appPath = Join-Path $projectRoot $appFile
$runtimeDir = Join-Path $projectRoot ".runtime"
$url = "http://localhost:$Port"
$healthUrl = "$url/_stcore/health"
$pidFile = Join-Path $runtimeDir "robinhood-mirror.pid"

function Test-MirrorHealth {
    try {
        return (Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200
    }
    catch { return $false }
}

function Get-ManagedProcess {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return $null }
    $managedPid = 0
    if (-not [int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$managedPid)) { return $null }
    $process = Get-Process -Id $managedPid -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if (-not [string]::Equals($process.Path, $pythonExe, [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $process
}

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Project Python was not found at '$pythonExe'." }
if (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) { throw "Mirror dashboard was not found at '$appPath'." }

$healthy = Test-MirrorHealth
$managed = Get-ManagedProcess
if ($healthy -and $null -eq $managed) { throw "Port $Port is occupied by another healthy service." }

if (-not $healthy) {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $runtimeDir "robinhood-mirror-$timestamp.out.log"
    $stderrLog = Join-Path $runtimeDir "robinhood-mirror-$timestamp.err.log"
    $arguments = @("-m", "streamlit", "run", $appFile, "--server.headless", "true", "--server.address", "localhost", "--server.port", "$Port", "--browser.gatherUsageStats", "false")
    $process = Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-MirrorHealth) { break }
        if ($process.HasExited) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-MirrorHealth)) {
        $detail = "Robinhood Mirror did not become ready. Review '$stderrLog'."
        if ($process.HasExited) { $detail += " Exit code: $($process.ExitCode)." }
        throw $detail
    }
}

if (-not $NoBrowser) { Start-Process $url }
Write-Host "Robinhood Mirror is ready at $url"
