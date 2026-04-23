param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

$composeFiles = @(
    (Join-Path $projectRoot "docker-compose.yml"),
    (Join-Path $projectRoot "docker-compose.runtime.yml"),
    (Join-Path $projectRoot "docker-compose.runtime.localvol.yml")
)

$runtimeArtifactsVolume = "stock_analyzer_runtime_artifacts"
$runtimeSuggestionsVolume = "stock_analyzer_runtime_suggestions"
$artifactsSource = Join-Path $projectRoot "artifacts"
$suggestionsSource = Join-Path $projectRoot "suggestions"
$helperImage = "stock-analyzer:latest"

function Invoke-Compose {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Args
    )

    $fileArgs = @()
    foreach ($composeFile in $composeFiles) {
        $fileArgs += "-f"
        $fileArgs += $composeFile
    }

    & docker compose @fileArgs @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($Args -join ' ')"
    }
}

function Ensure-Directory {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Reset-Volume {
    param([string]$VolumeName)

    & docker volume create $VolumeName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "failed to create volume: $VolumeName"
    }

    & docker run --rm -v "${VolumeName}:/target" $helperImage sh -lc "rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null || true"
    if ($LASTEXITCODE -ne 0) {
        throw "failed to reset volume: $VolumeName"
    }
}

function Copy-DirectoryToVolume {
    param(
        [string]$SourcePath,
        [string]$VolumeName
    )

    Ensure-Directory -Path $SourcePath
    $resolvedSource = (Resolve-Path -LiteralPath $SourcePath).Path

    & docker run --rm `
        -v "${resolvedSource}:/source:ro" `
        -v "${VolumeName}:/target" `
        $helperImage sh -lc "cd /source && tar cf - . | tar xf - -C /target"
    if ($LASTEXITCODE -ne 0) {
        throw "failed to copy $SourcePath into volume $VolumeName"
    }
}

Write-Host "Stopping api/scheduler containers before migration..."
Invoke-Compose stop api scheduler

Write-Host "Preparing runtime volumes..."
Reset-Volume -VolumeName $runtimeArtifactsVolume
Reset-Volume -VolumeName $runtimeSuggestionsVolume

Write-Host "Copying artifacts into local Docker volume..."
Copy-DirectoryToVolume -SourcePath $artifactsSource -VolumeName $runtimeArtifactsVolume

Write-Host "Copying suggestions into local Docker volume..."
Copy-DirectoryToVolume -SourcePath $suggestionsSource -VolumeName $runtimeSuggestionsVolume

Write-Host "Starting runtime stack with local Docker volumes..."
$upArgs = @("up", "-d")
if (-not $SkipBuild) {
    $upArgs += "--build"
}
$upArgs += @("api", "scheduler")
Invoke-Compose @upArgs

Write-Host ""
Write-Host "Migration completed."
Write-Host "Runtime artifacts volume: $runtimeArtifactsVolume"
Write-Host "Runtime suggestions volume: $runtimeSuggestionsVolume"
Write-Host "Use scripts/start_runtime_stack_localvol.ps1 for future restarts."
