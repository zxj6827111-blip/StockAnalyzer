"""主题评分加分 provider（M12 双通道之一：评分加分）。

``ThemeBoostProvider``：symbol → theme_boost 分量 [0,1]（主题热度 × 确认
强度 × 时间衰减，经 ``boost_max_lift`` 封顶），由 pipeline 在 components
中条件加入，权重来自 ``score.weights["theme_boost"]``（默认 0.0，boost
模式起步 0.05——权重 0.05 × 分量 1.0 × 100 = +5 分，即单股加分封顶 +5）。

``NeutralThemeBoostProvider``：as-of 回测中性化（照 ``NeutralNewsSignalProvider``
先例）——恒定 0 分量。权重为 0 时 ScoreEngine 归一化本就会抵消其影响，
显式中性化是双保险：即使误配权重 > 0，回测也不会被当前时点的主题状态污染。

数据源：``theme_state.json``（theme_service 原子写）——mtime 缓存读取，
shadow 模式下 state 里 boost 表为空（全 dry-run），自然退化为恒 0。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.config import MacroThemeConfig

_NEUTRAL_VALUE = 0.0
# state 文件最大容忍年龄（小时）：超龄视为过期，boost 全部失效（防陈旧主题
# 状态长期污染评分——主题热度本质上是短周期信息）。
_STATE_MAX_AGE_HOURS = 72.0


@dataclass(slots=True)
class ThemeBoostState:
    """从 theme_state.json 载入的当前激活 boost 表（symbol → 分量）。"""

    mode: str = "off"
    generated_at: str = ""
    boost_by_symbol: dict[str, float] = field(default_factory=dict)
    active_themes: list[str] = field(default_factory=list)
    dry_run: bool = True

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ThemeBoostState:
        boost_raw = payload.get("boost_by_symbol", {})
        boost_by_symbol: dict[str, float] = {}
        if isinstance(boost_raw, dict):
            for symbol, value in boost_raw.items():
                normalized = _normalize_symbol(str(symbol))
                parsed = _as_float(value)
                if normalized and parsed is not None and isfinite(parsed):
                    boost_by_symbol[normalized] = max(0.0, min(1.0, parsed))
        themes_raw = payload.get("active_themes", [])
        active_themes = (
            [str(item) for item in themes_raw if str(item).strip()]
            if isinstance(themes_raw, list)
            else []
        )
        return cls(
            mode=str(payload.get("mode", "off")),
            generated_at=str(payload.get("generated_at", "")),
            boost_by_symbol=boost_by_symbol,
            active_themes=active_themes,
            dry_run=bool(payload.get("dry_run", True)),
        )


class ThemeBoostProvider:
    """symbol → theme_boost 分量（读 theme_state.json，mtime 缓存）。"""

    def __init__(
        self,
        *,
        config: MacroThemeConfig,
        state_path: str | Path = "",
        now_func: Any = None,
    ) -> None:
        self._config = config
        self._state_path = _resolve_state_path(state_path or config.state_path)
        self._now_func = now_func if now_func is not None else _utc_now
        self._cached_mtime: float | None = None
        self._cached_state: ThemeBoostState | None = None

    @property
    def state_path(self) -> Path:
        return self._state_path

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = bars, features, strategy
        state = self._load_state()
        if state is None:
            return _NEUTRAL_VALUE
        if state.mode != "boost" or state.dry_run:
            # off/shadow 或 dry-run 标记未清：恒 0（不进入评分）。
            return _NEUTRAL_VALUE
        if _state_expired(state=state, now=_ensure_utc(self._now_func())):
            return _NEUTRAL_VALUE
        return state.boost_by_symbol.get(_normalize_symbol(symbol), _NEUTRAL_VALUE)

    def available(self, symbol: str = "") -> bool:
        """boost 表是否有该 symbol 的有效分量（无则 components 不加键）。

        过期检查与 score() 一致：过期 state 视为不可用（fail-closed）。
        """
        state = self._load_state()
        if state is None or state.mode != "boost" or state.dry_run:
            return False
        if _state_expired(state=state, now=_ensure_utc(self._now_func())):
            return False
        return _normalize_symbol(symbol) in state.boost_by_symbol

    def load_state(self) -> ThemeBoostState | None:
        """暴露当前 state（测试与报告用）。"""
        return self._load_state()

    def _load_state(self) -> ThemeBoostState | None:
        try:
            stat = self._state_path.stat()
        except OSError:
            self._cached_mtime = None
            self._cached_state = None
            return None
        mtime = float(stat.st_mtime)
        if self._cached_state is not None and self._cached_mtime == mtime:
            return self._cached_state
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._cached_mtime = None
            self._cached_state = None
            return None
        if not isinstance(payload, dict):
            self._cached_mtime = None
            self._cached_state = None
            return None
        state = ThemeBoostState.from_payload(payload)
        self._cached_mtime = mtime
        self._cached_state = state
        return state

    def build_boost_map(self) -> dict[str, float]:
        """当前 boost 表快照（报告用；shadow 下为空 dict）。"""
        state = self._load_state()
        if state is None or state.mode != "boost" or state.dry_run:
            return {}
        return dict(state.boost_by_symbol)


class NeutralThemeBoostProvider:
    """回测/中性化 provider：恒 0 分量（照 NeutralNewsSignalProvider 先例）。"""

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = symbol, bars, features, strategy
        return _NEUTRAL_VALUE

    def available(self, symbol: str = "") -> bool:
        _ = symbol
        return False


def build_theme_pool(
    *,
    active_theme_symbols: dict[str, list[str]],
    pinned_max: int,
) -> list[str]:
    """从激活主题成分股构造候选池注入清单（boost 模式才实际注入）。

    多主题合并去重保序，按主题列表顺序截断到 ``pinned_max``。shadow 模式
    下调用方只把它写进 dry-run 清单，不传给 pinned_symbols。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for theme_id, symbols in active_theme_symbols.items():
        _ = theme_id
        for raw in symbols:
            normalized = _normalize_symbol(str(raw))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
            if len(merged) >= max(0, int(pinned_max)):
                return merged
    return merged


def _resolve_state_path(path: str | Path) -> Path:
    target = Path(str(path))
    if target.is_absolute():
        return target
    return _project_root() / target


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _normalize_symbol(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    primary = text.split(".", maxsplit=1)[0]
    digits = "".join(ch for ch in primary if ch.isdigit())
    if len(digits) == 6:
        return digits
    return primary.upper()


def _state_expired(*, state: ThemeBoostState, now: datetime) -> bool:
    generated = _parse_iso(state.generated_at)
    if generated is None:
        return True
    age_hours = (now - generated).total_seconds() / 3600.0
    return age_hours > _STATE_MAX_AGE_HOURS or age_hours < -24.0


def _parse_iso(value: str) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return _ensure_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
