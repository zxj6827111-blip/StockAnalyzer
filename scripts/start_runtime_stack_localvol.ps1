param(
    [switch]$SkipBuild,
    [switch]$EnableLiveNotifications,
    [switch]$SkipScheduler,
    [int]$HealthTimeoutSec = 90,
    [string]$BaseUrl = "http://127.0.0.1:8001"
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

function Ensure-DockerDesktopService {
    if (-not $IsWindows) {
        return
    }
    $service = Get-Service -Name 'com.docker.service' -ErrorAction SilentlyContinue
    if ($null -eq $service) {
        return
    }
    if ($service.Status -eq 'Running') {
        return
    }
    Write-Host "Docker Desktop Service is not running. Attempting to start it..."
    Start-Service -Name 'com.docker.service'
    $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
    Write-Host "Docker Desktop Service is running."
}

$composeFiles = @(
    (Join-Path $projectRoot "docker-compose.yml"),
    (Join-Path $projectRoot "docker-compose.runtime.yml"),
    (Join-Path $projectRoot "docker-compose.runtime.localvol.yml")
)
if (-not $EnableLiveNotifications) {
    $composeFiles += (Join-Path $projectRoot "docker-compose.notifications.local.yml")
}

$requiredVolumes = @(
    "stock_analyzer_runtime_artifacts",
    "stock_analyzer_runtime_suggestions"
)

$fileArgs = @()
foreach ($composeFile in $composeFiles) {
    $fileArgs += "-f"
    $fileArgs += $composeFile
}

Ensure-DockerDesktopService

foreach ($volumeName in $requiredVolumes) {
    & docker volume create $volumeName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "failed to create volume: $volumeName"
    }
}

$args = @("up", "-d")
if (-not $SkipBuild) {
    $args += "--build"
}
$args += "api"
if (-not $SkipScheduler) {
    $args += "scheduler"
}

& docker compose @fileArgs @args
if ($LASTEXITCODE -ne 0) {
    throw "docker compose start failed"
}

$healthUri = "{0}/health" -f $BaseUrl.TrimEnd("/")
$deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
$lastError = ""
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUri -TimeoutSec 5
        if ($response.StatusCode -eq 200) {
            Write-Host "Runtime health check passed: $healthUri"
            if (-not $EnableLiveNotifications) {
                Write-Host "Local safe-notification override is active (console only)."
            }
            if ($SkipScheduler) {
                Write-Host "Scheduler was not started. Re-run without -SkipScheduler when you are ready."
            }
            return
        }
        $lastError = "unexpected status code $($response.StatusCode)"
    } catch {
        $lastError = $_.Exception.Message
    }
    Start-Sleep -Seconds 2
}

throw "runtime health check failed for $healthUri within ${HealthTimeoutSec}s: $lastError"
