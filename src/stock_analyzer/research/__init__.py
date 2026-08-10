"""Offline research sidecars for phase D."""

from stock_analyzer.research.alphalens_sidecar import (
    persist_alphalens_sidecar_report,
    run_alphalens_sidecar,
)
from stock_analyzer.research.catboost_shadow import (
    persist_catboost_shadow_report,
    run_catboost_shadow,
)
from stock_analyzer.research.daily_review_report import (
    compute_daily_review_report,
    persist_daily_review_report,
)
from stock_analyzer.research.finbert_sidecar import (
    persist_finbert_sidecar_report,
    run_finbert_sidecar,
)
from stock_analyzer.research.finrl_sidecar import persist_finrl_sidecar_report, run_finrl_sidecar
from stock_analyzer.research.heavy_ts_shadow import (
    persist_heavy_ts_shadow_report,
    run_heavy_ts_shadow,
)
from stock_analyzer.research.ic_decay_report import (
    bucket_ic_series_by_month,
    compute_factor_ic_series,
    compute_ic_decay_from_monthly_ics,
    compute_ic_decay_report,
    persist_ic_decay_report,
)
from stock_analyzer.research.monthly_review_report import (
    compute_discipline_score,
    compute_monthly_review_report,
    persist_monthly_review_report,
)
from stock_analyzer.research.qlib_bridge import export_qlib_bridge_bundle, run_qlib_bridge
from stock_analyzer.research.shap_sidecar import persist_shap_sidecar_report, run_shap_sidecar
from stock_analyzer.research.tabular_deep_shadow import (
    persist_tabular_deep_shadow_report,
    run_tabular_deep_shadow,
)
from stock_analyzer.research.tft_sidecar import persist_tft_sidecar_report, run_tft_sidecar

__all__ = [
    "bucket_ic_series_by_month",
    "compute_daily_review_report",
    "compute_discipline_score",
    "compute_factor_ic_series",
    "compute_ic_decay_from_monthly_ics",
    "compute_ic_decay_report",
    "compute_monthly_review_report",
    "export_qlib_bridge_bundle",
    "persist_alphalens_sidecar_report",
    "persist_catboost_shadow_report",
    "persist_daily_review_report",
    "persist_finbert_sidecar_report",
    "persist_finrl_sidecar_report",
    "persist_heavy_ts_shadow_report",
    "persist_ic_decay_report",
    "persist_monthly_review_report",
    "persist_shap_sidecar_report",
    "persist_tabular_deep_shadow_report",
    "persist_tft_sidecar_report",
    "run_alphalens_sidecar",
    "run_catboost_shadow",
    "run_finbert_sidecar",
    "run_finrl_sidecar",
    "run_heavy_ts_shadow",
    "run_qlib_bridge",
    "run_shap_sidecar",
    "run_tabular_deep_shadow",
    "run_tft_sidecar",
]
