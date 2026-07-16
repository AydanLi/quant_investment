[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(1, 65535)]
    [int]$Port = 8502
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appFile = "robinhood_mirror_dashboard.py"
$appPath = Join-Path $projectRoot $appFile
$optimizerPath = Join-Path $projectRoot "scripts\optimize_mirrored_portfolio.py"
$runtimeDir = Join-Path $projectRoot ".runtime"
$url = "http://localhost:$Port"
$healthUrl = "$url/_stcore/health"
$pidFile = Join-Path $runtimeDir "robinhood-mirror.pid"
$sourceStateFile = Join-Path $runtimeDir "robinhood-mirror.source-state"
$sourceRoots = @(
    "backtest",
    "config",
    "data",
    "execution",
    "report",
    "research",
    "risk",
    "services",
    "storage",
    "strategy",
    "utils"
)

function Test-MirrorHealth {
    try {
        $response = Invoke-WebRequest `
            -Uri $healthUrl `
            -UseBasicParsing `
            -TimeoutSec 1
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-LocalPort {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $task.Wait(500)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Get-MirrorSourceState {
    $sourceFiles = @(
        (Get-Item -LiteralPath $appPath),
        (Get-Item -LiteralPath $optimizerPath)
    )
    foreach ($relativeRoot in $sourceRoots) {
        $sourceRoot = Join-Path $projectRoot $relativeRoot
        if (Test-Path -LiteralPath $sourceRoot -PathType Container) {
            $sourceFiles += Get-ChildItem `
                -LiteralPath $sourceRoot `
                -Filter "*.py" `
                -File `
                -Recurse
        }
    }

    $entries = $sourceFiles |
        Sort-Object FullName -Unique |
        ForEach-Object {
            $relativePath = $_.FullName.Substring($projectRoot.Length).TrimStart("\")
            $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            "$relativePath|$hash"
        }
    return $entries -join "`n"
}

function Get-ManagedMirrorProcess {
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        return $null
    }

    $managedPid = 0
    $pidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if (-not [int]::TryParse($pidText, [ref]$managedPid)) {
        return $null
    }

    $process = Get-Process -Id $managedPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    if (-not [string]::Equals(
        $process.Path,
        $pythonExe,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $null
    }

    $pidWrittenAt = (Get-Item -LiteralPath $pidFile).LastWriteTime
    if ($process.StartTime -gt $pidWrittenAt.AddSeconds(5)) {
        return $null
    }
    return $process
}

function Stop-ManagedMirrorProcess {
    param([System.Diagnostics.Process]$Process)

    Stop-Process -Id $Process.Id -Force
    $Process.WaitForExit(10000) | Out-Null
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sourceStateFile -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Test-LocalPort)) {
        Start-Sleep -Milliseconds 200
    }
}

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project Python was not found at '$pythonExe'. Create .venv and install requirements first."
}
if (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) {
    throw "Mirror dashboard was not found at '$appPath'."
}
if (-not (Test-Path -LiteralPath $optimizerPath -PathType Leaf)) {
    throw "Mirror optimizer was not found at '$optimizerPath'."
}

$sourceState = Get-MirrorSourceState
$managedProcess = Get-ManagedMirrorProcess
$mirrorHealthy = Test-MirrorHealth

if ($null -ne $managedProcess) {
    $storedSourceState = if (Test-Path -LiteralPath $sourceStateFile) {
        Get-Content -LiteralPath $sourceStateFile -Raw
    }
    else {
        ""
    }
    if (-not $mirrorHealthy -or $sourceState -ne $storedSourceState.TrimEnd()) {
        Stop-ManagedMirrorProcess -Process $managedProcess
        $mirrorHealthy = $false
    }
}
elseif ($mirrorHealthy) {
    throw "Port $Port has a healthy service that is not managed by this launcher. Close it or use a different -Port value."
}

if (-not $mirrorHealthy) {
    if (Test-LocalPort) {
        throw "Port $Port is already in use by another application. Close it or run the launcher with a different -Port value."
    }

    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $runtimeDir "robinhood-mirror-$timestamp.out.log"
    $stderrLog = Join-Path $runtimeDir "robinhood-mirror-$timestamp.err.log"
    $arguments = @(
        "-m",
        "streamlit",
        "run",
        $appFile,
        "--server.headless",
        "true",
        "--server.address",
        "localhost",
        "--server.port",
        "$Port",
        "--browser.gatherUsageStats",
        "false"
    )
    $process = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-MirrorHealth) {
            break
        }
        if ($process.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-MirrorHealth)) {
        $detail = "Robinhood Mirror did not become ready. Review '$stderrLog' and '$stdoutLog'."
        if ($process.HasExited) {
            $detail += " Exit code: $($process.ExitCode)."
        }
        throw $detail
    }
    Set-Content -LiteralPath $sourceStateFile -Value $sourceState -Encoding utf8
}

if (-not $NoBrowser) {
    Start-Process $url
}
Write-Host "Robinhood Mirror is ready at $url"
