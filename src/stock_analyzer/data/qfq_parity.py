"""P3.3.1 —— RAW / QFQ 两份 delta 库的**逐键对账**（只读，不改任何数据）。

为什么需要这一层：``vendor_zip_overlay`` 的 qfq 批量路径允许"整只票跳过"，而
``import_vendor_zip_to_delta`` 只用集合差分记下 ``skipped_symbols`` 并把 ``ok`` 留在
true。于是 2026-07-17..07-30 生产上少了 295 个 ``(symbol, date)`` QFQ 行
（27 只票 × 11 个 session，含 000001 / 600000），**没有任何一层把它当失败**：
覆盖率 0.995 > 0.90 的比例闸看不见它，增量导入又永不回填那些日期。

本模块回答的不是"像不像缺数据"，而是一个可复算的集合问题：

```text
RAW 有 bar 且 QFQ 没有     → 派生链丢了东西，必须分类
  因子也取不到             → QFQ_FACTOR_MISSING      （上游交付问题）
  因子取得到               → QFQ_DERIVATION_GAP      （我们自己派生问题）
QFQ 有 bar 且 RAW 没有     → QFQ_ROW_WITHOUT_RAW     （结构不一致：qfq 由 raw 派生，
                                                       没有 raw 的 qfq 行无从解释）
RAW 与 QFQ 都没有          → 不在本模块职责内
                            （两侧同缺既可能是停牌/退市/换号，也可能是对称断供，
                              仓库没有独立停牌真值源，见 ADR-002 §6.2）
```

三种情况全部**只报告**，不修数据：修复是另一件需要单独授权的事
（见 ``docs/alpha_v2/p3_3_data_remediation/P3_3_REPAIR_MANIFEST.json``）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import duckdb
import pandas as pd

#: 三条判定的 reason code。
REASON_FACTOR_MISSING = "QFQ_FACTOR_MISSING"
REASON_DERIVATION_GAP = "QFQ_DERIVATION_GAP"
REASON_ROW_WITHOUT_RAW = "QFQ_ROW_WITHOUT_RAW"

#: ``has_factor(symbol, date)``：该票该日**是否**能拿到一个可用复权因子。
#: 由调用方注入（生产实现读 ``复权因子_前复权.zip``），这样本模块不绑定任何来源格式，
#: 也方便测试直接给一张表。
FactorAvailability = Callable[[str, str], bool]

MAX_EXAMPLES = 200


@dataclass
class QfqParity:
    """一次对账的结果。``to_payload`` 是机器可读形态，直接进 readiness 产物。"""

    window: tuple[str, str]
    raw_rows: int = 0
    qfq_rows: int = 0
    raw_present_qfq_missing: int = 0
    qfq_present_raw_missing: int = 0
    factor_missing_for_raw: int = 0
    derivation_gap_count: int = 0
    affected_dates: list[str] = field(default_factory=list)
    top_affected_dates: dict[str, int] = field(default_factory=dict)
    affected_symbols: list[str] = field(default_factory=list)
    keys: list[tuple[str, str]] = field(default_factory=list)
    #: 逐键判定明细 ``(symbol, trade_date, reason)``——与 repair manifest 对账要用它，
    #: 只给计数就没法证明"检测到的键集"和"计划补的键集"是同一批。
    classified: list[tuple[str, str, str]] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.raw_present_qfq_missing == 0 and self.qfq_present_raw_missing == 0

    @property
    def reason(self) -> str:
        if self.ok:
            return ""
        # 优先级：先报最严重也最可修的派生缺陷，再报上游因子缺失，最后报结构异常。
        for code in (
            REASON_DERIVATION_GAP,
            REASON_FACTOR_MISSING,
            REASON_ROW_WITHOUT_RAW,
        ):
            if self.reasons.get(code):
                return code
        return REASON_DERIVATION_GAP

    def to_payload(self) -> dict[str, object]:
        return {
            "guard": "qfq_parity",
            "ok": self.ok,
            "reason": self.reason,
            "window": list(self.window),
            "raw_rows": int(self.raw_rows),
            "qfq_rows": int(self.qfq_rows),
            "raw_present_qfq_missing": int(self.raw_present_qfq_missing),
            "qfq_present_raw_missing": int(self.qfq_present_raw_missing),
            "factor_missing_for_raw": int(self.factor_missing_for_raw),
            "derivation_gap_count": int(self.derivation_gap_count),
            "affected_date_count": len(self.affected_dates),
            "top_affected_dates": dict(self.top_affected_dates),
            "affected_symbol_count": len(self.affected_symbols),
            "affected_symbols": self.affected_symbols[:MAX_EXAMPLES],
            "keys": [f"{symbol}@{day}" for symbol, day in self.keys[:MAX_EXAMPLES]],
        }


def _keys(
    con: duckdb.DuckDBPyConnection, alias: str, window: Sequence[str]
) -> set[tuple[str, str]]:
    return {
        (str(symbol), str(day))
        for symbol, day in con.execute(
            f"SELECT symbol, CAST(date AS VARCHAR) FROM {alias}.daily_bars "
            "WHERE date BETWEEN ? AND ?",
            [window[0], window[1]],
        ).fetchall()
    }


def assess_qfq_parity(
    *,
    raw_db: str,
    qfq_db: str,
    window: Sequence[str],
    has_factor: FactorAvailability | None = None,
) -> QfqParity:
    """两份库逐键对账。只读连接，绝不写。

    ``has_factor=None`` 时不做 B/C 细分：所有 ``raw 有 / qfq 无`` 一律按
    ``QFQ_DERIVATION_GAP`` 报（更严），因为"没有因子"本身也需要证据才允许免责。
    """
    if len(window) != 2:
        raise ValueError("window must be (start, end)")
    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit='700MB'")
    con.execute("SET threads=2")
    con.execute(f"ATTACH '{qfq_db}' AS q (READ_ONLY)")
    con.execute(f"ATTACH '{raw_db}' AS r (READ_ONLY)")
    raw_keys = _keys(con, "r", window)
    qfq_keys = _keys(con, "q", window)
    con.close()

    parity = QfqParity(
        window=(str(window[0]), str(window[1])),
        raw_rows=len(raw_keys),
        qfq_rows=len(qfq_keys),
    )
    missing_qfq = sorted(raw_keys - qfq_keys)
    orphan_qfq = sorted(qfq_keys - raw_keys)

    tally: Counter[str] = Counter()
    for symbol, day in missing_qfq:
        code = (
            REASON_FACTOR_MISSING
            if has_factor is not None and not has_factor(symbol, day)
            else REASON_DERIVATION_GAP
        )
        if code == REASON_FACTOR_MISSING:
            parity.factor_missing_for_raw += 1
        else:
            parity.derivation_gap_count += 1
        tally[day] += 1
        parity.reasons.setdefault(code, []).append(f"{symbol}@{day}")
        parity.keys.append((symbol, day))
        parity.classified.append((symbol, day, code))
    for symbol, day in orphan_qfq:
        parity.reasons.setdefault(REASON_ROW_WITHOUT_RAW, []).append(f"{symbol}@{day}")

    parity.raw_present_qfq_missing = len(missing_qfq)
    parity.qfq_present_raw_missing = len(orphan_qfq)
    parity.affected_dates = sorted(tally)
    parity.top_affected_dates = {
        day: int(count) for day, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    }
    parity.affected_symbols = sorted({symbol for symbol, _day in missing_qfq})
    return parity


def factor_availability_from_series(
    factors: Mapping[str, Iterable[str]] | Mapping[str, Sequence[str]],
) -> FactorAvailability:
    """把 ``symbol -> 升序因子日期`` 变成判定函数（因子日期**不晚于**该日即算可用）。

    "不晚于"是必需的：复权因子是分段常数（``reindex(...).ffill().bfill()``），
    所以一条 2026-08-05 的因子同样能覆盖 2026-07-20 这一天。
    反过来，如果该票的因子序列**整段都晚于**这一天，那才是真的取不到。
    """
    import bisect

    indexed = {str(symbol): sorted(str(day) for day in days) for symbol, days in factors.items()}

    def _has(symbol: str, day: str) -> bool:
        series = indexed.get(str(symbol))
        if not series:
            return False
        return bisect.bisect_right(series, str(day)) > 0

    return _has


def frame_of(parity: QfqParity) -> pd.DataFrame:
    """逐键判定表（与 repair manifest 逐键对账用）。"""
    return pd.DataFrame(
        [
            {"symbol": symbol, "trade_date": day, "reason": reason}
            for symbol, day, reason in parity.classified
        ],
        columns=["symbol", "trade_date", "reason"],
    )
