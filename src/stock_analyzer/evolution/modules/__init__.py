"""Evolution module implementations."""

from stock_analyzer.evolution.modules.m1_case_learning import (
    M1LearningResult,
    run_m1_dual_learning,
)
from stock_analyzer.evolution.modules.m2_regime_hmm import (
    M2OptunaLikeConfig,
    M2OptunaLikeResult,
    M2RegimeResult,
    RegimeInference,
    RegimeModelParams,
    RegimeObservation,
    RegimeSnapshot,
    RegimeStateController,
    evaluate_m2_regime,
    infer_regime,
    tune_regime_with_optuna_like_search,
)
from stock_analyzer.evolution.modules.m3_pattern_memory import (
    PatternAppendResult,
    PatternMemoryStore,
    PatternSearchResult,
)
from stock_analyzer.evolution.modules.m4_capital_flow import (
    M4CapitalFlowResult,
    M4FlowMetrics,
    evaluate_m4_capital_flow,
)
from stock_analyzer.evolution.modules.m5_label_loader import load_m5_label_records
from stock_analyzer.evolution.modules.m5_label_optimization import (
    M5LabelMetrics,
    M5LabelOptimizationResult,
    M5StrategyLinkage,
    build_m5_strategy_linkage,
    evaluate_m5_label_optimization,
)
from stock_analyzer.evolution.modules.m6_counterparty import (
    M6CounterpartyMetrics,
    M6CounterpartyResult,
    evaluate_m6_counterparty,
)
from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records
from stock_analyzer.evolution.modules.m7_event_ledger import (
    M7EventLedger,
    M7EventLedgerEffectivenessSummary,
    M7EventLedgerIngestSummary,
    M7EventLedgerRunReport,
)
from stock_analyzer.evolution.modules.m7_news_sentiment import (
    M7ClusterSummary,
    M7NewsMetrics,
    M7NewsSentimentResult,
    evaluate_m7_news_sentiment,
)
from stock_analyzer.evolution.modules.m8_memory_bridge import (
    M8GateCheck,
    M8Suggestion,
    M8SuggestionResult,
    build_m8_query_vector,
    run_m8_memory_bridge,
)
from stock_analyzer.evolution.modules.m9_data_quality import (
    M9InspectionResult,
    inspect_data_quality,
)
from stock_analyzer.evolution.modules.m10_model_health import (
    M10HealthMetrics,
    M10ModelHealthResult,
    evaluate_m10_model_health,
)
from stock_analyzer.evolution.modules.m11_shadow_loader import (
    M11ShadowObservation,
    load_m11_shadow_observations,
    parse_m11_shadow_records,
)
from stock_analyzer.evolution.modules.m11_shadow_portfolio import (
    M11AttributionItem,
    M11ShadowMetrics,
    M11ShadowResult,
    evaluate_m11_shadow_portfolio,
)
from stock_analyzer.evolution.modules.shadow_online_model import (
    ShadowOnlineMetrics,
    ShadowOnlineResult,
    run_shadow_online_model,
    shadow_online_result_to_dict,
)
from stock_analyzer.evolution.modules.shadow_online_model_v2 import (
    ShadowOnlineV2Metrics,
    ShadowOnlineV2Result,
    run_shadow_online_model_v2,
    score_shadow_online_model_v2_record,
    shadow_online_model_v2_features_from_record,
    shadow_online_v2_result_to_dict,
)

__all__ = [
    "M1LearningResult",
    "M10HealthMetrics",
    "M10ModelHealthResult",
    "M11AttributionItem",
    "M11ShadowObservation",
    "M11ShadowMetrics",
    "M11ShadowResult",
    "ShadowOnlineMetrics",
    "ShadowOnlineResult",
    "ShadowOnlineV2Metrics",
    "ShadowOnlineV2Result",
    "M2OptunaLikeConfig",
    "M2OptunaLikeResult",
    "M2RegimeResult",
    "M9InspectionResult",
    "PatternAppendResult",
    "PatternMemoryStore",
    "PatternSearchResult",
    "M4CapitalFlowResult",
    "M4FlowMetrics",
    "M5LabelMetrics",
    "M5LabelOptimizationResult",
    "M5StrategyLinkage",
    "M6CounterpartyMetrics",
    "M6CounterpartyResult",
    "M7ClusterSummary",
    "M7EventLedger",
    "M7EventLedgerEffectivenessSummary",
    "M7EventLedgerIngestSummary",
    "M7EventLedgerRunReport",
    "M7NewsMetrics",
    "M7NewsSentimentResult",
    "M8GateCheck",
    "M8Suggestion",
    "M8SuggestionResult",
    "RegimeInference",
    "RegimeModelParams",
    "RegimeObservation",
    "RegimeSnapshot",
    "RegimeStateController",
    "build_m8_query_vector",
    "evaluate_m10_model_health",
    "evaluate_m11_shadow_portfolio",
    "evaluate_m4_capital_flow",
    "evaluate_m5_label_optimization",
    "evaluate_m6_counterparty",
    "evaluate_m7_news_sentiment",
    "evaluate_m2_regime",
    "infer_regime",
    "inspect_data_quality",
    "run_m8_memory_bridge",
    "run_m1_dual_learning",
    "build_m5_strategy_linkage",
    "load_m5_label_records",
    "load_m7_news_records",
    "load_m11_shadow_observations",
    "parse_m11_shadow_records",
    "run_shadow_online_model",
    "run_shadow_online_model_v2",
    "score_shadow_online_model_v2_record",
    "shadow_online_model_v2_features_from_record",
    "shadow_online_result_to_dict",
    "shadow_online_v2_result_to_dict",
    "tune_regime_with_optuna_like_search",
]
