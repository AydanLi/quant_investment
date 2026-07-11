[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(1, 65535)]
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dashboardFile = "streamlit_dashboard_db_v1_1_save_experiment.py"
$dashboardPath = Join-Path $projectRoot $dashboardFile
$runtimeDir = Join-Path $projectRoot ".runtime"
$dashboardUrl = "http://localhost:$Port"
$healthUrl = "$dashboardUrl/_stcore/health"

function Test-DashboardHealth {
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

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project Python was not found at '$pythonExe'. Create .venv and install requirements first."
}
if (-not (Test-Path -LiteralPath $dashboardPath -PathType Leaf)) {
    throw "Dashboard entry point was not found at '$dashboardPath'."
}

if (-not (Test-DashboardHealth)) {
    if (Test-LocalPort) {
        throw "Port $Port is already in use by another application. Close it or run the launcher with a different -Port value."
    }

    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $runtimeDir "dashboard-$timestamp.out.log"
    $stderrLog = Join-Path $runtimeDir "dashboard-$timestamp.err.log"
    $pidFile = Join-Path $runtimeDir "dashboard.pid"

    $streamlitArgs = @(
        "-m",
        "streamlit",
        "run",
        $dashboardFile,
        "--server.headless",
        "true",
        "--server.address",
        "localhost",
        "--server.port",
        "$Port",
        "--browser.gatherUsageStats",
        "false"
    )

    $dashboardProcess = Start-Process `
        -FilePath $pythonExe `
        -ArgumentList $streamlitArgs `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    Set-Content -LiteralPath $pidFile -Value $dashboardProcess.Id -Encoding ascii

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-DashboardHealth) {
            break
        }
        if ($dashboardProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if (-not (Test-DashboardHealth)) {
        $details = "Dashboard did not become ready. Review '$stderrLog' and '$stdoutLog'."
        if ($dashboardProcess.HasExited) {
            $details += " Process exit code: $($dashboardProcess.ExitCode)."
        }
        throw $details
    }
}

if (-not $NoBrowser) {
    Start-Process $dashboardUrl
}

Write-Host "Quant Dashboard is ready at $dashboardUrl"
