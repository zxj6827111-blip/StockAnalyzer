param(
    [switch]$SkipBuild,
    [switch]$IncludeScheduler,
    [switch]$IncludeWriteChecks,
    [int]$HealthTimeoutSec = 120,
    [string]$BaseUrl = "http://127.0.0.1:8001"
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

function New-ComposeArgs {
    param(
        [string]$Root
    )

    $composeFiles = @(
        (Join-Path $Root "docker-compose.yml"),
        (Join-Path $Root "docker-compose.runtime.yml"),
        (Join-Path $Root "docker-compose.runtime.localvol.yml"),
        (Join-Path $Root "docker-compose.notifications.local.yml")
    )

    $args = @()
    foreach ($composeFile in $composeFiles) {
        $args += "-f"
        $args += $composeFile
    }
    return $args
}

function Assert-DockerAvailable {
    Write-Host "Checking Docker connectivity..."
    & docker version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "docker version failed. Please confirm Docker Desktop is running and the current shell can access it."
    }
}

function Start-ComposeStack {
    param(
        [string[]]$ComposeArgs
    )

    $upArgs = @("compose") + $ComposeArgs + @("up", "-d")
    if (-not $SkipBuild) {
        $upArgs += "--build"
    }
    $upArgs += @("api", "redis")
    if ($IncludeScheduler) {
        $upArgs += "scheduler"
    }

    Write-Host "Starting Docker services..."
    & docker @upArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose up failed."
    }
}

function Wait-ForHealth {
    param(
        [string]$HealthUrl,
        [int]$TimeoutSec
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
            if ($null -ne $response.status) {
                return $response
            }
            $lastError = "health endpoint returned no status field"
        } catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }

    throw "Health check timed out after ${TimeoutSec}s. Last error: $lastError"
}

function Test-HasAllKeys {
    param(
        [object]$Value,
        [string[]]$Keys
    )

    if ($Value -isnot [psobject]) {
        return "response is not a JSON object"
    }
    foreach ($key in $Keys) {
        if ($null -eq $Value.PSObject.Properties[$key]) {
            return "missing key: $key"
        }
    }
    return $null
}

function Test-HasAnyKey {
    param(
        [object]$Value,
        [string[]]$Keys
    )

    if ($Value -isnot [psobject]) {
        return "response is not a JSON object"
    }
    foreach ($key in $Keys) {
        if ($null -ne $Value.PSObject.Properties[$key]) {
            return $null
        }
    }
    return "missing any expected keys: $($Keys -join ', ')"
}

function Invoke-SmokeEndpoint {
    param(
        [hashtable]$Spec,
        [string]$ResolvedBaseUrl
    )

    $uri = "{0}{1}" -f $ResolvedBaseUrl.TrimEnd("/"), $Spec.Path
    $started = Get-Date

    try {
        if ($Spec.Mode -eq "text") {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -Method $Spec.Method -TimeoutSec 30
            $body = $response.Content
            $statusCode = [int]$response.StatusCode
        } else {
            if ($null -ne $Spec.Body) {
                $jsonBody = $Spec.Body | ConvertTo-Json -Depth 8
                $body = Invoke-RestMethod -Uri $uri -Method $Spec.Method -ContentType "application/json" -Body $jsonBody -TimeoutSec 30
            } else {
                $body = Invoke-RestMethod -Uri $uri -Method $Spec.Method -TimeoutSec 30
            }
            $statusCode = 200
        }

        $detail = $null
        if ($Spec.Mode -eq "text") {
            if ($body -notlike "*$($Spec.Contains)*") {
                $detail = "expected text fragment '$($Spec.Contains)'"
            }
        } elseif ($null -ne $Spec.AllKeys) {
            $detail = Test-HasAllKeys -Value $body -Keys $Spec.AllKeys
        } elseif ($null -ne $Spec.AnyKeys) {
            $detail = Test-HasAnyKey -Value $body -Keys $Spec.AnyKeys
        }

        if ($null -eq $detail) {
            $detail = "ok"
        }

        return [pscustomobject]@{
            Name = $Spec.Name
            Method = $Spec.Method
            Path = $Spec.Path
            Ok = ($detail -eq "ok")
            StatusCode = $statusCode
            DurationMs = [int]((Get-Date) - $started).TotalMilliseconds
            Detail = $detail
        }
    } catch {
        return [pscustomobject]@{
            Name = $Spec.Name
            Method = $Spec.Method
            Path = $Spec.Path
            Ok = $false
            StatusCode = 0
            DurationMs = [int]((Get-Date) - $started).TotalMilliseconds
            Detail = $_.Exception.Message
        }
    }
}

function Show-Diagnostics {
    param(
        [string[]]$ComposeArgs
    )

    Write-Host ""
    Write-Host "Docker compose status:"
    & docker compose @ComposeArgs ps

    Write-Host ""
    Write-Host "Recent API logs:"
    & docker logs stock-analyzer-api --tail 200

    Write-Host ""
    Write-Host "Recent Redis logs:"
    & docker logs stock-analyzer-redis --tail 100
}

$composeArgs = New-ComposeArgs -Root $projectRoot
$baseUrl = $BaseUrl.TrimEnd("/")
$healthUrl = "$baseUrl/health"
$traceId = "docker-smoke-{0}" -f (Get-Date -Format "yyyyMMddHHmmss")

$endpointSpecs = @(
    @{
        Name = "health"
        Method = "GET"
        Path = "/health"
        AllKeys = @("status")
    },
    @{
        Name = "ui_index"
        Method = "GET"
        Path = "/ui/"
        Mode = "text"
        Contains = "/ui/assets/"
    },
    @{
        Name = "dashboard_portfolio"
        Method = "GET"
        Path = "/dashboard/portfolio?days=7&trade_limit=20"
        AllKeys = @("summary", "positions_panel", "recent_trades")
    },
    @{
        Name = "week5_latest"
        Method = "GET"
        Path = "/week5/scan/latest"
        AnyKeys = @("report", "status")
    },
    @{
        Name = "week6_latest"
        Method = "GET"
        Path = "/week6/latest"
        AnyKeys = @("report", "status")
    },
    @{
        Name = "reconcile_latest"
        Method = "GET"
        Path = "/portfolio/reconcile/latest"
        AnyKeys = @("report", "status")
    },
    @{
        Name = "week7_latest"
        Method = "GET"
        Path = "/week7/sim-broker/latest"
        AnyKeys = @("report", "status")
    }
)

if ($IncludeWriteChecks) {
    $endpointSpecs += @(
        @{
            Name = "broker_snapshot"
            Method = "POST"
            Path = "/portfolio/broker_snapshot"
            Body = @{
                positions = @(
                    @{
                        symbol = "600000"
                        target_position = 0.2
                        quantity = 200
                        account = "STAGING"
                    }
                )
                source_trace_id = $traceId
            }
        },
        @{
            Name = "reconcile_run"
            Method = "POST"
            Path = "/portfolio/reconcile/run"
            Body = @{
                now = "2026-03-13T04:10:00"
            }
            AllKeys = @("report")
        },
        @{
            Name = "week7_run"
            Method = "POST"
            Path = "/week7/sim-broker/run"
            Body = @{
                days = 7
                notify_enabled = $false
                export_enabled = $false
                source_trace_id = "$traceId-week7"
            }
            AllKeys = @("status", "summary", "drilldown", "trend")
        },
        @{
            Name = "broker_snapshot_cleanup"
            Method = "POST"
            Path = "/portfolio/broker_snapshot"
            Body = @{
                positions = @()
                source_trace_id = "$traceId-cleanup"
            }
        },
        @{
            Name = "reconcile_run_cleanup"
            Method = "POST"
            Path = "/portfolio/reconcile/run"
            Body = @{
                now = "2026-03-13T04:11:00"
            }
            AllKeys = @("report")
        }
    )
}

try {
    Set-Location $projectRoot
    Assert-DockerAvailable
    Start-ComposeStack -ComposeArgs $composeArgs
    $health = Wait-ForHealth -HealthUrl $healthUrl -TimeoutSec $HealthTimeoutSec

    Write-Host ""
    Write-Host ("Health OK: status={0}" -f $health.status)
    Write-Host ("Base URL: {0}" -f $baseUrl)
    Write-Host ""

    $results = foreach ($spec in $endpointSpecs) {
        Invoke-SmokeEndpoint -Spec $spec -ResolvedBaseUrl $baseUrl
    }

    $results | Format-Table Name, Method, StatusCode, Ok, DurationMs, Detail -AutoSize

    $failures = @($results | Where-Object { -not $_.Ok })
    if ($failures.Count -gt 0) {
        Write-Host ""
        Write-Host ("Smoke test failed: {0} endpoint(s) reported errors." -f $failures.Count)
        Show-Diagnostics -ComposeArgs $composeArgs
        exit 2
    }

    Write-Host ""
    Write-Host "Docker smoke test passed."
    exit 0
} catch {
    Write-Host ""
    Write-Host ("Docker smoke test aborted: {0}" -f $_.Exception.Message)
    Show-Diagnostics -ComposeArgs $composeArgs
    exit 1
}
