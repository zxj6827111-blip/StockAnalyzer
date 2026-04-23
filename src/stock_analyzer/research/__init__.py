"""Offline research sidecars for phase D."""

from stock_analyzer.research.alphalens_sidecar import (
    persist_alphalens_sidecar_report,
    run_alphalens_sidecar,
)
from stock_analyzer.research.catboost_shadow import (
    persist_catboost_shadow_report,
    run_catboost_shadow,
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
from stock_analyzer.research.qlib_bridge import export_qlib_bridge_bundle, run_qlib_bridge
from stock_analyzer.research.shap_sidecar import persist_shap_sidecar_report, run_shap_sidecar
from stock_analyzer.research.tabular_deep_shadow import (
    persist_tabular_deep_shadow_report,
    run_tabular_deep_shadow,
)
from stock_analyzer.research.tft_sidecar import persist_tft_sidecar_report, run_tft_sidecar

__all__ = [
    "export_qlib_bridge_bundle",
    "persist_alphalens_sidecar_report",
    "persist_catboost_shadow_report",
    "persist_finbert_sidecar_report",
    "persist_finrl_sidecar_report",
    "persist_heavy_ts_shadow_report",
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
