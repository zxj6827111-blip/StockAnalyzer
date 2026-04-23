"""Off-hours evolution system package."""

from stock_analyzer.evolution.champion_shadow_report import (
    ChampionShadowComparisonReport,
    ChampionShadowComparisonRow,
    ChampionShadowReportBuilder,
)
from stock_analyzer.evolution.core.fusion import FusionResult, ScoreFusionEngine
from stock_analyzer.evolution.core.statistics import (
    BootstrapTestResult,
    FDRCorrectionResult,
    bootstrap_test,
    fdr_correct,
    information_ratio,
)
from stock_analyzer.evolution.m3_vector_profile import (
    M3VectorProfileRecord,
    M3VectorProfileRegistry,
    build_default_m3_vector_profile,
    build_legacy_m3_vector_profile,
    build_m3_vector_from_record,
    build_m3_vector_profile,
)
from stock_analyzer.evolution.modules.m7_event_ledger import (
    M7EventLedger,
    M7EventLedgerEffectivenessSummary,
    M7EventLedgerIngestSummary,
    M7EventLedgerRunReport,
)
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator
from stock_analyzer.evolution.shadow_online_v2_metrics_store import ShadowOnlineV2MetricsStore
from stock_analyzer.evolution.shadow_online_v2_report import (
    ShadowOnlineV2Report,
    ShadowOnlineV2ReportBuilder,
    ShadowOnlineV2ReportRow,
)
from stock_analyzer.evolution.shadow_online_v2_state_store import ShadowOnlineV2StateStore
from stock_analyzer.evolution.shadow_dataset_builder import (
    ShadowDataset,
    ShadowDatasetBuilder,
    ShadowDatasetRow,
)

__all__ = [
    "BootstrapTestResult",
    "ChampionShadowComparisonReport",
    "ChampionShadowComparisonRow",
    "ChampionShadowReportBuilder",
    "FDRCorrectionResult",
    "FusionResult",
    "M3VectorProfileRecord",
    "M3VectorProfileRegistry",
    "M7EventLedger",
    "M7EventLedgerEffectivenessSummary",
    "M7EventLedgerIngestSummary",
    "M7EventLedgerRunReport",
    "OffhoursEvolutionOrchestrator",
    "ScoreFusionEngine",
    "ShadowDataset",
    "ShadowDatasetBuilder",
    "ShadowDatasetRow",
    "ShadowOnlineV2MetricsStore",
    "ShadowOnlineV2Report",
    "ShadowOnlineV2ReportBuilder",
    "ShadowOnlineV2ReportRow",
    "ShadowOnlineV2StateStore",
    "bootstrap_test",
    "build_default_m3_vector_profile",
    "build_legacy_m3_vector_profile",
    "build_m3_vector_from_record",
    "build_m3_vector_profile",
    "fdr_correct",
    "information_ratio",
]
