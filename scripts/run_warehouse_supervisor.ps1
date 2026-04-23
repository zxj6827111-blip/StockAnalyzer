$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot "artifacts\\runtime"
$statePath = Join-Path $runtimeDir "warehouse_followup_supervisor_state.json"
$followupLogPath = Join-Path $runtimeDir "post_warehouse_followup_exec.log"
$followupScriptPath = Join-Path $repoRoot "scripts\\run_post_warehouse_followup.py"
$apiContainer = "stock-analyzer-api"
$statusUrl = "http://127.0.0.1:8001/warehouse/sync/status"
$pollSeconds = 60
$maxResumeRuns = 8
$maxRetryRuns = 6

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Write-State {
    param(
        [string]$Phase,
        [string]$Status,
        [hashtable]$Payload = @{}
    )

    $state = @{
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
        phase = $Phase
        status = $Status
        payload = $Payload
    }
    $state | ConvertTo-Json -Depth 8 | Set-Content -Path $statePath -Encoding UTF8
}

function Get-WarehouseStatus {
    return Invoke-RestMethod -Uri $statusUrl -TimeoutSec 20
}

function Get-PropValue {
    param(
        $Object,
        [string]$Name,
        $Default = $null
    )

    if ($null -eq $Object) {
        return $Default
    }

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    if ($null -eq $property.Value) {
        return $Default
    }

    return $property.Value
}

function Start-WarehouseSync {
    param(
        [string]$TraceId,
        [switch]$RetryFailedOnly,
        [string]$RetryReportTraceId = ""
    )

    $command = "python -u -m stock_analyzer.cli warehouse-sync-run --source-trace-id $TraceId"
    if ($RetryFailedOnly.IsPresent) {
        if (-not $RetryReportTraceId) {
            throw "RetryReportTraceId is required when RetryFailedOnly is set."
        }
        $command += " --retry-failed-only --retry-report-trace-id $RetryReportTraceId"
    }
    $command += " > /app/artifacts/runtime/$TraceId.log 2>&1"

    & docker exec -d $apiContainer sh -lc $command | Out-Null
}

function Start-ResumeRun {
    param([int]$Attempt)
    $traceId = "manual-warehouse-supervisor-full-{0}-{1:00}" -f (Get-Date -Format "yyyyMMddHHmmss"), $Attempt
    Write-State -Phase "warehouse_resume" -Status "starting" -Payload @{
        trace_id = $traceId
        attempt = $Attempt
    }
    Start-WarehouseSync -TraceId $traceId
    Start-Sleep -Seconds 12
}

function Start-RetryRun {
    param(
        [string]$SourceTraceId,
        [int]$Attempt
    )
    $traceId = "manual-warehouse-supervisor-retry-{0}-{1:00}" -f (Get-Date -Format "yyyyMMddHHmmss"), $Attempt
    Write-State -Phase "warehouse_retry_failed" -Status "starting" -Payload @{
        trace_id = $traceId
        retry_report_trace_id = $SourceTraceId
        attempt = $Attempt
    }
    Start-WarehouseSync -TraceId $traceId -RetryFailedOnly -RetryReportTraceId $SourceTraceId
    Start-Sleep -Seconds 12
}

$resumeRuns = 0
$retryRuns = 0

while ($true) {
    try {
        $status = Get-WarehouseStatus
    }
    catch {
        Write-State -Phase "warehouse_wait_api" -Status "waiting" -Payload @{
            error = $_.Exception.Message
        }
        Start-Sleep -Seconds 30
        continue
    }

    $progress = Get-PropValue -Object $status -Name "progress" -Default @{}
    $lock = Get-PropValue -Object $status -Name "lock" -Default @{}
    $background = Get-PropValue -Object $status -Name "background_data" -Default $null
    if ($null -eq $background) {
        $report = Get-PropValue -Object $status -Name "report" -Default @{}
        $background = Get-PropValue -Object $report -Name "background_data" -Default @{}
    }

    $progressStatus = [string](Get-PropValue -Object $progress -Name "status" -Default "")
    $traceId = [string](Get-PropValue -Object $progress -Name "trace_id" -Default "")
    $lockRunning = [bool](Get-PropValue -Object $lock -Name "running" -Default $false)
    $lockStale = [bool](Get-PropValue -Object $lock -Name "is_stale" -Default $false)
    $failedSymbols = [int](Get-PropValue -Object $progress -Name "failed_symbols_total" -Default 0)
    $symbolsCompleted = [int](Get-PropValue -Object $progress -Name "symbols_completed" -Default 0)
    $symbolsTotal = [int](Get-PropValue -Object $progress -Name "symbols_total" -Default 0)
    $coverageRatio = [double](Get-PropValue -Object $background -Name "latest_trade_date_coverage_ratio" -Default 0.0)
    $symbolsOnLatestTradeDate = [int](Get-PropValue -Object $background -Name "symbols_on_latest_trade_date" -Default 0)
    $symbolsStale = [int](Get-PropValue -Object $background -Name "symbols_stale" -Default 0)
    $latestTradeDate = [string](Get-PropValue -Object $background -Name "latest_trade_date" -Default "")

    if ($lockRunning) {
        Write-State -Phase "warehouse_sync" -Status "running" -Payload @{
            trace_id = $traceId
            current_symbol = [string]$progress.current_symbol
            symbols_completed = $symbolsCompleted
            symbols_total = $symbolsTotal
            failed_symbols_total = $failedSymbols
            latest_trade_date = $latestTradeDate
            symbols_on_latest_trade_date = $symbolsOnLatestTradeDate
            coverage_ratio = $coverageRatio
        }
        Start-Sleep -Seconds $pollSeconds
        continue
    }

    if ($progressStatus -eq "completed") {
        if ($failedSymbols -gt 0 -and $retryRuns -lt $maxRetryRuns) {
            $retryRuns += 1
            Start-RetryRun -SourceTraceId $traceId -Attempt $retryRuns
            continue
        }
        Write-State -Phase "warehouse_sync" -Status "completed" -Payload @{
            trace_id = $traceId
            latest_trade_date = $latestTradeDate
            symbols_on_latest_trade_date = $symbolsOnLatestTradeDate
            coverage_ratio = $coverageRatio
            symbols_stale = $symbolsStale
            failed_symbols_total = $failedSymbols
        }
        break
    }

    if (($progressStatus -eq "running" -and $lockStale) -or $progressStatus -eq "failed" -or [string]::IsNullOrWhiteSpace($progressStatus)) {
        if ($resumeRuns -ge $maxResumeRuns) {
            throw "warehouse_sync_resume_exhausted"
        }
        $resumeRuns += 1
        Start-ResumeRun -Attempt $resumeRuns
        continue
    }

    if ($coverageRatio -lt 0.999 -or $symbolsStale -gt 0) {
        if ($resumeRuns -ge $maxResumeRuns) {
            throw "warehouse_sync_incomplete_after_max_resume_runs"
        }
        $resumeRuns += 1
        Start-ResumeRun -Attempt $resumeRuns
        continue
    }

    Write-State -Phase "warehouse_sync" -Status "completed" -Payload @{
        trace_id = $traceId
        latest_trade_date = $latestTradeDate
        symbols_on_latest_trade_date = $symbolsOnLatestTradeDate
        coverage_ratio = $coverageRatio
        symbols_stale = $symbolsStale
        failed_symbols_total = $failedSymbols
    }
    break
}

Write-State -Phase "post_warehouse_followup" -Status "running"
& docker cp $followupScriptPath "${apiContainer}:/app/scripts/run_post_warehouse_followup.py" | Out-Null
$followupOutput = & docker exec $apiContainer python /app/scripts/run_post_warehouse_followup.py 2>&1
$followupOutput | Out-File -FilePath $followupLogPath -Encoding utf8
if ($LASTEXITCODE -ne 0) {
    Write-State -Phase "post_warehouse_followup" -Status "failed" -Payload @{
        log_path = $followupLogPath
        exit_code = $LASTEXITCODE
    }
    throw "post_warehouse_followup_failed"
}

Write-State -Phase "post_warehouse_followup" -Status "completed" -Payload @{
    log_path = $followupLogPath
}
