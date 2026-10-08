"""把独立补采的流通市值真值接到研究侧消费点（改进计划 §3.1 / §3j）。

为什么要有这一层：仓库 ``daily_bars.float_market_cap`` 自 2026-03 中旬起被 provider 的
兜底常数 12,000,000,000 填掉（根因见 ADR-004 §3，缺陷记录 NOTE-002 D15），
硬性资格门 ``min_float_market_cap`` 的阈值又按同一列取分位 —— 列成常数时阈值=众数，
那条门跑完了却谁都不淘汰。读取侧的带版本解释规则只能把它标成"没测过"，
要真的重算"这条门本来会淘汰谁"，必须有**另一个来源**的市值。

真值落在研究库的独立表里（``float_market_cap_ref``，由
``scripts/load_float_market_cap_research.py`` 写入），仓库那些占位行原地不动 ——
§3.1 要求旧记录保留原始值，占位行本身是缺陷的证据。

这里只提供一个动作：把 frame 的 ``float_market_cap`` 在**能对上的 symbol-day** 上换成真值，
并把"换了多少、对不上多少、两边都声称测过却差出 1% 的有多少"如实返回。
对不上的行保留原值，不装作有数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from stock_analyzer.feature.trend_candidate_contract import (
    unproven_float_market_cap_mask,
)

TABLE = "float_market_cap_ref"

SOURCE_LABEL = "tushare_daily_basic_circ_mv_research_reference"

#: 与真值差多少算"实质不同"：1% 以上。低于它是万元→元换算后的舍入噪声
#: （接口侧 circ_mv 只有四位小数），按 1e-6 判会把舍入读成口径冲突。
MEANINGFUL_DIFF_RATIO = 0.01

#: 占位行占到多少比例以上，就认为这一天的市值门整体无从判定（与就绪审计
#: ``MAX_MODAL_VALUE_SHARE`` 同一个门槛）。
NON_EVALUABLE_PLACEHOLDER_SHARE = 0.5


def read_reference(db: Path | str) -> pd.DataFrame:
    """读研究库的真值表；空表直接报错，不让调用方拿空帧算出"全都补不上"的假结论。"""
    con = duckdb.connect(str(db), read_only=True)
    try:
        frame = con.execute(
            f"SELECT symbol, CAST(trade_date AS VARCHAR) AS trade_date, float_market_cap "
            f"FROM {TABLE}"
        ).fetch_df()
    finally:
        con.close()
    if frame.empty:
        raise SystemExit(f"reference table {TABLE} is empty: {db}")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def apply_float_cap_reference(
    frame: pd.DataFrame, ref_db: Path | str, *, symbol_col: str = "symbol",
    date_col: str = "date", cap_col: str = "float_market_cap",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """在对得上的 symbol-day 上用真值替换 ``cap_col``，并返回可写进报告的计数。

    替换本身不改判定顺序，只改输入值；调用方仍按契约的归因顺序跑各条门。
    """
    ref = read_reference(ref_db)
    wanted = ref.rename(columns={"trade_date": f"__ref_{date_col}",
                                 "float_market_cap": "cap_ref"})
    merged = frame.merge(
        wanted[["symbol", f"__ref_{date_col}", "cap_ref"]],
        left_on=[symbol_col, date_col],
        right_on=["symbol", f"__ref_{date_col}"],
        how="left",
    )
    if len(merged) != len(frame):
        raise SystemExit(
            "reference join multiplied rows — "
            f"{TABLE} is not unique by symbol+trade_date"
        )
    before = pd.to_numeric(merged[cap_col], errors="coerce")
    hit = merged["cap_ref"].notna()
    placeholder_before = unproven_float_market_cap_mask(before)
    after = merged["cap_ref"].where(hit, before)
    merged[cap_col] = after
    both_measured = hit & ~placeholder_before
    stats: dict[str, Any] = {
        "ref_db": str(ref_db),
        "source": SOURCE_LABEL,
        "rows_total": int(len(merged)),
        "rows_with_reference": int(hit.sum()),
        "rows_replaced_from_placeholder": int((hit & placeholder_before).sum()),
        "rows_left_placeholder_without_reference": int(
            (~hit & placeholder_before).sum()
        ),
        "rows_both_claim_measured": int(both_measured.sum()),
        "rows_measured_differing_beyond_1pct": int(
            (both_measured & ((before / after).sub(1.0).abs() > MEANINGFUL_DIFF_RATIO)).sum()
        ),
        "placeholder_share_before": round(
            float(placeholder_before.mean()) if len(merged) else 0.0, 6
        ),
        "placeholder_share_after": round(
            float(unproven_float_market_cap_mask(after).mean()) if len(merged) else 0.0, 6
        ),
    }
    stats["gate_non_evaluable_before"] = bool(
        stats["placeholder_share_before"] >= NON_EVALUABLE_PLACEHOLDER_SHARE
    )
    stats["gate_non_evaluable_after"] = bool(
        stats["placeholder_share_after"] >= NON_EVALUABLE_PLACEHOLDER_SHARE
    )
    # 只丢连接用的辅助列；symbol/date 是原帧就有的，留着给调用方继续判定。
    return merged.drop(columns=["cap_ref", f"__ref_{date_col}"]), stats


__all__ = [
    "MEANINGFUL_DIFF_RATIO",
    "NON_EVALUABLE_PLACEHOLDER_SHARE",
    "SOURCE_LABEL",
    "TABLE",
    "apply_float_cap_reference",
    "read_reference",
]
