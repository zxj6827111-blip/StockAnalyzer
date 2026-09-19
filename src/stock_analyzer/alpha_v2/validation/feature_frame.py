"""Alpha V2 日度原始特征帧（M3 训练/采集共用）。

FeatureEngineer 的单日输出 -> long-frame（``decision_date / symbol / <feature...>``）。
NaN 保留（缺失是信息；LightGBM 原生支持 NaN）。Base V2 准入裁剪发生在调用方
（训练：``fit_frozen_model``；Shadow 采集：``alpha_v2_shadow_capture`` 的显式裁剪）
——本模块不做门禁，只保证"同一份代码产出同一框架的特征格式"。
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd


def daily_feature_frame(panel, decisions: Sequence) -> pd.DataFrame:
    """逐 symbol 的 FeatureEngineer 输出拼成长表（PIT 安全由 S14 登记来保证）。"""
    from stock_analyzer.feature.engineer import FeatureEngineer

    grouped: dict[str, list] = {}
    for item in decisions:
        grouped.setdefault(str(item.symbol), []).append(item)
    engineer = FeatureEngineer()
    rows: list[dict[str, object]] = []
    for symbol in sorted(grouped):
        bars = panel.symbol_bars(symbol)
        if bars is None or bars.empty:
            continue
        wanted = {item.decision_date for item in grouped[symbol]}
        try:
            features = engineer.transform(bars)
        except Exception:  # noqa: BLE001 - 单票失败不吞整批（后续诊断如实缺列）
            continue
        for ts, values in features.iterrows():
            day = ts.date() if hasattr(ts, "date") else ts
            if day not in wanted:
                continue
            row: dict[str, object] = {"decision_date": day.isoformat(), "symbol": symbol}
            for key, value in values.items():
                if isinstance(value, bool):
                    row[str(key)] = float(value)
                    continue
                try:
                    row[str(key)] = float(value)
                except (TypeError, ValueError):
                    row[str(key)] = float("nan")
            rows.append(row)
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["decision_date", "symbol"])


__all__ = ["daily_feature_frame"]
