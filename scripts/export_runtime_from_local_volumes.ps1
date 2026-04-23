param()

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

$runtimeArtifactsVolume = "stock_analyzer_runtime_artifacts"
$runtimeSuggestionsVolume = "stock_analyzer_runtime_suggestions"
$artifactsTarget = Join-Path $projectRoot "artifacts"
$suggestionsTarget = Join-Path $projectRoot "suggestions"
$helperImage = "stock-analyzer:latest"

function Ensure-Directory {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Reset-DirectoryContents {
    param([string]$Path)

    Ensure-Directory -Path $Path
    Get-ChildItem -LiteralPath $Path -Force | Remove-Item -Recurse -Force
}

function Copy-VolumeToDirectory {
    param(
        [string]$VolumeName,
        [string]$TargetPath
    )

    Ensure-Directory -Path $TargetPath
    $resolvedTarget = (Resolve-Path -LiteralPath $TargetPath).Path

    & docker run --rm `
        -v "${VolumeName}:/source:ro" `
        -v "${resolvedTarget}:/target" `
        $helperImage sh -lc "cd /source && tar cf - . | tar xf - -C /target"
    if ($LASTEXITCODE -ne 0) {
        throw "failed to export volume $VolumeName into $TargetPath"
    }
}

Write-Host "Exporting runtime artifacts volume to host..."
Reset-DirectoryContents -Path $artifactsTarget
Copy-VolumeToDirectory -VolumeName $runtimeArtifactsVolume -TargetPath $artifactsTarget

Write-Host "Exporting runtime suggestions volume to host..."
Reset-DirectoryContents -Path $suggestionsTarget
Copy-VolumeToDirectory -VolumeName $runtimeSuggestionsVolume -TargetPath $suggestionsTarget

Write-Host ""
Write-Host "Export completed."
