#!/usr/bin/env python3
"""把补采到的流通市值真值装进**独立研究库**，并与仓库里的可疑副本对账。

为什么单独一张表、不直接改写 ``daily_bars``：改进计划 §3.1 要求"旧记录保留原始值，
通过带版本的解释规则兼容"，ADR-004 §3 也定了读取侧口径 —— 生产/仓库副本里那些
被填成常数 12,000,000,000 的行是缺陷的**证据**，抹掉它们等于把案发现场擦干净。
真值放在 ``float_market_cap_ref``，由消费方显式加入并记录自己用了哪一版口径。

单位：tushare ``daily_basic.circ_mv`` 是**万元**，这里统一换算成**元**再落表，
和 ``daily_bars.float_market_cap`` 同口径，否则市值门阈值会差一万倍。

对账是这张表的验收条件，不是附加品：一月份仓库副本里只有 4.7% 是占位常数，
那 95.3% 是独立来源的真值 —— 两边对得上，才说明采集链路本身没算错单位、
没搞错 ts_code 到 symbol 的映射。

用法::

    python scripts/load_float_market_cap_research.py \\
        --csv artifacts/research/float_cap_20260105_20260717.csv.gz \\
        --out-db artifacts/research/float_cap_reference.duckdb \\
        --warehouse artifacts/research/market_copy.duckdb \\
        --report artifacts/research/float_cap_reference_report.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

#: 与 provider 兜底常数同一事实，见 ADR-004 §3。
UNPROVEN_PLACEHOLDER = 12_000_000_000.0

TABLE = "float_market_cap_ref"

SOURCE_LABEL = "tushare_daily_basic_circ_mv"

WAN_TO_YUAN = 10_000.0

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol VARCHAR,
    trade_date DATE,
    float_market_cap DOUBLE,
    circ_mv_wan DOUBLE,
    ts_code VARCHAR,
    source VARCHAR,
    collected_at TIMESTAMP
)
"""


def normalize(frame: pd.DataFrame) -> pd.DataFrame:
    """CSV → 落表用的帧：单位换算、ts_code→symbol、剔除不可用值。

    ``symbol`` 存 6 位代码而不是 ``600000.SH``：仓库 ``daily_bars.symbol`` 用的就是
    6 位（实测 ``603056``），两边口径不一致时 join 会静默返回 0 行 —— 对账阶段
    就是这么发现的，所以这里把 ts_code 单独留一列备查。
    """
    out = frame.copy()
    out["ts_code"] = out["ts_code"].astype(str).str.strip()
    out["symbol"] = out["ts_code"].str.split(".").str[0]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.date
    circ = pd.to_numeric(out["circ_mv"], errors="coerce")
    out["circ_mv_wan"] = circ
    out["float_market_cap"] = circ * WAN_TO_YUAN
    return out


def audit_frames(
    ref: pd.DataFrame, warehouse: str | Path | None, months: list[str]
) -> dict[str, Any]:
    """按月份把真值与仓库副本对账。

    ``agreement_rate`` 只在**副本不是占位常数**的行上算 —— 那些行是独立来源的
    真值，两边应当逐分对齐；占位行不参与一致率，否则等于用假数据给自己打分。
    """
    if warehouse is None:
        return {"warehouse": None, "months": {}}
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        stored = con.execute(
            """
            SELECT symbol, CAST(date AS DATE) AS trade_date, float_market_cap
            FROM daily_bars
            WHERE CAST(date AS DATE) BETWEEN ? AND ?
            """,
            [
                min(ref["trade_date"]),
                max(ref["trade_date"]),
            ],
        ).df()
    finally:
        con.close()
    stored["float_market_cap"] = pd.to_numeric(
        stored["float_market_cap"], errors="coerce"
    )
    # 两边都转成 YYYY-MM-DD 字符串再 join：duckdb 给回的是 datetime64，
    # 采集侧落的是 python date，混着 join 会被 pandas 判成类型不兼容。
    stored["trade_date"] = pd.to_datetime(stored["trade_date"]).dt.strftime("%Y-%m-%d")
    ref = ref.assign(
        trade_date=pd.to_datetime(ref["trade_date"].astype(str)).dt.strftime("%Y-%m-%d")
    )
    joined = stored.merge(
        ref[["symbol", "trade_date", "float_market_cap"]],
        on=["symbol", "trade_date"],
        how="inner",
        suffixes=("_stored", "_ref"),
    )
    joined["month"] = joined["trade_date"].str[:7]
    joined["is_placeholder"] = joined["float_market_cap_stored"].round(6).eq(
        UNPROVEN_PLACEHOLDER
    )
    rel = (joined["float_market_cap_stored"] - joined["float_market_cap_ref"]).abs() / (
        joined["float_market_cap_ref"].abs()
    )
    # 1% 而不是逐位相等：circ_mv 在接口侧就是四位小数的万元，换算成元后
    # 天然带 1e-5 量级的舍入差；按 1e-6 判"不一致"会把舍入读成口径冲突。
    joined["matches_ref"] = rel.lt(0.01)

    per_month: dict[str, Any] = {}
    for month, group in joined.groupby("month"):
        measured = group.loc[~group["is_placeholder"]]
        per_month[str(month)] = {
            "symbol_days_joined": int(len(group)),
            "stored_placeholder_rows": int(group["is_placeholder"].sum()),
            "stored_placeholder_share": round(
                float(group["is_placeholder"].mean()) if len(group) else 0.0, 6
            ),
            "measured_rows": int(len(measured)),
            "measured_agreeing_with_reference": int(measured["matches_ref"].sum()),
            "measured_agreement_rate": round(
                float(measured["matches_ref"].mean()) if len(measured) else 0.0, 6
            ),
        }
    return {
        "warehouse": str(warehouse),
        "join_key": "symbol + trade_date",
        "stored_rows_in_window": int(len(stored)),
        "joined_rows": int(len(joined)),
        "months": per_month,
        "months_expected": months,
        "symbol_days_without_reference": int(
            len(stored) - len(joined)
        ),
        # 一行都对不上=连接键口径不一致（多半是 6 位代码 vs ts_code 后缀），
        # 绝不能让它安静地输出一份空 months 表。
        "warning": (
            "join_returned_zero_rows_check_symbol_format"
            if len(stored) and not len(joined)
            else ""
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--out-db", default="artifacts/research/float_cap_reference.duckdb")
    parser.add_argument("--warehouse", default="", help="研究库副本（只读，用于对账）")
    parser.add_argument("--report", default="")
    parser.add_argument("--replace", action="store_true", help="重建整张表")
    args = parser.parse_args(argv)

    raw = pd.read_csv(args.csv, compression="infer")
    ref = normalize(raw)

    unusable = {
        "rows_in": int(len(ref)),
        "rows_missing_date": int(ref["trade_date"].isna().sum()),
        "rows_missing_or_nonpositive_circ_mv": int(
            ((~ref["float_market_cap"].notna()) | (ref["float_market_cap"] <= 0.0)).sum()
        ),
    }
    ref = ref.loc[
        ref["trade_date"].notna()
        & ref["float_market_cap"].notna()
        & (ref["float_market_cap"] > 0.0)
    ].copy()
    ref = ref.drop_duplicates(subset=["symbol", "trade_date"], keep="last")

    unusable["rows_kept"] = int(len(ref))
    unusable["rows_dropped_as_unusable"] = unusable["rows_in"] - unusable["rows_kept"]

    collected_at = datetime.now(UTC).replace(tzinfo=None)
    ref["source"] = SOURCE_LABEL
    ref["collected_at"] = collected_at

    out_path = Path(args.out_db)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(out_path))
    try:
        if args.replace:
            con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        con.execute(DDL)
        con.register("ref_frame", ref[
            ["symbol", "trade_date", "float_market_cap", "circ_mv_wan",
             "ts_code", "source", "collected_at"]
        ])
        con.execute(
            f"INSERT INTO {TABLE} SELECT * FROM ref_frame"
        )
        stored_rows = int(con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
        per_day = con.execute(
            f"SELECT CAST(trade_date AS VARCHAR) d, COUNT(*) c FROM {TABLE} GROUP BY d"
        ).df()
    finally:
        con.close()

    months = sorted({str(item)[:7] for item in per_day["d"]})
    summary: dict[str, Any] = {
        "csv": str(args.csv),
        "out_db": str(out_path),
        "table": TABLE,
        "source": SOURCE_LABEL,
        "unit_conversion": "circ_mv 万元 * 10000 -> float_market_cap 元",
        "rows_written_total": stored_rows,
        "days": int(per_day["d"].nunique()),
        "day_range": [str(per_day["d"].min()), str(per_day["d"].max())],
        "symbols_distinct": int(ref["symbol"].nunique()),
        "rows_per_day_min": int(per_day["c"].min()),
        "rows_per_day_max": int(per_day["c"].max()),
        "months": months,
        "integrity": unusable,
        "placeholder_rows_in_reference": int(
            (ref["float_market_cap"].round(6) == UNPROVEN_PLACEHOLDER).sum()
        ),
    }
    if args.warehouse:
        summary["reconciliation"] = audit_frames(ref, args.warehouse, months)

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if args.report:
        Path(args.report).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    # 一天都没落表 = 采集或转换链路坏了，必须非零退出。
    return 0 if stored_rows and summary["days"] else 6


if __name__ == "__main__":
    raise SystemExit(main())
