"""Trend 尾盘策略契约：线上推荐、训练标签、历史验证三方共用的唯一真相源。

计划 §3.1 要求"把尾盘入场规则落实到线上、训练标签和历史验证共同调用的策略契约"。
在此之前 ``soup_strategy.entry_mode="tail_confirm"`` / ``entry_window=["14:30","14:50"]``
在全仓库没有任何消费者，而入场/出场规则在三处各自硬编码、TP-SL 口径互不一致
（``labels/soup.py`` 默认 5%/5%/5d、``config`` 声明 8%/5%/10d）。本模块把这些规则
收敛成一个带版本的契约：任何一侧要改语义必须改这里。

口径（第一轮，仅 trend；monster 保留既有策略，不走本契约）：

- 夜扫产出观察池，次日 14:30–14:50 尾盘确认后才成为最终推荐；
- 尾盘窗口每 5 分钟检查一次，确认只读**当时已完成**的分钟行情；
- 历史模拟的成交价取**确认之后下一根**已完成分钟 bar（用确认时刻那根的 close
  自成交是 look-ahead）；
- 每只按 1 万元参考金额计成本，整手向下取整，最低佣金照计；
- 止盈 +8%、止损 −5%，计划持有 5 个交易日，**入场日计为第 1 日**；
- T+1：入场当日不可卖，止盈止损监控从第 2 日开盘开始；
- 同一根行情内双触发时**止损优先**；
- 第 5 日尝试退出，不可卖则顺延，成熟时间以**实际可成交退出**为准；
- 未成交 / 数据末尾强平 / 未知交易状态**不计为已实现盈亏**，单列。

成交模拟必须用 raw（未复权）价格：QFQ 只可用于特征，不可用于模拟成交。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

TREND_STRATEGY_CONTRACT_VERSION = "trend_tail_v1"
NET_PROFIT_PROBABILITY_FIELD = "p_net_profit_5d_tail"

#: 观察池 / 尾盘确认 / 最终推荐 / 成交与退出——漏斗各层的稳定层名（含新增层）。
FUNNEL_LAYERS: tuple[str, ...] = (
    "universe",
    "hard_eligibility",
    "quality_300",
    "light_100",
    "deep_50",
    "night_watch_pool",
    "tail_confirmation",
    "final_recommendation",
    "execution_exit",
)

STATUS_FILLED = "filled_and_exited"
STATUS_NOT_FILLED = "not_filled"
STATUS_UNCERTAIN = "uncertain"

#: 持仓期内必须显式声明交易状态；缺失即不确定，不假设"能卖"。
UNKNOWN_TRADE_STATUS = "unknown_trade_status"


class TrendContractError(ValueError):
    """契约自身不成立（配置冲突、口径被改写）。必须阻断，不得回退默认值。"""


def _parse_clock(value: str, *, field_name: str) -> time:
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise TrendContractError(f"{field_name} must be HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise TrendContractError(f"{field_name} must be HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise TrendContractError(f"{field_name} out of range, got {value!r}")
    return time(hour=hour, minute=minute)


def _clock_parts(value: str) -> tuple[int, int]:
    clock = _parse_clock(value, field_name="clock")
    return clock.hour, clock.minute


def _to_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


@dataclass(frozen=True)
class TrendStrategyContract:
    """带版本的 trend 尾盘策略契约。构造即校验，不成立就 raise。"""

    contract_version: str = TREND_STRATEGY_CONTRACT_VERSION
    strategy: str = "trend"
    timezone: str = "Asia/Shanghai"
    entry_window_start: str = "14:30"
    entry_window_end: str = "14:50"
    check_interval_minutes: int = 5
    #: 历史成交价来自确认后第几根已完成分钟 bar（1 = 下一根）。
    fill_bar_offset: int = 1
    reference_notional: float = 10_000.0
    take_profit_pct: float = 0.08
    stop_loss_pct: float = 0.05
    holding_days: int = 5
    #: 入场日计为持仓第 1 日。
    entry_day_counts_as_holding_day: bool = True
    #: 同一根行情无法区分先后时止损优先。
    same_bar_conflict_policy: str = "stop_loss_first"
    #: 第 5 日不可卖时的最大顺延交易日数；用尽仍不可卖 → 不确定样本。
    exit_defer_max_sessions: int = 5
    max_final_recommendations: int = 3
    #: 初始准入阈值。这是**选股规则**，不是已证明的命中率。
    min_net_profit_probability: float = 0.60
    #: 线上路径的行情陈旧度上限（秒）；历史路径不传 as_of，即不受此门约束。
    max_quote_age_seconds: int = 120
    execution_price_basis: str = "raw"
    #: 与日期化成本表一起冻结的版本号，落到每条留档记录。
    cost_model_version: str = "cost_schedule_v2"
    #: 无法核算的公司行动 → 不确定样本。
    corporate_action_uncertain: bool = True

    def __post_init__(self) -> None:
        if str(self.strategy).strip().lower() != "trend":
            raise TrendContractError(
                f"this contract is trend-only, got strategy={self.strategy!r}; "
                "monster keeps its own legacy strategy"
            )
        if str(self.execution_price_basis).strip().lower() != "raw":
            raise TrendContractError(
                "execution_price_basis must be 'raw'; qfq prices must not simulate fills"
            )
        if str(self.same_bar_conflict_policy) != "stop_loss_first":
            raise TrendContractError(
                "same_bar_conflict_policy must be 'stop_loss_first' (TP/SL ambiguity "
                "resolves against the position)"
            )
        start = _parse_clock(self.entry_window_start, field_name="entry_window_start")
        end = _parse_clock(self.entry_window_end, field_name="entry_window_end")
        if int(self.check_interval_minutes) <= 0:
            raise TrendContractError("check_interval_minutes must be > 0")
        if start >= end:
            raise TrendContractError("entry_window_start must be before entry_window_end")
        if not self.confirmation_slots:
            raise TrendContractError(
                "entry window shorter than one check interval: no confirmation slot "
                "leaves a later completed bar to fill on"
            )
        if int(self.fill_bar_offset) < 1:
            raise TrendContractError("fill_bar_offset must be >= 1")
        if float(self.reference_notional) <= 0:
            raise TrendContractError("reference_notional must be > 0")
        if float(self.take_profit_pct) <= 0 or float(self.stop_loss_pct) <= 0:
            raise TrendContractError("take_profit_pct and stop_loss_pct must be > 0")
        if int(self.holding_days) < 2:
            raise TrendContractError(
                "holding_days must be >= 2: T+1 forbids selling on the entry day"
            )
        if int(self.exit_defer_max_sessions) < 0:
            raise TrendContractError("exit_defer_max_sessions must be >= 0")
        if int(self.max_final_recommendations) < 1:
            raise TrendContractError("max_final_recommendations must be >= 1")
        if not (0.0 < float(self.min_net_profit_probability) < 1.0):
            raise TrendContractError("min_net_profit_probability must be in (0, 1)")
        if int(self.max_quote_age_seconds) <= 0:
            raise TrendContractError("max_quote_age_seconds must be > 0")

    @property
    def confirmation_slots(self) -> tuple[str, ...]:
        """窗口起点起每 ``check_interval_minutes`` 一个确认时点，**不含**窗口终点。

        终点排除的理由：成交要用确认后严格更晚的已完成 bar，在终点确认就没有
        属于本窗口的下一根 bar 可成交了。
        """
        start = _parse_clock(self.entry_window_start, field_name="entry_window_start")
        end = _parse_clock(self.entry_window_end, field_name="entry_window_end")
        start_minutes = start.hour * 60 + start.minute
        end_minutes = end.hour * 60 + end.minute
        slots: list[str] = []
        cursor = start_minutes
        while cursor < end_minutes:
            slots.append(f"{cursor // 60:02d}:{cursor % 60:02d}")
            cursor += int(self.check_interval_minutes)
        return tuple(slots)

    @property
    def latest_confirmation_clock(self) -> str:
        return self.confirmation_slots[-1]

    @property
    def exit_attempt_index(self) -> int:
        """计划退出日在持仓序列中的下标（入场日=第 1 日 → holding_days-1）。"""
        return max(0, int(self.holding_days) - 1)

    def confirmation_datetimes(self, trading_day: date | datetime) -> tuple[datetime, ...]:
        day = _to_datetime(trading_day).date()
        stamps: list[datetime] = []
        for slot in self.confirmation_slots:
            hour, minute = (int(part) for part in slot.split(":"))
            stamps.append(datetime(day.year, day.month, day.day, hour, minute))
        return tuple(stamps)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "strategy": self.strategy,
            "timezone": self.timezone,
            "entry_window_start": self.entry_window_start,
            "entry_window_end": self.entry_window_end,
            "check_interval_minutes": int(self.check_interval_minutes),
            "fill_bar_offset": int(self.fill_bar_offset),
            "reference_notional": float(self.reference_notional),
            "take_profit_pct": float(self.take_profit_pct),
            "stop_loss_pct": float(self.stop_loss_pct),
            "holding_days": int(self.holding_days),
            "entry_day_counts_as_holding_day": bool(self.entry_day_counts_as_holding_day),
            "same_bar_conflict_policy": self.same_bar_conflict_policy,
            "exit_defer_max_sessions": int(self.exit_defer_max_sessions),
            "max_final_recommendations": int(self.max_final_recommendations),
            "min_net_profit_probability": float(self.min_net_profit_probability),
            "max_quote_age_seconds": int(self.max_quote_age_seconds),
            "execution_price_basis": self.execution_price_basis,
            "cost_model_version": self.cost_model_version,
            "corporate_action_uncertain": bool(self.corporate_action_uncertain),
            "confirmation_slots": list(self.confirmation_slots),
        }
        return payload

    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


DEFAULT_TREND_CONTRACT = TrendStrategyContract()


# ---------------------------------------------------------------------------
# 分钟 bar 时间语义（线上与历史共用同一套）
# ---------------------------------------------------------------------------


def bar_end_time(bar: Mapping[str, Any], *, fallback: datetime) -> datetime:
    """bar 的**完成**时刻（区间终点）。

    只有以终点标注的 bar 才可能被判为"已完成"；用区间起点会让确认读到尚未
    收盘的那根 bar（look-ahead）。
    """
    for key in ("bar_time", "end_time", "datetime", "time", "timestamp"):
        value = bar.get(key)
        if isinstance(value, datetime):
            return value
    return fallback


def normalize_bars(
    bars: Sequence[tuple[datetime, Mapping[str, Any]]],
) -> list[tuple[datetime, Mapping[str, Any]]]:
    """统一按 bar 终点标注并按时间升序；同终点后到的覆盖先到的。"""
    keyed: dict[datetime, Mapping[str, Any]] = {}
    for hint, bar in bars:
        keyed[bar_end_time(bar, fallback=hint)] = bar
    return sorted(keyed.items(), key=lambda item: item[0])


def completed_bars(
    bars: Sequence[tuple[datetime, Mapping[str, Any]]],
    *,
    as_of: datetime,
) -> list[tuple[datetime, Mapping[str, Any]]]:
    """``as_of`` 时点已完全结束的 bar（``bar_end <= as_of``）。"""
    return [(end, bar) for end, bar in normalize_bars(bars) if end <= as_of]


def bars_after(
    bars: Sequence[tuple[datetime, Mapping[str, Any]]],
    *,
    after: datetime,
) -> list[tuple[datetime, Mapping[str, Any]]]:
    """严格晚于 ``after`` 的已完成 bar，升序。"""
    return [(end, bar) for end, bar in normalize_bars(bars) if end > after]


def buy_lot_quantity(price: float, reference_notional: float) -> int:
    """参考金额 → 申报数量：整手（100 股）向下取整。"""
    if price <= 0:
        return 0
    return int(reference_notional // float(price)) // 100 * 100


def _positive_float(bar: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = bar.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


#: 明确表示"这段时间不能交易"的状态码；缺失或 unknown 不算停牌（走未知状态分支）。
SUSPENDED_TRADE_STATUS = frozenset({
    "s", "suspend", "suspended", "halt", "halted", "h", "p", "停牌", "暂停交易",
})


def _is_suspended(bar: Mapping[str, Any]) -> bool:
    """停牌判定：显式标记优先，其次看交易状态码。

    ``trade_status`` 是数据源里唯一会说"停牌"的字段（tushare ``suspend_d`` /
    行情状态码），只认显式标记会让停牌股被当成可正常买入。
    """
    for key in ("suspended", "is_suspended", "suspend"):
        value = bar.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "y", "yes", "suspended", "halt"}:
                return True
        elif bool(value):
            return True
    raw = bar.get("trade_status")
    if raw is not None and str(raw).strip().lower() in SUSPENDED_TRADE_STATUS:
        return True
    return False


def _is_sellable(bar: Mapping[str, Any]) -> bool:
    """能不能卖：停牌不可；收盘封跌停不可（跌停卖队排队不假设成交）。"""
    if _is_suspended(bar):
        return False
    close = _positive_float(bar, "close", "open")
    down_limit = _positive_float(bar, "down_limit", "limit_down", "low_limit")
    if close is None:
        return False
    if down_limit is not None and close <= down_limit:
        return False
    return True


def _trade_status_declared(bar: Mapping[str, Any]) -> bool:
    raw = bar.get("trade_status")
    if raw is None:
        return False
    return str(raw).strip().lower() not in {"", "unknown", "nan", "none"}


@dataclass(frozen=True)
class TailConfirmationContext:
    """喂给确认谓词的全部信息——谓词不得自行读取未列出的未来数据。"""

    symbol: str
    trading_day: date
    confirmation_slot: datetime
    #: 截至本确认时点已完成的分钟 bar（含本时点那根）。
    completed: tuple[tuple[datetime, Mapping[str, Any]], ...]
    latest_price_raw: float | None
    #: 夜扫阶段冻结的特征快照；确认谓词只能读它。
    overnight_features: Mapping[str, Any]
    model_probabilities: Mapping[str, float]
    contract: TrendStrategyContract


def hard_gate_confirmation(context: TailConfirmationContext) -> tuple[bool, str]:
    """尾盘确认的默认谓词：**只用硬门** —— 线上与历史重建共用同一个函数（§3.4/§4）。

    新路径不让旧综合分、S/A 等级、分歧试探决定资格，模型分只参与最终排序
    （见 ``rank_final_recommendations``）。这里唯一要复核的是"这个确认时点有已完成的
    最新价"；涨停锁死、停牌、报价陈旧度、资金与风险约束都由 ``evaluate_tail_entry``
    和调用方的门负责，谓词本身不读 ``completed`` 之外的任何数据。

    放在契约模块里而不是服务的私有函数里，是因为 §4 要求"相同输入下线上与历史产生
    一致的筛选判定"—— 两边引用同一个对象才是结构上的保证，各写一份迟早会漂。
    """
    if context.latest_price_raw is None:
        return False, "no_completed_minute_bar"
    return True, ""


@dataclass(frozen=True)
class TailEntryDecision:
    symbol: str
    confirmed: bool
    #: 确认失败原因（``confirmed=False``）；成交失败另走 ``no_fill_reason``。
    reason: str
    confirmation_slot: datetime | None = None
    fill_time: datetime | None = None
    fill_price_raw: float | None = None
    net_fill_price: float | None = None
    quantity: int = 0
    entry_amount: float = 0.0
    buy_cost: float = 0.0
    slippage_increment: float = 0.0
    no_fill_reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return bool(self.confirmed and self.net_fill_price and self.quantity > 0)


def evaluate_tail_entry(
    *,
    symbol: str,
    trading_day: date | datetime,
    minute_bars: Sequence[tuple[datetime, Mapping[str, Any]]],
    confirmation: Callable[[TailConfirmationContext], tuple[bool, str]],
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    reference_notional: float | None = None,
    model_probabilities: Mapping[str, float] | None = None,
    overnight_features: Mapping[str, Any] | None = None,
    quote_as_of: datetime | None = None,
    slippage_ratio: float = 0.0,
    cost_estimator: Callable[[str, float, int, datetime], float] | None = None,
    price_ticker: Callable[[float, str], float] | None = None,
) -> TailEntryDecision:
    """逐确认时点跑确认谓词，对首个通过的时点按契约模拟下一根 bar 成交。

    线上与历史调用同一个函数：线上传 ``quote_as_of``（当前时钟）以过滤未完成 bar
    并做陈旧度检查；历史传 None，即用全部已完成历史 bar。只要传同一个 ``confirmation``
    谓词，两条路径的判定必然一致（计划 §4 工程验收要求）。

    ``no_fill`` 与"亏损"严格分开：确认了但买不进（停牌 / 涨停锁死 / 无有效价格 /
    不足一手 / 确认之后没有已完成 bar）→ ``confirmed=True`` 且 ``no_fill_reason`` 非空。
    缺 bar 的原因如实写 ``no_completed_bar_after_confirmation``，不冒充停牌。
    """
    day = _to_datetime(trading_day).date()
    notional = float(reference_notional or contract.reference_notional)
    bars = normalize_bars(minute_bars)
    probs = dict(model_probabilities or {})
    features = dict(overnight_features or {})

    last: TailEntryDecision | None = None
    for slot in contract.confirmation_datetimes(day):
        if quote_as_of is None:
            eligible = bars
        else:
            eligible = [(end, bar) for end, bar in bars if end <= quote_as_of]
            # 陈旧度是对整次确认的门，不是逐根 bar 的筛选：用最新已完成 bar 的年龄判，
            # 否则会把早于确认时点的 bar 换掉，让线上用更晚的 bar 去"成交"早先的确认。
            if eligible:
                latest_end = eligible[-1][0]
                if (quote_as_of - latest_end).total_seconds() > contract.max_quote_age_seconds:
                    return TailEntryDecision(
                        symbol=symbol,
                        confirmed=False,
                        reason="realtime_snapshot_stale",
                        confirmation_slot=slot,
                        details={
                            "quote_as_of": quote_as_of.isoformat(),
                            "latest_bar_end": latest_end.isoformat(),
                        },
                    )
            elif (quote_as_of - datetime(
                day.year, day.month, day.day,
                *_clock_parts(contract.entry_window_start),
            )).total_seconds() > contract.max_quote_age_seconds:
                return TailEntryDecision(
                    symbol=symbol,
                    confirmed=False,
                    reason="no_completed_minute_bar",
                    details={"quote_as_of": quote_as_of.isoformat()},
                )
            # 线上只评估"已经到了"的确认时点；未到的时点不能提前判断。
            if slot > quote_as_of:
                break
        completed = [(end, bar) for end, bar in eligible if end <= slot]
        latest_price = _positive_float(completed[-1][1], "close", "price") if completed else None
        confirmed, reason = confirmation(
            TailConfirmationContext(
                symbol=symbol,
                trading_day=day,
                confirmation_slot=slot,
                completed=tuple(completed),
                latest_price_raw=latest_price,
                overnight_features=features,
                model_probabilities=probs,
                contract=contract,
            )
        )
        if not confirmed:
            last = TailEntryDecision(
                symbol=symbol,
                confirmed=False,
                reason=reason or "tail_confirmation_failed",
                confirmation_slot=slot,
            )
            continue

        following = [(end, bar) for end, bar in eligible if end > slot]
        fill_index = int(contract.fill_bar_offset) - 1
        if len(following) <= fill_index:
            return TailEntryDecision(
                symbol=symbol, confirmed=True, reason="", confirmation_slot=slot,
                no_fill_reason="no_completed_bar_after_confirmation",
                details={"bars_after_slot": len(following)},
            )
        fill_time, fill_bar = following[fill_index]
        if _is_suspended(fill_bar):
            return TailEntryDecision(
                symbol=symbol, confirmed=True, reason="", confirmation_slot=slot,
                no_fill_reason="suspended",
                details={"fill_time": fill_time.isoformat()},
            )
        raw_price = _positive_float(fill_bar, "open", "price", "close")
        up_limit = _positive_float(fill_bar, "up_limit", "limit_up", "high_limit")
        if raw_price is None or up_limit is None:
            return TailEntryDecision(
                symbol=symbol, confirmed=True, reason="", confirmation_slot=slot,
                no_fill_reason="no_valid_price_data",
                details={"fill_time": fill_time.isoformat(), "price": raw_price,
                         "up_limit": up_limit},
            )
        if raw_price >= up_limit:
            return TailEntryDecision(
                symbol=symbol, confirmed=True, reason="", confirmation_slot=slot,
                no_fill_reason="limit_up_locked",
                details={"fill_time": fill_time.isoformat(), "price": raw_price,
                         "up_limit": up_limit},
            )

        slipped = raw_price * (1.0 + max(0.0, float(slippage_ratio)))
        ticked = price_ticker(slipped, "buy") if price_ticker else round(slipped, 2)
        quantity = buy_lot_quantity(ticked, notional)
        if quantity <= 0:
            return TailEntryDecision(
                symbol=symbol, confirmed=True, reason="", confirmation_slot=slot,
                no_fill_reason="below_lot_size",
                details={"fill_price": ticked, "reference_notional": notional},
            )
        amount = ticked * quantity
        buy_cost = (
            float(cost_estimator("buy", ticked, quantity, fill_time)) if cost_estimator else 0.0
        )
        return TailEntryDecision(
            symbol=symbol,
            confirmed=True,
            reason="",
            confirmation_slot=slot,
            fill_time=fill_time,
            fill_price_raw=raw_price,
            net_fill_price=ticked,
            quantity=quantity,
            entry_amount=amount,
            buy_cost=buy_cost,
            slippage_increment=ticked - raw_price,
            details={"price_basis": contract.execution_price_basis},
        )

    if last is not None:
        return last
    return TailEntryDecision(symbol=symbol, confirmed=False, reason="no_confirmation_slot")


# ---------------------------------------------------------------------------
# 出场与净收益（标签与回测共用同一实现）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TailExitResult:
    symbol: str
    status: str
    realized: bool
    net_profit: bool
    net_return: float
    gross_return: float
    entry_date: datetime
    exit_date: datetime | None
    entry_price: float
    exit_price: float
    quantity: int
    buy_cost: float
    sell_cost: float
    reason: str
    take_profit_hit: bool = False
    stop_loss_hit: bool = False
    ambiguous_same_bar: bool = False
    gap_exit: bool = False
    deferred_sessions: int = 0
    sessions_held: int = 0
    corporate_action_uncertain: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def _uncertain_exit(
    *,
    symbol: str,
    entry_date: datetime,
    entry_price: float,
    quantity: int,
    buy_cost: float,
    reason: str,
    sessions_held: int = 0,
    deferred_sessions: int = 0,
    corporate_action_uncertain: bool = False,
) -> TailExitResult:
    return TailExitResult(
        symbol=symbol,
        status=STATUS_UNCERTAIN,
        realized=False,
        net_profit=False,
        net_return=0.0,
        gross_return=0.0,
        entry_date=entry_date,
        exit_date=None,
        entry_price=float(entry_price),
        exit_price=0.0,
        quantity=int(quantity),
        buy_cost=float(buy_cost),
        sell_cost=0.0,
        reason=reason,
        sessions_held=int(sessions_held),
        deferred_sessions=int(deferred_sessions),
        corporate_action_uncertain=bool(corporate_action_uncertain),
    )


def simulate_tail_exit(
    *,
    symbol: str,
    entry_date: date | datetime,
    entry_price: float,
    quantity: int,
    buy_cost: float,
    daily_bars: Sequence[tuple[date | datetime, Mapping[str, Any]]],
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    cost_estimator: Callable[[str, float, int, datetime], float] | None = None,
    price_ticker: Callable[[float, str], float] | None = None,
) -> TailExitResult:
    """按契约跑出场：T+1、跳空、止损优先、第 ``holding_days`` 日退出、不可卖则顺延。

    ``daily_bars`` 必须是 raw 日线并**包含入场日那根**（它决定第 1 日的位置）；
    顺序不要求有序，函数自行按日期排序。净收益口径：

    ``net_return = (卖出净额 - 卖出成本 - 买入成本) / (买入金额 + 买入成本) - 1``
    """
    entry_dt = _to_datetime(entry_date)
    if entry_price <= 0 or quantity <= 0:
        raise TrendContractError("entry_price and quantity must be > 0 to simulate exit")

    ordered = sorted(
        ((_to_datetime(bar_day).date(), _to_datetime(bar_day), bar) for bar_day, bar in daily_bars),
        key=lambda item: item[0],
    )
    sessions = [(dt, bar) for day, dt, bar in ordered if day >= entry_dt.date()]
    if not sessions or sessions[0][0].date() != entry_dt.date():
        return _uncertain_exit(
            symbol=symbol, entry_date=entry_dt, entry_price=entry_price, quantity=quantity,
            buy_cost=buy_cost, reason="entry_session_missing",
        )

    tp_level = entry_price * (1.0 + float(contract.take_profit_pct))
    sl_level = entry_price * (1.0 - float(contract.stop_loss_pct))
    attempt_index = contract.exit_attempt_index
    defer_cap = int(contract.exit_defer_max_sessions)
    invested = entry_price * quantity + buy_cost

    def _tick(price: float, side: str) -> float:
        return price_ticker(price, side) if price_ticker else round(price, 2)

    def _realize(
        *,
        exit_dt: datetime,
        exit_price_raw: float,
        tp_hit: bool,
        sl_hit: bool,
        ambiguous: bool,
        gap_exit: bool,
        deferred: int,
    ) -> TailExitResult:
        gross_price = _tick(exit_price_raw, "sell")
        amount = gross_price * quantity
        sell_cost = (
            float(cost_estimator("sell", gross_price, quantity, exit_dt))
            if cost_estimator
            else 0.0
        )
        net_return = (amount - sell_cost) / invested - 1.0
        gross_return = amount / (entry_price * quantity) - 1.0
        held = sum(1 for dt, _ in sessions if dt.date() <= exit_dt.date())
        return TailExitResult(
            symbol=symbol,
            status=STATUS_FILLED,
            realized=True,
            net_profit=bool(net_return > 0),
            net_return=float(net_return),
            gross_return=float(gross_return),
            entry_date=entry_dt,
            exit_date=exit_dt,
            entry_price=float(entry_price),
            exit_price=float(gross_price),
            quantity=int(quantity),
            buy_cost=float(buy_cost),
            sell_cost=float(sell_cost),
            reason="",
            take_profit_hit=tp_hit,
            stop_loss_hit=sl_hit,
            ambiguous_same_bar=ambiguous,
            gap_exit=gap_exit,
            deferred_sessions=int(deferred),
            sessions_held=int(held),
            details={"price_basis": contract.execution_price_basis},
        )

    deferred = 0
    for index in range(1, len(sessions)):
        bar_dt, bar = sessions[index]
        if contract.corporate_action_uncertain and bool(bar.get("corporate_action_uncertain")):
            return _uncertain_exit(
                symbol=symbol, entry_date=entry_dt, entry_price=entry_price,
                quantity=quantity, buy_cost=buy_cost,
                reason="corporate_action_uncertain", sessions_held=index,
                deferred_sessions=deferred, corporate_action_uncertain=True,
            )
        if not _trade_status_declared(bar):
            return _uncertain_exit(
                symbol=symbol, entry_date=entry_dt, entry_price=entry_price,
                quantity=quantity, buy_cost=buy_cost, reason=UNKNOWN_TRADE_STATUS,
                sessions_held=index, deferred_sessions=deferred,
            )
        open_price = _positive_float(bar, "open", "close")
        high_price = _positive_float(bar, "high", "open", "close")
        low_price = _positive_float(bar, "low", "open", "close")
        close_price = _positive_float(bar, "close", "open")
        if open_price is None or high_price is None or low_price is None:
            return _uncertain_exit(
                symbol=symbol, entry_date=entry_dt, entry_price=entry_price,
                quantity=quantity, buy_cost=buy_cost, reason="no_valid_price_data",
                sessions_held=index, deferred_sessions=deferred,
            )

        sl_touched = low_price <= sl_level
        tp_touched = high_price >= tp_level
        ambiguous = bool(sl_touched and tp_touched)
        if ambiguous:
            # 一根日线里先触及谁无法从 OHLC 判定 → 止损优先（保守）。
            tp_touched = False

        if _is_sellable(bar):
            # 顺延交易日数按计划退出日与实际退出日之差计（计划当天卖不掉也算顺延）。
            carried = max(deferred, index - attempt_index)
            if sl_touched:
                gap = open_price <= sl_level
                return _realize(
                    exit_dt=bar_dt,
                    exit_price_raw=open_price if gap else sl_level,
                    tp_hit=False, sl_hit=True, ambiguous=ambiguous,
                    gap_exit=bool(gap), deferred=carried,
                )
            if tp_touched:
                gap = open_price >= tp_level
                return _realize(
                    exit_dt=bar_dt,
                    exit_price_raw=open_price if gap else tp_level,
                    tp_hit=True, sl_hit=False, ambiguous=ambiguous,
                    gap_exit=bool(gap), deferred=carried,
                )
            if index >= attempt_index:
                if close_price is None:
                    return _uncertain_exit(
                        symbol=symbol, entry_date=entry_dt, entry_price=entry_price,
                        quantity=quantity, buy_cost=buy_cost,
                        reason="no_valid_price_data", sessions_held=index,
                        deferred_sessions=deferred,
                    )
                return _realize(
                    exit_dt=bar_dt, exit_price_raw=close_price,
                    tp_hit=False, sl_hit=False, ambiguous=False,
                    gap_exit=False, deferred=carried,
                )
        elif index > attempt_index:
            deferred += 1
            if deferred > defer_cap:
                return _uncertain_exit(
                    symbol=symbol, entry_date=entry_dt, entry_price=entry_price,
                    quantity=quantity, buy_cost=buy_cost, reason="exit_not_executable",
                    sessions_held=index, deferred_sessions=deferred,
                )

    # bar 序列在计划退出日之前用尽：数据末尾，不得以强平价生成已实现盈亏。
    return _uncertain_exit(
        symbol=symbol, entry_date=entry_dt, entry_price=entry_price, quantity=quantity,
        buy_cost=buy_cost, reason="insufficient_data_at_series_end",
        sessions_held=max(0, len(sessions) - 1), deferred_sessions=deferred,
    )


def summarize_tail_trade(
    *,
    entry: TailEntryDecision,
    exit_result: TailExitResult | None,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> dict[str, Any]:
    """成交状态三分类（filled / not_filled / uncertain）+ 净盈亏布尔与成熟时刻。

    成交率必须与净盈利率分开报告：未成交不得计成亏损，也不得计成盈利。
    """
    if not entry.filled:
        reason = entry.no_fill_reason if entry.confirmed else entry.reason
        return {
            "symbol": entry.symbol,
            "status": STATUS_NOT_FILLED,
            "fill_rate_bucket": 0,
            "realized": False,
            "net_profit": None,
            "net_return": None,
            "reason": reason or "not_filled",
            "confirmation_slot": (
                entry.confirmation_slot.isoformat() if entry.confirmation_slot else None
            ),
            "contract_digest": contract.digest(),
        }
    if exit_result is None:
        return {
            "symbol": entry.symbol,
            "status": STATUS_UNCERTAIN,
            "fill_rate_bucket": 1,
            "realized": False,
            "net_profit": None,
            "net_return": None,
            "reason": "exit_not_simulated",
            "contract_digest": contract.digest(),
        }
    return {
        "symbol": entry.symbol,
        "status": exit_result.status,
        "fill_rate_bucket": 1,
        "realized": exit_result.realized,
        "net_profit": bool(exit_result.net_profit) if exit_result.realized else None,
        "net_return": exit_result.net_return if exit_result.realized else None,
        "reason": exit_result.reason,
        "entry_date": entry.fill_time.isoformat() if entry.fill_time else None,
        "exit_date": exit_result.exit_date.isoformat() if exit_result.exit_date else None,
        "quantity": exit_result.quantity,
        "buy_cost": exit_result.buy_cost,
        "sell_cost": exit_result.sell_cost,
        "take_profit_hit": exit_result.take_profit_hit,
        "stop_loss_hit": exit_result.stop_loss_hit,
        "ambiguous_same_bar": exit_result.ambiguous_same_bar,
        "gap_exit": exit_result.gap_exit,
        "deferred_sessions": exit_result.deferred_sessions,
        "corporate_action_uncertain": exit_result.corporate_action_uncertain,
        "contract_digest": contract.digest(),
    }


def label_maturity_time(
    exit_result: TailExitResult,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> datetime | None:
    """标签成熟时刻 = 实际可成交退出时刻；未成交/不确定 → None（永不猜）。"""
    if not exit_result.realized or exit_result.exit_date is None:
        return None
    return exit_result.exit_date


# ---------------------------------------------------------------------------
# 最终推荐排序（准入规则；线上与历史回测共用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelIdentity:
    """决策时必须可验证的模型身份；任一字段缺失或不符 → 0 只推荐。

    ``training_manifest_id`` 是计划 §3.1 要求绑定的"训练 manifest"：它记录**当时实际
    加载的是哪份 serving manifest**，缺失不改变准入（准入仍由上面这些字段裁决），
    但必须作为"记录失败"被看见 —— 静默留空就等于把绑不上说成绑上了。
    """

    model_id: str
    artifact_content_hash: str
    training_commit: str
    runtime_commit: str
    feature_compute_version: int
    label_policy_id: str
    contract_digest: str
    training_manifest_id: str = ""

    def validate(self, contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT) -> str:
        if not str(self.model_id).strip():
            return "model_id_missing"
        if not str(self.artifact_content_hash).strip():
            return "artifact_content_hash_missing"
        if not str(self.training_commit).strip():
            return "training_commit_unknown"
        if not str(self.runtime_commit).strip():
            return "runtime_commit_unknown"
        if str(self.training_commit) != str(self.runtime_commit):
            return "training_runtime_commit_mismatch"
        if int(self.feature_compute_version) <= 0:
            return "feature_compute_version_invalid"
        if not str(self.label_policy_id).strip():
            return "label_policy_id_missing"
        if str(self.contract_digest) != contract.digest():
            return "strategy_contract_digest_mismatch"
        return ""


@dataclass(frozen=True)
class RankedCandidate:
    symbol: str
    probability: float | None
    reason: str
    accepted: bool
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FinalRecommendationResult:
    trade_date: date
    selected: tuple[RankedCandidate, ...]
    rejected: tuple[RankedCandidate, ...]
    counts: Mapping[str, Any]
    contract_digest: str
    blocking_reason: str = ""

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.selected)


def _probability_of(row: Mapping[str, Any], probability_field: str) -> float | None:
    raw = row.get(probability_field)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not (0.0 <= value <= 1.0):
        return None
    return value


def rank_final_recommendations(
    *,
    trade_date: date | datetime,
    rows: Sequence[Mapping[str, Any]],
    model_identity: ModelIdentity | None,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    probability_field: str = NET_PROFIT_PROBABILITY_FIELD,
) -> FinalRecommendationResult:
    """按新净盈利概率降序、股票代码升序取前 N；不补名额，旧分数不参与资格。

    模型身份不可验证 / 数据不足 / 风险不允许 → 输出 0 只并写 ``blocking_reason``。
    """
    day = _to_datetime(trade_date).date()
    identity_error = (
        model_identity.validate(contract)
        if model_identity is not None
        else "model_identity_missing"
    )
    if identity_error:
        return FinalRecommendationResult(
            trade_date=day,
            selected=(),
            rejected=tuple(
                RankedCandidate(
                    symbol=str(row.get("symbol", "")), probability=None,
                    reason=identity_error, accepted=False, details=dict(row),
                )
                for row in rows
            ),
            counts={"input": len(rows), "unique_symbols": 0, "eligible": 0,
                    "selected": 0, "rejected": len(rows)},
            contract_digest=contract.digest(),
            blocking_reason=identity_error,
        )

    deduped: dict[str, Mapping[str, Any]] = {}
    duplicates = 0
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        if not symbol:
            continue
        if symbol in deduped:
            duplicates += 1
            incoming = _probability_of(row, probability_field) or 0.0
            kept = _probability_of(deduped[symbol], probability_field) or 0.0
            if incoming > kept:
                deduped[symbol] = row
            continue
        deduped[symbol] = row

    ranked: list[tuple[float, str, Mapping[str, Any]]] = []
    rejected: list[RankedCandidate] = []
    for symbol, row in deduped.items():
        probability = _probability_of(row, probability_field)
        if probability is None:
            rejected.append(RankedCandidate(symbol=symbol, probability=None,
                                           reason="missing_probability", accepted=False,
                                           details=dict(row)))
            continue
        if not row.get("tradeable", True):
            rejected.append(RankedCandidate(
                symbol=symbol, probability=probability,
                reason=str(row.get("not_tradeable_reason") or "not_tradeable"),
                accepted=False, details=dict(row)))
            continue
        if str(row.get("risk_state", "")).strip().lower() in {"blocked", "risk_blocked"}:
            rejected.append(RankedCandidate(symbol=symbol, probability=probability,
                                           reason="risk_blocked", accepted=False,
                                           details=dict(row)))
            continue
        if probability < float(contract.min_net_profit_probability):
            rejected.append(RankedCandidate(symbol=symbol, probability=probability,
                                           reason="below_threshold", accepted=False,
                                           details=dict(row)))
            continue
        ranked.append((probability, symbol, row))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    cap = int(contract.max_final_recommendations)
    selected: list[RankedCandidate] = []
    for position, (probability, symbol, row) in enumerate(ranked):
        if position < cap:
            selected.append(RankedCandidate(symbol=symbol, probability=probability,
                                           reason="", accepted=True, details=dict(row)))
        else:
            rejected.append(RankedCandidate(symbol=symbol, probability=probability,
                                           reason="cap_exceeded", accepted=False,
                                           details=dict(row)))
    return FinalRecommendationResult(
        trade_date=day,
        selected=tuple(selected),
        rejected=tuple(rejected),
        counts={
            "input": len(rows),
            "unique_symbols": len(deduped),
            "duplicates_dropped": duplicates,
            "eligible": len(ranked),
            "selected": len(selected),
            "rejected": len(rejected),
        },
        contract_digest=contract.digest(),
    )


# ---------------------------------------------------------------------------
# 配置接线：契约是唯一声明；现存冲突副本必须可见（不静默取其一）
# ---------------------------------------------------------------------------


def contract_from_config(config: Any) -> TrendStrategyContract:
    """从 ``config.trend_strategy`` 构造契约；缺失即 raise，不回退默认。"""
    block = getattr(config, "trend_strategy", None)
    if block is None:
        raise TrendContractError("config.trend_strategy is required for the trend path")
    mapping = block.model_dump() if hasattr(block, "model_dump") else dict(block)
    return TrendStrategyContract(**mapping)


def audit_strategy_contract_conflicts(config: Any) -> list[dict[str, Any]]:
    """列出与契约不一致的旧声明位置，供第一阶段逐条修掉。

    这些是**现存事实**（配置声明 vs 开盘入场评估入口的口径冲突），不是假设。
    """
    try:
        contract = contract_from_config(config)
    except TrendContractError as exc:
        return [{"site": "config.trend_strategy", "problem": str(exc)}]

    findings: list[dict[str, Any]] = []
    labels = getattr(config, "labels", None)
    if labels is not None:
        mismatched = {}
        for key, want in (
            ("take_profit_pct", float(contract.take_profit_pct)),
            ("stop_loss_pct", float(contract.stop_loss_pct)),
            ("horizon_days", float(contract.holding_days)),
        ):
            got = float(getattr(labels, key, want))
            if abs(got - want) > 1e-12:
                mismatched[key] = got
        if mismatched:
            findings.append({"site": "labels", "declares": mismatched, "contract_wants": {
                "take_profit_pct": float(contract.take_profit_pct),
                "stop_loss_pct": float(contract.stop_loss_pct),
                "horizon_days": float(contract.holding_days),
            }})
        if str(getattr(labels, "pnl_price_basis", "")) == "next_tradable_open":
            findings.append({
                "site": "labels.pnl_price_basis",
                "declares": "next_tradable_open",
                "problem": "开盘入场口径与尾盘确认契约不一致",
            })

    soup = getattr(config, "soup_strategy", None)
    if soup is not None:
        gaps: dict[str, Any] = {}
        soup_stop = float(getattr(soup, "stop_loss", 0.0))
        if abs(soup_stop - float(contract.stop_loss_pct) * 100.0) > 1e-9:
            gaps["stop_loss"] = float(getattr(soup, "stop_loss", 0.0))
        if int(getattr(soup, "max_hold_days", 0)) != int(contract.holding_days):
            gaps["max_hold_days"] = int(getattr(soup, "max_hold_days", 0))
        if list(getattr(soup, "entry_window", [])) != [
            contract.entry_window_start, contract.entry_window_end
        ]:
            gaps["entry_window"] = list(getattr(soup, "entry_window", []))
        if int(getattr(soup, "max_holdings", 0)) != int(contract.max_final_recommendations):
            gaps["max_holdings"] = int(getattr(soup, "max_holdings", 0))
        if gaps:
            findings.append({"site": "soup_strategy", "declares": gaps,
                             "problem": "与契约不一致或存在无消费者的声明"})

    asof = getattr(config, "asof_backtest", None)
    if asof is not None:
        gaps = {}
        asof_tp = float(getattr(asof, "take_profit_pct", 0.0))
        if abs(asof_tp - float(contract.take_profit_pct)) > 1e-12:
            gaps["take_profit_pct"] = float(getattr(asof, "take_profit_pct", 0.0))
        if abs(float(getattr(asof, "stop_loss_pct", 0.0)) - float(contract.stop_loss_pct)) > 1e-12:
            gaps["stop_loss_pct"] = float(getattr(asof, "stop_loss_pct", 0.0))
        if int(getattr(asof, "default_horizon_days", 0)) != int(contract.holding_days):
            gaps["default_horizon_days"] = int(getattr(asof, "default_horizon_days", 0))
        if gaps:
            findings.append({"site": "asof_backtest", "declares": gaps,
                             "problem": "历史验证口径与契约不一致"})

    matcher = getattr(config, "backtest_matcher", None)
    if matcher is not None and int(getattr(matcher, "max_exit_carry_days", 0)) < int(
        contract.exit_defer_max_sessions
    ):
        findings.append({
            "site": "backtest_matcher.max_exit_carry_days",
            "declares": int(getattr(matcher, "max_exit_carry_days", 0)),
            "problem": "顺延上限小于契约，退出会假成功",
        })
    return findings


__all__ = [
    "DEFAULT_TREND_CONTRACT",
    "FUNNEL_LAYERS",
    "NET_PROFIT_PROBABILITY_FIELD",
    "STATUS_FILLED",
    "STATUS_NOT_FILLED",
    "STATUS_UNCERTAIN",
    "TREND_STRATEGY_CONTRACT_VERSION",
    "UNKNOWN_TRADE_STATUS",
    "FinalRecommendationResult",
    "ModelIdentity",
    "RankedCandidate",
    "TailConfirmationContext",
    "hard_gate_confirmation",
    "TailEntryDecision",
    "TailExitResult",
    "TrendContractError",
    "TrendStrategyContract",
    "audit_strategy_contract_conflicts",
    "bar_end_time",
    "buy_lot_quantity",
    "completed_bars",
    "contract_from_config",
    "evaluate_tail_entry",
    "label_maturity_time",
    "normalize_bars",
    "rank_final_recommendations",
    "simulate_tail_exit",
    "summarize_tail_trade",
]
