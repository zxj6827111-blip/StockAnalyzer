"""P3.3 —— vendor 缺行的 **repair / supplemental layer**（原始 ZIP 保持 immutable）。

为什么是"补一层"而不是"改 ZIP"：``全A日K/2025.zip`` 现在是**原始来源证据**，它的
sha256 就是"上游确实少交付了这批 bar"这件事的凭证。改它就等于把事故现场擦掉，
之后再也分不清"vendor 没有"和"我们弄丢了"。

两类缺口，两个来源，**风险完全不同**，所以分开建：

```text
A. UPSTREAM_SOURCE_GAP（2025-11-11/12/13/17/18、12-24）
   vendor ZIP 没有 → RAW 与 QFQ 两侧都没有 → 只能引外部来源（Tushare）
   语义已实测：OHLC 逐值全等，volume = vol × 100，turnover = amount × 1000
   （4 个对照日 × 5,438 票，dominant_share = 1.0，max_abs_rel = 0.0）

B. FEATURE_DERIVATION_DROP（2026-07-17..07-30，295 键）
   RAW 有 bar、QFQ 没有 → **不需要任何外部来源**：
   qfq = raw × 复权因子，891/891 条对照逐值全等（最大相对差 5.4e-11，纯浮点噪声）
```

三条硬约束（都由测试钉住，不是文档承诺）：

1. **insert-missing-only**：``(symbol, trade_date)`` 已在 delta 里就一律不写。
   落库走 :meth:`MarketWarehouse.upsert_daily_bars` 的默认
   ``overwrite_existing=False``（ANTI JOIN），与 delta 自身的合并规则同一份实现，
   避免"两个入口对'赢的一方'判断不一致"。
2. **可逆**：每一条 repair 行都在 ``daily_bar_repairs`` 里登记主键 + 批次 + 内容哈希，
   按 ``repair_batch_id`` 精确删除；不删任何未登记的行。
3. **不伪装成 vendor 原始数据**：repair 行的 ``adjustment_source`` 用独立取值，
   该列**不在** ``PANEL_BAR_COLUMNS`` / 训练数据指纹的哈希列里，所以既不改变
   freeze 读到的口径，也不会被误当成 vendor 交付。⚠️ 反过来，**新增行本身**一定会
   改变 ``compute_training_data_fingerprint`` ——那是正确行为（训练输入真的变了），
   但它意味着任何在修复前封存的工件身份都会失配，必须先确认再重冻。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import pandas as pd

#: 只有这四个价格列参与复权（与 ``vendor_zip_overlay._normalize_vendor_daily`` 一致：
#: volume / turnover **不**乘因子，实测两侧逐日 min/max 完全相同）。
REPAIR_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

REPAIR_SCHEMA_VERSION = 1
REPAIR_PROVENANCE_TABLE = "daily_bar_repairs"

#: 两类缺口的原因码（审计与 Note 里按这两个名字取用）。
REPAIR_REASON_SOURCE_GAP = "UPSTREAM_SOURCE_GAP"
REPAIR_REASON_FEATURE_DROP = "FEATURE_DERIVATION_DROP"

#: 独立来源与自建派生各自的 ``adjustment_source`` 标记值。
ADJUSTMENT_SOURCE_REPAIR_FROM_TUSHARE = "tushare_repair_supplemental"
ADJUSTMENT_SOURCE_REPAIR_QFQ_DERIVED = "local_vendor_qfq_repaired"

#: 补写行必须带的 bar 列（缺一个都会在面板侧变成 NULL，见 ``panel.PANEL_BAR_COLUMNS``）。
REPAIR_BAR_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "board",
    "is_st",
    "is_delisting_risk",
    "suspended",
    "price_series_mode",
    "adjustment_source",
)

PROVENANCE_DDL = f"""
CREATE TABLE IF NOT EXISTS {REPAIR_PROVENANCE_TABLE} (
    repair_batch_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    trade_date VARCHAR NOT NULL,
    price_series_mode VARCHAR NOT NULL,
    repair_source VARCHAR NOT NULL,
    source_file VARCHAR,
    source_query_time VARCHAR,
    source_row_hash VARCHAR,
    stored_row_hash VARCHAR,
    vendor_original_missing BOOLEAN,
    repair_reason VARCHAR,
    verified_by VARCHAR,
    schema_version INTEGER,
    created_at VARCHAR,
    PRIMARY KEY (repair_batch_id, symbol, trade_date, price_series_mode)
)
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_content_hash(values: Mapping[str, object]) -> str:
    """对一行的**语义内容**取哈希（键排序、浮点按 12 位定点化，不依赖平台 repr）。

    日期必须归一化：计划侧拿的是 ``"2025-11-17"``，回读时驱动可能给 ``Timestamp`` /
    ``datetime`` / ``date``。不统一成 ISO 日期的话，:func:`verify_repairs` 会把每一行
    都报成"漂移"——哈希一旦依赖取值路径，它就不是内容哈希了。
    """
    normalized: dict[str, object] = {}
    for key in sorted(values):
        value = values[key]
        if isinstance(value, float):
            normalized[str(key)] = f"{value:.12f}"
        elif hasattr(value, "date") and callable(getattr(value, "date", None)):
            normalized[str(key)] = str(value.date().isoformat())
        else:
            normalized[str(key)] = value
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RepairProvenance:
    """一批 repair 的来源证据。缺 any 一项就不许落库（见 :func:`validate_provenance`）。"""

    repair_batch_id: str
    repair_source: str
    repair_reason: str
    verified_by: str
    source_file: str = ""
    source_query_time: str = ""
    vendor_original_missing: bool = True

    def as_row_fields(self) -> dict[str, object]:
        return asdict(self)


def validate_provenance(provenance: RepairProvenance) -> None:
    for field in ("repair_batch_id", "repair_source", "repair_reason", "verified_by"):
        if not str(getattr(provenance, field) or "").strip():
            raise ValueError(f"repair provenance field {field!r} must not be empty")
    if provenance.repair_reason not in (
        REPAIR_REASON_SOURCE_GAP,
        REPAIR_REASON_FEATURE_DROP,
    ):
        raise ValueError(f"unknown repair_reason {provenance.repair_reason!r}")


def derive_qfq_from_raw(
    raw_prices: Mapping[str, float], *, factor: float
) -> dict[str, float]:
    """``qfq_price = raw_price * factor``——与 delta 里已存的 QFQ 同一算法。

    该式在 891/891 条生产对照行上逐值成立（P3.3 实测）。因子必须为正：
    非正是 ``_parse_vendor_factor_frame`` 的 fail-closed 条件，这里同样拒绝。
    """
    if not factor > 0:
        raise ValueError(f"qfq factor must be strictly positive, got {factor!r}")
    missing = [column for column in REPAIR_PRICE_COLUMNS if raw_prices.get(column) is None]
    if missing:
        raise ValueError(f"raw bar missing price columns: {missing}")
    return {column: float(raw_prices[column]) * factor for column in REPAIR_PRICE_COLUMNS}


def plan_insert_missing_only(
    *,
    incumbent_keys: Iterable[tuple[str, str]],
    candidate_rows: Sequence[Mapping[str, object]],
    price_series_mode: str,
    provenance: RepairProvenance,
) -> tuple[list[dict[str, object]], int]:
    """把候选行裁成**只补缺口**：已存在的 ``(symbol, date)`` 一律不进计划。

    返回 ``(planned_rows, dropped_because_present)``。第二个返回值必须被记进证据——
    "repair 源对同一天也有另一套值"是真实会发生的，静默丢弃它就是把覆盖藏进计划里。
    """
    validate_provenance(provenance)
    present = set(incumbent_keys)
    planned: list[dict[str, object]] = []
    skipped = 0
    for row in candidate_rows:
        symbol = str(row.get("symbol") or "").strip()
        date = str(row.get("date") or "").strip()
        if not symbol or not date:
            raise ValueError(f"repair candidate missing symbol/date: {row!r}")
        if (symbol, date) in present:
            skipped += 1
            continue
        stored = dict(row)
        stored["price_series_mode"] = str(price_series_mode)
        stored["adjustment_source"] = str(provenance.repair_source)
        planned.append(stored)
    return planned, skipped


def provenance_rows(
    *,
    planned_rows: Sequence[Mapping[str, object]],
    price_series_mode: str,
    provenance: RepairProvenance,
    created_at: str | None = None,
) -> pd.DataFrame:
    """给每条补写行配上可审计的身份（任务书 §5.2 要求的字段一个都不能少）。"""
    stamp = created_at or utc_now_iso()
    records = [
        {
            "repair_batch_id": provenance.repair_batch_id,
            "symbol": str(row["symbol"]),
            "trade_date": str(row["date"]),
            "price_series_mode": str(price_series_mode),
            "repair_source": provenance.repair_source,
            "source_file": provenance.source_file,
            "source_query_time": provenance.source_query_time,
            "source_row_hash": str(row.get("_source_row_hash") or ""),
            "stored_row_hash": row_content_hash(
                {
                    key: value
                    for key, value in row.items()
                    # 与 :func:`verify_repairs` 必须逐字同一套列，否则每次校验都"漂移"。
                    # ``adjustment_source`` 排除在外：它的值就等于本行的 repair_source，
                    # 单独进哈希只是把同一个事实编码两遍。
                    if key in REPAIR_BAR_COLUMNS and key != "adjustment_source"
                }
            ),
            "vendor_original_missing": bool(provenance.vendor_original_missing),
            "repair_reason": provenance.repair_reason,
            "verified_by": provenance.verified_by,
            "schema_version": REPAIR_SCHEMA_VERSION,
            "created_at": stamp,
        }
        for row in planned_rows
    ]
    return pd.DataFrame(
        records,
        columns=[
            "repair_batch_id",
            "symbol",
            "trade_date",
            "price_series_mode",
            "repair_source",
            "source_file",
            "source_query_time",
            "source_row_hash",
            "stored_row_hash",
            "vendor_original_missing",
            "repair_reason",
            "verified_by",
            "schema_version",
            "created_at",
        ],
    )


def register_provenance(con: object, rows: pd.DataFrame) -> int:
    """写入 ``daily_bar_repairs``。同批次重复注册必须**内容一致**，否则报漂移。"""
    if rows.empty:
        return 0
    con.execute(PROVENANCE_DDL)  # type: ignore[attr-defined]
    batch_id = str(rows.iloc[0]["repair_batch_id"])
    existing = con.execute(  # type: ignore[attr-defined]
        f"SELECT symbol, trade_date, stored_row_hash FROM {REPAIR_PROVENANCE_TABLE} "
        "WHERE repair_batch_id = ?",
        [batch_id],
    ).df()
    if not existing.empty:
        merged = existing.merge(
            rows[["symbol", "trade_date", "stored_row_hash"]],
            on=["symbol", "trade_date"],
            how="inner",
            suffixes=("_old", "_new"),
        )
        drifted = merged[merged["stored_row_hash_new"] != merged["stored_row_hash_old"]]
        if not drifted.empty:
            raise ValueError(
                f"repair batch {batch_id} already registered with different content for "
                f"{len(drifted)} keys — refuse to silently re-repair"
            )
    registered = set(zip(existing["symbol"], existing["trade_date"], strict=False))
    fresh = rows[
        [
            (str(s), str(d)) not in registered
            for s, d in zip(rows["symbol"], rows["trade_date"], strict=True)
        ]
    ]
    if fresh.empty:
        return 0
    columns = ", ".join(fresh.columns)
    con.register("df_repair_provenance", fresh)  # type: ignore[attr-defined]
    con.execute(  # type: ignore[attr-defined]
        f"INSERT INTO {REPAIR_PROVENANCE_TABLE} ({columns}) "
        "SELECT * FROM df_repair_provenance"
    )
    return int(len(fresh))


def verify_repairs(
    con: object, *, batch_id: str, bar_table: str = "daily_bars"
) -> dict[str, object]:
    """每条登记的 repair 行现在**是否还在、内容是否还是当初那个**。

    ``missing`` 非空说明补写行被后续流程删掉或从未写进去；``drifted`` 非空说明它被
    改过。两者都必须为 0 才算"修复仍然成立"——这一条是给 freeze/preflight 读的，
    不是给人看的安慰数字。
    """
    rows = con.execute(  # type: ignore[attr-defined]
        f"SELECT symbol, trade_date, stored_row_hash FROM {REPAIR_PROVENANCE_TABLE} "
        "WHERE repair_batch_id = ?",
        [batch_id],
    ).df()
    missing: list[str] = []
    drifted: list[str] = []
    checked = 0
    for symbol, date, expected in zip(
        rows["symbol"], rows["trade_date"], rows["stored_row_hash"], strict=True
    ):
        found = con.execute(  # type: ignore[attr-defined]
            f"SELECT * FROM {bar_table} WHERE symbol = ? AND CAST(date AS VARCHAR) = ?",
            [str(symbol), str(date)],
        ).df()
        if found.empty:
            missing.append(f"{symbol}@{date}")
            continue
        checked += 1
        stored = row_content_hash(
            {
                k: v
                for k, v in found.iloc[0].to_dict().items()
                if k in REPAIR_BAR_COLUMNS and k != "adjustment_source"
            }
        )
        if stored != str(expected):
            drifted.append(f"{symbol}@{date}")
    return {
        "batch_id": str(batch_id),
        "registered": int(len(rows)),
        "present": int(checked),
        "missing": missing,
        "drifted": drifted,
        "status": "PASS" if not missing and not drifted else "FAIL",
    }


def revert_repairs(
    con: object, *, batch_id: str, bar_table: str = "daily_bars"
) -> int:
    """按批次精确回滚：只删**登记过**的主键。

    这是"改错了能不能退回去"的唯一答案，所以它不删任何未登记的行——即使票号同段。
    原始 ZIP 从未被改，回滚后重建即可回到 vendor 原样。
    """
    keys = repair_keys(con, batch_id)
    for symbol, date in keys:
        con.execute(  # type: ignore[attr-defined]
            f"DELETE FROM {bar_table} WHERE symbol = ? AND CAST(date AS VARCHAR) = ?",
            [symbol, date],
        )
    con.execute(  # type: ignore[attr-defined]
        f"DELETE FROM {REPAIR_PROVENANCE_TABLE} WHERE repair_batch_id = ?", [batch_id]
    )
    return len(keys)


def repair_keys(con: object, batch_id: str) -> list[tuple[str, str]]:
    """该批次登记过的主键——:func:`revert` 只敢删这些。"""
    frame = con.execute(  # type: ignore[attr-defined]
        f"SELECT symbol, trade_date FROM {REPAIR_PROVENANCE_TABLE} "
        "WHERE repair_batch_id = ? ORDER BY 1, 2",
        [batch_id],
    ).df()
    return [(str(a), str(b)) for a, b in zip(frame["symbol"], frame["trade_date"], strict=True)]
