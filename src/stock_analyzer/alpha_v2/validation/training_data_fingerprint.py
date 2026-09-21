"""M4-L R1（外部复核 BLOCKER 4）：训练数据内容指纹。

**为什么需要**：既有 ``panel_fingerprint()`` 只包含窗口/行数/列名等**形状**信息，
数据库内容被改写（同样的行数、同样的最新日期）它完全看不出来；preflight 的
``data_identity`` 里也只有 path / row_count / latest_date。于是"检查的数据"与
"训练用到的数据"之间没有内容级证据链。

本模块给出**内容级、确定性**的指纹：

```text
training_data_fingerprint = sha256(
    canonical_header(列清单 + 窗口) || 逐行 canonical(symbol, date, OHLC, volume, turnover, ...)
)
```

性质（有测试钉住）：

1. 相同数据 → 同 hash（跨进程、跨运行稳定；不依赖库里 hash 函数的实现版本）；
2. 训练窗内任一价格/成交量/成交额被改 → hash 改变；
3. **只在训练窗外追加新交易日 → hash 不变**（窗口外数据不参与输入构造）。

实现走流式游标（``fetchmany``）+ 固定列序 + ``repr`` 规范化，避免把
百万行一次性拉进内存；列集合缺列即报错（宁可失败也不产生"少列也算过"的指纹）。
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

FINGERPRINT_COLUMNS: tuple[str, ...] = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
)
_FETCH_BATCH = 50_000


class TrainingDataFingerprintError(RuntimeError):
    """指纹不可计算（库不可读/缺列/窗口非法）——调用方按 BLOCKED 处理。"""


def compute_training_data_fingerprint(
    market_db: str | Path,
    *,
    training_start: date,
    training_end: date,
    columns: tuple[str, ...] = FINGERPRINT_COLUMNS,
) -> dict[str, object]:
    """返回 ``{"fingerprint", "rows", "window", "columns"}``（确定性）。"""
    if training_end < training_start:
        raise TrainingDataFingerprintError(
            f"training_end({training_end}) 早于 training_start({training_start})"
        )
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
        available = {
            str(row[0]) for row in connection.execute("DESCRIBE daily_bars").fetchall()
        }
        missing = [column for column in columns if column not in available]
        if missing:
            raise TrainingDataFingerprintError(f"daily_bars 缺列: {missing}")
        digest = hashlib.sha256()
        header = "|".join(columns)
        digest.update(
            f"v1|{header}|{training_start.isoformat()}|{training_end.isoformat()}".encode()
        )
        cursor = connection.execute(
            f"SELECT {', '.join(columns)} FROM daily_bars "
            "WHERE date >= CAST(? AS DATE) AND date <= CAST(? AS DATE) "
            "ORDER BY symbol, date",
            [training_start.isoformat(), training_end.isoformat()],
        )
        rows = 0
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                rows += 1
                digest.update(("\n" + _canonical_row(row)).encode())
        return {
            "fingerprint": digest.hexdigest(),
            "rows": rows,
            "window": [training_start.isoformat(), training_end.isoformat()],
            "columns": list(columns),
        }
    finally:
        connection.close()


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
    "FINGERPRINT_COLUMNS",
    "TrainingDataFingerprintError",
    "compute_training_data_fingerprint",
]
