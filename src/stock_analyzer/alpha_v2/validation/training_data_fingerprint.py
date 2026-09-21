"""训练数据**内容**指纹（M4-L R1 引入，R1.1 扩到实际训练输入）。

**为什么需要**：既有 ``panel_fingerprint()`` 只包含窗口/行数/列名等**形状**信息，
数据库内容被改写（同样的行数、同样的最新日期）它完全看不出来；preflight 的
``data_identity`` 里也只有 path / row_count / latest_date。于是"检查的数据"与
"训练用到的数据"之间没有内容级证据链。

本模块给出**内容级、确定性**的指纹：

```text
fingerprint = sha256(
    canonical_json(header) || 逐行 canonical(按 MODEL_TRAINING_SOURCE_COLUMNS 取值)
)
```

R1.1 相对 R1 的三处**证伪性修复**（外部复核：R1 的指纹漏掉会改变训练结果的输入）：

1. **窗口**：R1 只 hash ``window_start..window_end``（决策窗）。但
   ``load_daily_panel(warmup_days=N)`` 实际读取
   ``window_start - N 自然日 .. window_end``——warmup 段参与 rolling / MA / EMA /
   return / volatility / 量比等**全部技术特征**的构造，改它必然改训练帧。
   现在 hash 的是 :func:`source_window_start` 推出的 **source_window**（含 warmup）。
2. **列**：R1 只 hash ``symbol/date/OHLC/volume/turnover`` 八列。实际训练链还消费
   ``float_market_cap``（S12 风格维度 → 基准成分 → ``excess_return_*`` **目标**）、
   ``board`` / ``is_st`` / ``is_delisting_risk`` / ``suspended``（涨跌停与可成交语义）、
   ``pre_close`` / ``up_limit`` / ``down_limit``（``limit_rule`` 与 ``ExecutionMatcher``）、
   ``price_series_mode``（价格口径认证）。列清单不再是手写的第二套，而是从
   ``research.panel.PANEL_BAR_COLUMNS`` **派生**——面板读什么，这里就 hash 什么。
3. **来源存在性**：header 记录 ``available_source_columns`` 与
   ``missing_optional_source_columns``。"列不存在"与"列存在但全空"会触发不同的
   派生/回退路径（``pre_close`` 缺失 → 退回上一根 raw 收盘；``board`` 缺失 → 按
   代码前缀推断），因此**存在状态**本身也是训练输入身份的一部分。

性质（有测试钉住，FP-1..FP-6）：

1. 相同数据 → 同 hash（跨进程、跨运行稳定；不依赖库里 hash 函数的实现版本）；
2. 源窗口内任一被 hash 的列被改 → hash 改变；
3. 只在 ``window_end`` 之后追加新交易日 → hash 不变；
4. 只在 source_window 之前的数据被改 → hash 不变（那些行训练链根本没读）。

实现走流式游标（``fetchmany``）+ 固定列序 + ``repr`` 规范化，避免把百万行一次性
拉进内存；必需列缺列即报错（宁可失败也不产生"少列也算过"的指纹）；行数与
``count(*)`` 不符即报错（读取期间库被改写 = 不可复现的指纹，不能悄悄放过）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from stock_analyzer.alpha_v2.research.panel import PANEL_BAR_COLUMNS

# 指纹契约版本：v1 = R1（8 列 / 决策窗），v2 = R1.1（全 panel 源列 / source_window）。
# 版本进 header → 换契约必然换 hash；provenance 与 preflight 都按版本对账。
FINGERPRINT_VERSION = "v2"
FINGERPRINT_SCHEMA = "alpha_v2_training_data_fingerprint.v2"

# 面板列名 → daily_bars 源列名（与 ``load_daily_panel`` 的别名规则同一约定）。
PANEL_DATE_COLUMN = "trade_date"
SOURCE_DATE_COLUMN = "date"

# 训练链**必需**的源列：缺任一项都无法构造训练帧（load_daily_panel 只是补 None，
# 那会让"读不到数据"伪装成"数据是空值"）。缺列 → 直接报错，不给指纹。
REQUIRED_SOURCE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
)


def _panel_source_columns() -> tuple[str, ...]:
    """从面板契约派生训练源列（单一真相源，不手写第二套字段清单）。"""
    return tuple(
        SOURCE_DATE_COLUMN if column == PANEL_DATE_COLUMN else column
        for column in PANEL_BAR_COLUMNS
    )


# 训练链实际读取的全部原始市场列（面板契约的 daily_bars 形态）。
MODEL_TRAINING_SOURCE_COLUMNS: tuple[str, ...] = _panel_source_columns()
# 可选源列：缺失时训练链走**派生的确定性回退**（不是"没有数据"），但回退路径与
# 直接取值是两种不同的语义，所以缺失与否必须进 header。
OPTIONAL_SOURCE_COLUMNS: tuple[str, ...] = tuple(
    column for column in MODEL_TRAINING_SOURCE_COLUMNS if column not in REQUIRED_SOURCE_COLUMNS
)

_FETCH_BATCH = 50_000


class TrainingDataFingerprintError(RuntimeError):
    """指纹不可计算（库不可读/缺列/窗口非法/读取期间内容变化）——按 BLOCKED 处理。"""


def source_window_start(decision_start: date, warmup_days: int) -> date:
    """``source_window`` 起点 = 决策窗起点向前 ``warmup_days`` **自然日**。

    与 ``load_daily_panel`` 的 ``warmup_start = window_start - timedelta(days=warmup)``
    同一公式——指纹窗口必须等于训练链真正读到的行范围，否则 warmup 段的变化
    会被漏掉（FP-4 钉住这条不变量）。
    """
    return decision_start - timedelta(days=max(0, int(warmup_days)))


def compute_training_data_fingerprint(
    market_db: str | Path,
    *,
    training_start: date,
    training_end: date,
    warmup_days: int = 0,
    columns: Sequence[str] | None = None,
) -> dict[str, object]:
    """返回确定性指纹载荷（含窗口身份、列存在性、行数与内容哈希）。

    ``training_start/end`` 是**决策窗**；``warmup_days`` 是训练链传给
    ``load_daily_panel`` 的同名参数，指纹覆盖 ``source_window``（含 warmup）。
    """
    if training_end < training_start:
        raise TrainingDataFingerprintError(
            f"training_end({training_end}) 早于 training_start({training_start})"
        )
    warmup = max(0, int(warmup_days))
    source_start = source_window_start(training_start, warmup)
    requested = tuple(str(item) for item in (columns or MODEL_TRAINING_SOURCE_COLUMNS))
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise TrainingDataFingerprintError(f"duckdb 不可用: {exc}") from exc
    path = Path(market_db)
    try:
        connection = duckdb.connect(str(path), read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise TrainingDataFingerprintError(
            f"行情库不可读: {exc.__class__.__name__}: {exc}"
        ) from exc
    try:
        available = {str(row[0]) for row in connection.execute("DESCRIBE daily_bars").fetchall()}
        missing_required = [
            column
            for column in REQUIRED_SOURCE_COLUMNS
            if column in requested and column not in available
        ]
        if missing_required:
            raise TrainingDataFingerprintError(f"daily_bars 缺必需源列: {missing_required}")
        available_requested = [column for column in requested if column in available]
        missing_optional = [column for column in requested if column not in available]
        # 缺的可选列用 NULL 占位：行宽固定，列存在性由 header 表达（"不存在"与
        # "存在但全空"因此仍然可区分——后者 header 里是 available）。
        projection = ", ".join(
            column if column in available else f"NULL AS {column}" for column in requested
        )
        count_row = connection.execute(
            "SELECT count(*) FROM daily_bars "
            "WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)",
            [source_start.isoformat(), training_end.isoformat()],
        ).fetchone()
        expected_rows = int(count_row[0] if count_row else 0)
        header = {
            "fingerprint_version": FINGERPRINT_VERSION,
            "decision_window": [training_start.isoformat(), training_end.isoformat()],
            "source_window": [source_start.isoformat(), training_end.isoformat()],
            "warmup_days": warmup,
            "requested_source_columns": list(requested),
            "available_source_columns": available_requested,
            "missing_optional_source_columns": missing_optional,
            "row_count": expected_rows,
        }
        digest = hashlib.sha256()
        digest.update(_canonical_json(header).encode("utf-8"))
        cursor = connection.execute(
            f"SELECT {projection} FROM daily_bars "
            "WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) "
            "ORDER BY symbol, date",
            [source_start.isoformat(), training_end.isoformat()],
        )
        rows = 0
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                rows += 1
                digest.update(("\n" + _canonical_row(row)).encode())
        if rows != expected_rows:
            # 读取期间库被改写（count 与逐行扫描不是同一快照）→ 指纹不可复现。
            raise TrainingDataFingerprintError(
                f"指纹计算期间行情库发生变化（count={expected_rows}, scanned={rows}）；"
                "请在没有写入任务时重跑"
            )
        fingerprint = digest.hexdigest()
        return {
            "schema": FINGERPRINT_SCHEMA,
            "fingerprint_version": FINGERPRINT_VERSION,
            "fingerprint": fingerprint,
            # §4 契约名：与 fingerprint 是同一个 digest（保留旧键名兼容 R1 消费者）。
            "content_hash": fingerprint,
            "decision_window": header["decision_window"],
            "source_window": header["source_window"],
            "warmup_days": warmup,
            "requested_source_columns": list(requested),
            "available_source_columns": available_requested,
            "missing_optional_source_columns": missing_optional,
            "row_count": rows,
            "rows": rows,
            "columns": list(requested),
        }
    finally:
        connection.close()


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _canonical_row(row: tuple[object, ...]) -> str:
    return "|".join(_canonical_value(value) for value in row)


def _canonical_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value:
            return "nan"
        return repr(value)
    if isinstance(value, (int, str)):
        return str(value)
    # Decimal / date / timestamp 等：用字符串形式（库侧格式稳定）
    return str(value)


__all__ = [
    "FINGERPRINT_SCHEMA",
    "FINGERPRINT_VERSION",
    "MODEL_TRAINING_SOURCE_COLUMNS",
    "OPTIONAL_SOURCE_COLUMNS",
    "PANEL_DATE_COLUMN",
    "REQUIRED_SOURCE_COLUMNS",
    "SOURCE_DATE_COLUMN",
    "TrainingDataFingerprintError",
    "compute_training_data_fingerprint",
    "source_window_start",
]
