param(
    [string]$Symbol = "600000",
    [int]$LookbackDays = 320,
    [string]$BaselineOutputPath = "",
    [string]$V13OutputPath = "",
    [string]$GateOutputPath = ""
)

$ErrorActionPreference = "Stop"

python -m pytest -q -p no:cacheprovider tests/test_service_closed_loop_flow.py

$bundleArgs = @(
    "-m", "stock_analyzer.cli",
    "acceptance-bundle",
    "--symbol", $Symbol,
    "--lookback-days", "$LookbackDays"
)
if ($BaselineOutputPath) {
    $bundleArgs += @("--baseline-output-path", $BaselineOutputPath)
}
if ($V13OutputPath) {
    $bundleArgs += @("--v13-output-path", $V13OutputPath)
}
python @bundleArgs | Out-Null

$gateArgs = @(
    "-m", "stock_analyzer.cli",
    "acceptance-release-gate",
    "--closed-loop-smoke-passed",
    "--closed-loop-smoke-detail", "pytest tests/test_service_closed_loop_flow.py",
    "--fail-on-blocked"
)
if ($V13OutputPath) {
    $gateArgs += @("--v13-report-path", $V13OutputPath)
}
if ($GateOutputPath) {
    $gateArgs += @("--output-path", $GateOutputPath)
}
python @gateArgs
