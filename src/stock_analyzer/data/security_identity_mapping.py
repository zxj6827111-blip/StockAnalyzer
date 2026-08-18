"""Security identity mapping for Beijing exchange code transitions.

The Beijing exchange went through a code migration: legacy 43/83/87-series
codes were superseded by 92-series codes around 2025-09-30. This module
provides validation and querying utilities for the mapping, but does NOT
hardcode any unverified mapping pairs — all mappings must come from a
verifiable external source (Tushare stock_basic, an explicit mapping file,
or operator-verified data loaded into the security_identity_mapping table).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from stock_analyzer.data.provider import DataSourceError

# 北交所历史代码前缀与现行代码前缀。
# 仅作为过滤辅助，不用于推断具体映射关系。
BEIJING_LEGACY_PREFIXES = ("43", "83", "87")
BEIJING_CURRENT_PREFIX = "92"

# 价格连续性检查的合理窗口（天）：映射前后最后/首个交易日相差不超过此值
# 视为可拼接。超过则视为不连续并返回告警。
_PRICE_CONTINUITY_MAX_GAP_DAYS = 30


@dataclass
class IdentityMappingEntry:
    """北交所代码历史映射的单条记录。

    Attributes:
        historical_symbol: 旧代码（43/83/87 系列等）。
        canonical_symbol:  现行规范代码（92 系列等）。
        effective_from:    映射生效起始日（含）。
        effective_to:      映射生效结束日（含）；None 表示至今有效。
        source:            映射来源描述，便于追溯（如 "tushare.stock_basic"）。
        as_of:             数据截取时间戳字符串，便于审计。
    """

    historical_symbol: str
    canonical_symbol: str
    effective_from: date
    effective_to: date | None
    source: str
    as_of: str


def validate_mapping_entries(entries: list[IdentityMappingEntry]) -> list[str]:
    """校验映射条目集合，返回错误信息列表。

    空列表表示全部通过。检查项：
      1. historical_symbol 与 canonical_symbol 不能相同；
      2. effective_from 不能晚于 effective_to（若 effective_to 存在）；
      3. 同一 historical_symbol 的不同条目之间生效区间不能重叠；
      4. 同一 historical_symbol 在重叠时间段内只能映射到唯一的 canonical_symbol
         （由区间不重叠保证，但若同 historical_symbol 出现不同 canonical_symbol
         仍单独告警，便于人工复核）。

    Args:
        entries: 待校验的映射条目列表。

    Returns:
        错误信息字符串列表；空列表表示校验通过。
    """
    errors: list[str] = []

    # 基本字段约束
    for idx, entry in enumerate(entries):
        if entry.historical_symbol == entry.canonical_symbol:
            errors.append(
                f"entry[{idx}] historical_symbol == canonical_symbol "
                f"({entry.historical_symbol}); 自映射无效"
            )
        if entry.effective_to is not None and entry.effective_from > entry.effective_to:
            errors.append(
                f"entry[{idx}] {entry.historical_symbol}->{entry.canonical_symbol} "
                f"effective_from {entry.effective_from} 晚于 "
                f"effective_to {entry.effective_to}"
            )

    # 按 historical_symbol 分组检查区间重叠与 canonical 一致性
    by_hist: dict[str, list[IdentityMappingEntry]] = {}
    for entry in entries:
        by_hist.setdefault(entry.historical_symbol, []).append(entry)

    for hist, group in by_hist.items():
        # 按 effective_from 排序后两两比较区间是否重叠
        sorted_group = sorted(group, key=lambda e: e.effective_from)
        for i in range(len(sorted_group)):
            e1 = sorted_group[i]
            end1 = e1.effective_to if e1.effective_to is not None else date.max
            for j in range(i + 1, len(sorted_group)):
                e2 = sorted_group[j]
                start2 = e2.effective_from
                # 区间 [start1, end1] 与 [start2, ...] 是否相交（闭区间）
                if start2 <= end1:
                    errors.append(
                        f"historical_symbol={hist} 存在重叠区间: "
                        f"[{e1.effective_from}, {e1.effective_to}] -> "
                        f"{e1.canonical_symbol} 与 "
                        f"[{e2.effective_from}, {e2.effective_to}] -> "
                        f"{e2.canonical_symbol}"
                    )
                # 同一 historical_symbol 映射到不同 canonical_symbol 时提示
                if e1.canonical_symbol != e2.canonical_symbol:
                    errors.append(
                        f"historical_symbol={hist} 存在不同 canonical_symbol: "
                        f"{e1.canonical_symbol} / {e2.canonical_symbol}，请人工复核"
                    )

    return errors


def _parse_date(value: str | None) -> date | None:
    """解析 YYYY-MM-DD 字符串为 date；空值返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析日期 '{value}'（应为 YYYY-MM-DD）") from exc


def load_mapping_from_csv(path: str | Path) -> list[IdentityMappingEntry]:
    """从 CSV 文件加载映射条目并校验。

    CSV 必须包含列：
    historical_symbol, canonical_symbol, effective_from, effective_to, source, as_of
    effective_to 可为空。日期格式 YYYY-MM-DD。

    Args:
        path: CSV 文件路径。

    Returns:
        校验通过的映射条目列表。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: CSV 缺少必要列或校验失败。
        DataSourceError: CSV 读取失败。
    """
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"映射 CSV 文件不存在: {csv_path}")

    required_cols = (
        "historical_symbol",
        "canonical_symbol",
        "effective_from",
        "effective_to",
        "source",
        "as_of",
    )

    entries: list[IdentityMappingEntry] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ValueError(f"CSV 文件为空或无表头: {csv_path}")
            missing = [c for c in required_cols if c not in reader.fieldnames]
            if missing:
                raise ValueError(
                    f"CSV 缺少必要列: {missing}; 实际列: {reader.fieldnames}"
                )
            for lineno, row in enumerate(reader, start=2):
                hist = (row.get("historical_symbol") or "").strip()
                canon = (row.get("canonical_symbol") or "").strip()
                if not hist or not canon:
                    # 跳过空行，但完全空白才跳过；缺一侧则报错
                    if not hist and not canon:
                        continue
                    raise ValueError(
                        f"CSV 第 {lineno} 行 historical_symbol/canonical_symbol 不能为空"
                    )
                effective_from = _parse_date(row.get("effective_from"))
                if effective_from is None:
                    raise ValueError(
                        f"CSV 第 {lineno} 行 effective_from 不能为空"
                    )
                effective_to = _parse_date(row.get("effective_to"))
                source = (row.get("source") or "").strip()
                as_of = (row.get("as_of") or "").strip()
                entries.append(
                    IdentityMappingEntry(
                        historical_symbol=hist,
                        canonical_symbol=canon,
                        effective_from=effective_from,
                        effective_to=effective_to,
                        source=source,
                        as_of=as_of,
                    )
                )
    except UnicodeDecodeError as exc:
        raise DataSourceError(f"CSV 编码读取失败: {csv_path}") from exc
    except csv.Error as exc:
        raise DataSourceError(f"CSV 解析失败: {csv_path}: {exc}") from exc

    errors = validate_mapping_entries(entries)
    if errors:
        raise ValueError(
            "映射条目校验失败，共 {} 项错误:\n{}".format(
                len(errors), "\n".join(f"  - {e}" for e in errors)
            )
        )
    return entries


def load_mapping_from_db(warehouse) -> list[IdentityMappingEntry]:
    """从 MarketWarehouse.fetch_security_identity_mapping() 加载映射条目。

    warehouse 参数为 MarketWarehouse 实例。读取后进行校验，校验失败则
    raise DataSourceError（数据已落库但存在不一致，需人工介入）。

    Args:
        warehouse: 提供 fetch_security_identity_mapping 方法的对象。

    Returns:
        校验通过的映射条目列表；表不存在或为空时返回空列表。

    Raises:
        DataSourceError: 落库数据校验失败或读取异常。
    """
    try:
        frame = warehouse.fetch_security_identity_mapping()
    except Exception as exc:  # noqa: BLE001 - 仓库访问异常统一转 DataSourceError
        raise DataSourceError(
            f"从 warehouse 读取 security_identity_mapping 失败: {exc}"
        ) from exc

    if frame is None or frame.empty:
        return []

    required_cols = (
        "historical_symbol",
        "canonical_symbol",
        "effective_from",
        "effective_to",
        "source",
        "as_of",
    )
    missing = [c for c in required_cols if c not in frame.columns]
    if missing:
        raise DataSourceError(
            f"warehouse 返回数据缺少列: {missing}; 实际列: {list(frame.columns)}"
        )

    entries: list[IdentityMappingEntry] = []
    for idx, row in frame.iterrows():
        hist = str(row.get("historical_symbol", "")).strip()
        canon = str(row.get("canonical_symbol", "")).strip()
        if not hist or not canon:
            raise DataSourceError(
                f"warehouse 第 {idx} 行 historical_symbol/canonical_symbol 为空"
            )
        from_raw = row.get("effective_from")
        to_raw = row.get("effective_to")
        # warehouse 返回的日期可能是 datetime.date 或 pandas.Timestamp
        if from_raw is None or bool(pd.isna(from_raw)):
            effective_from = None
        else:
            effective_from = (
                from_raw.date() if hasattr(from_raw, "date") else _parse_date(from_raw)
            )
        if effective_from is None:
            raise DataSourceError(
                f"warehouse 第 {idx} 行 effective_from 为空"
            )
        effective_to: date | None
        if to_raw is None or bool(pd.isna(to_raw)):
            effective_to = None
        elif hasattr(to_raw, "date"):
            effective_to = to_raw.date()
        else:
            effective_to = _parse_date(str(to_raw))
        entries.append(
            IdentityMappingEntry(
                historical_symbol=hist,
                canonical_symbol=canon,
                effective_from=effective_from,
                effective_to=effective_to,
                source=str(row.get("source", "")).strip(),
                as_of=str(row.get("as_of", "")).strip(),
            )
        )

    errors = validate_mapping_entries(entries)
    if errors:
        raise DataSourceError(
            "warehouse 映射数据校验失败，共 {} 项错误:\n{}".format(
                len(errors), "\n".join(f"  - {e}" for e in errors)
            )
        )
    return entries


def _is_active(entry: IdentityMappingEntry, as_of: date) -> bool:
    """判断条目在 as_of 日期是否生效（闭区间）。"""
    if as_of < entry.effective_from:
        return False
    if entry.effective_to is not None and as_of > entry.effective_to:
        return False
    return True


def resolve_canonical_symbol(
    entries: list[IdentityMappingEntry],
    historical_symbol: str,
    as_of: date,
) -> str | None:
    """给定历史代码与日期，返回对应的 canonical_symbol。

    fail-closed 策略：若无可靠映射或映射不唯一（同一日期命中多个条目），
    返回 None，由调用方决定如何处理（如跳过、人工复核）。

    Args:
        entries:           映射条目列表（应已通过 validate_mapping_entries）。
        historical_symbol: 旧代码。
        as_of:             查询日期。

    Returns:
        canonical_symbol 或 None。
    """
    matched = [
        e for e in entries
        if e.historical_symbol == historical_symbol and _is_active(e, as_of)
    ]
    if not matched:
        # 无可靠映射：fail-closed
        return None
    if len(matched) > 1:
        # 同一日期命中多条：数据不一致，fail-closed
        return None
    return matched[0].canonical_symbol


def check_price_continuity(
    entries: list[IdentityMappingEntry],
    daily_bars_frame: pd.DataFrame,
) -> list[str]:
    """检查映射前后价格历史能否连续拼接。

    对每个 mapping entry：检查 historical_symbol 的最后交易日与
    canonical_symbol 的第一个交易日是否在合理范围内（默认 30 天内）。
    超过窗口视为不连续并返回告警描述。

    Args:
        entries:          映射条目列表。
        daily_bars_frame: 包含 symbol/date/close 列的 DataFrame。

    Returns:
        不连续的 mapping 描述列表；空列表表示全部连续。
    """
    if daily_bars_frame is None or daily_bars_frame.empty:
        return []

    required_cols = ("symbol", "date", "close")
    missing = [c for c in required_cols if c not in daily_bars_frame.columns]
    if missing:
        raise ValueError(
            f"daily_bars_frame 缺少必要列: {missing}; 实际列: {list(daily_bars_frame.columns)}"
        )

    # 规范化日期列，便于比较
    bars = daily_bars_frame.copy()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars = bars.dropna(subset=["date"])
    if bars.empty:
        return []

    warnings = []
    for entry in entries:
        hist_bars = bars[bars["symbol"] == entry.historical_symbol]
        canon_bars = bars[bars["symbol"] == entry.canonical_symbol]
        if hist_bars.empty:
            warnings.append(
                f"{entry.historical_symbol}->{entry.canonical_symbol} "
                f"缺少 historical_symbol 价格数据"
            )
            continue
        if canon_bars.empty:
            warnings.append(
                f"{entry.historical_symbol}->{entry.canonical_symbol} "
                f"缺少 canonical_symbol 价格数据"
            )
            continue

        last_hist_date = hist_bars["date"].max()
        first_canon_date = canon_bars["date"].min()
        gap_days = (first_canon_date - last_hist_date).days
        # canonical 首个交易日应晚于 historical 最后交易日，且间隔合理
        if gap_days < 0:
            # canonical 数据早于 historical 结束：存在重叠而非断点
            warnings.append(
                f"{entry.historical_symbol}->{entry.canonical_symbol} "
                f"canonical 首交易日 {first_canon_date.date()} 早于 "
                f"historical 末交易日 {last_hist_date.date()}，可能存在重叠"
            )
        elif gap_days > _PRICE_CONTINUITY_MAX_GAP_DAYS:
            warnings.append(
                f"{entry.historical_symbol}->{entry.canonical_symbol} "
                f"价格断点: historical 末交易日 {last_hist_date.date()} 与 "
                f"canonical 首交易日 {first_canon_date.date()} 相差 {gap_days} 天，"
                f"超过 {_PRICE_CONTINUITY_MAX_GAP_DAYS} 天窗口"
            )

    return warnings


def deduplicate_event_count(
    events: list[dict],
    entries: list[IdentityMappingEntry],
) -> list[dict]:
    """用映射表把旧代码事件归并到 canonical_symbol，避免重复计算。

    策略：
      - 对每个事件，若其 symbol 命中映射（按事件 date 解析），则将 symbol
        替换为 canonical_symbol；
      - 替换后，若同一 canonical_symbol + date 已存在事件，则丢弃重复事件
        （以原 canonical 事件优先，旧代码事件去重）；
      - 未命中映射的事件原样保留。

    Args:
        events:  事件列表，每个事件为含 symbol 与 date 字段的 dict。
        entries: 映射条目列表。

    Returns:
        去重后的事件列表（新 list，不修改输入）。
    """
    if not events:
        return []

    normalized: list[dict] = []
    seen_keys: set[tuple[str, date]] = set()

    for event in events:
        if not isinstance(event, dict):
            continue
        symbol = event.get("symbol")
        raw_date = event.get("date")
        if symbol is None or raw_date is None:
            # 缺少必要字段：原样保留，不去重
            normalized.append(event)
            continue

        # 解析事件日期
        if isinstance(raw_date, date):
            event_date = raw_date
        elif isinstance(raw_date, pd.Timestamp):
            event_date = raw_date.date()
        else:
            try:
                event_date = date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                # 日期不可解析：原样保留，不做归并
                normalized.append(event)
                continue

        # 尝试将旧代码归并到 canonical_symbol
        canonical = resolve_canonical_symbol(entries, str(symbol), event_date)
        resolved_symbol = canonical if canonical is not None else str(symbol)

        key = (resolved_symbol, event_date)
        if key in seen_keys:
            # 已存在同 symbol+date 事件：视为重复，丢弃
            continue
        seen_keys.add(key)

        if canonical is not None:
            # 归并到 canonical_symbol：构造新事件避免修改输入
            merged = dict(event)
            merged["symbol"] = canonical
            normalized.append(merged)
        else:
            normalized.append(event)

    return normalized
