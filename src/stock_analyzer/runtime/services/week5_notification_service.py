"""Week5 notification rendering helpers extracted from the runtime service."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeWeek5NotificationService:
    """Encapsulate week5 scan notification and formatting logic."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def build_scan_notification_content(
        self,
        *,
        symbol_list: list[str],
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
        watchlist_sync: dict[str, object],
        runtime_mode: str,
    ) -> str:
        impact_line = (
            f"影响=观察池数量={len(symbol_list)}；"
            f"首板候选={len(first_board_candidates)}；"
            f"龙头候选={len(leaders)}；"
            f"异常项={len(anomalies)}；"
            f"空信号触发={_bool_zh(bool(empty_signal.get('triggered', False)))}；"
            f"已同步观察池={_bool_zh(bool(watchlist_sync.get('updated', False)))}"
        )
        first_board_line = "首板候选=" + _format_week5_symbol_rows(
            first_board_candidates,
            row_type="candidate",
        )
        leader_line = "龙头候选=" + _format_week5_symbol_rows(leaders, row_type="leader")
        anomaly_line = "异常标的=" + _format_week5_anomaly_rows(anomalies)
        runtime_line = f"数据链路={runtime_mode}"
        explain_line = (
            "说明=首板候选指当天首次涨停的观察标的；龙头候选指首板里相对更强、优先盯盘的标的"
        )
        conclusion_line = "结论=" + self.week5_scan_conclusion_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )
        action_line = "建议动作=" + self.week5_scan_action_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )
        return "\n".join(
            [
                "事件=盘中扫描更新",
                impact_line,
                first_board_line,
                leader_line,
                anomaly_line,
                runtime_line,
                explain_line,
                conclusion_line,
                action_line,
            ]
        )

    def week5_scan_action_hint(
        self,
        *,
        leaders: list[dict[str, object]],
        first_board_candidates: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        if leaders:
            leader_symbols = ",".join(
                str(item.get("symbol", "")).strip()
                for item in leaders[:3]
                if str(item.get("symbol", "")).strip()
            )
            if bool(empty_signal.get("triggered", False)):
                return f"优先复核龙头 {leader_symbols}，但当前空信号已触发，先观察后处理，避免追高"
            return f"优先复核龙头 {leader_symbols}，结合盘口强弱决定是否升级为重点跟踪"
        if first_board_candidates:
            candidate_symbols = ",".join(
                str(item.get("symbol", "")).strip()
                for item in first_board_candidates[:3]
                if str(item.get("symbol", "")).strip()
            )
            return f"先盯首板候选 {candidate_symbols}，确认封板质量和量能后再决定是否升级"
        if anomalies:
            return "当前以风险排查为主，异常标的不直接作为开仓依据，请先人工复核"
        if bool(empty_signal.get("triggered", False)):
            return "当前开仓条件偏弱，继续等待下一轮扫描变化，暂不新增仓位"
        return "暂无重点候选，继续观察下一轮扫描结果"

    def week5_scan_conclusion_hint(
        self,
        *,
        leaders: list[dict[str, object]],
        first_board_candidates: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        rows = leaders + first_board_candidates
        if bool(empty_signal.get("triggered", False)):
            return (
                "当前空信号已触发，本轮仅观察，不建议新开仓；"
                "只有后续单独出现“买入候选”推送才考虑动作"
            )
        buy_symbols = self.week5_symbols_by_action(rows=rows, action="buy")
        if buy_symbols:
            return (
                f"当前存在可执行候选 {','.join(buy_symbols[:3])}；"
                "但仍以单独“买入候选”推送作为最终执行口径"
            )
        watch_symbols = self.week5_symbols_by_action(rows=rows, action="watch")
        if watch_symbols:
            return f"当前以观察为主，重点盯盘 {','.join(watch_symbols[:3])}，还不是直接买点"
        if anomalies:
            return "当前以异常排查为主，不把这条扫描结果当作开仓依据"
        return "本条是扫描摘要，不是买入指令；当前没有明确可执行买点"

    def week5_symbols_by_action(
        self,
        *,
        rows: list[dict[str, object]],
        action: str,
    ) -> list[str]:
        normalized_action = action.strip().lower()
        symbols: list[str] = []
        seen: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            if not symbol or symbol in seen:
                continue
            item_action = str(item.get("action", "")).strip().lower()
            suggested_position = _as_float(item.get("suggested_position"), default=0.0)
            if item_action != normalized_action:
                continue
            if normalized_action == "buy" and suggested_position <= 0:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def week5_scan_notification_signature(
        self,
        *,
        first_board_candidates: list[dict[str, object]],
        leaders: list[dict[str, object]],
        anomalies: list[dict[str, object]],
        empty_signal: dict[str, object],
    ) -> str:
        conclusion = self.week5_scan_conclusion_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )
        action = self.week5_scan_action_hint(
            leaders=leaders,
            first_board_candidates=first_board_candidates,
            anomalies=anomalies,
            empty_signal=empty_signal,
        )
        payload = {
            "first_board_candidates": sorted(
                [
                    {
                        "symbol": str(item.get("symbol", "")).strip(),
                        "action": str(item.get("action", "")).strip().lower(),
                        "isolated": bool(item.get("isolated", False)),
                        "isolation_reason": str(item.get("isolation_reason", "")).strip(),
                    }
                    for item in first_board_candidates
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ],
                key=lambda item: (
                    str(item.get("symbol", "")),
                    str(item.get("action", "")),
                    str(item.get("isolation_reason", "")),
                ),
            ),
            "leaders": sorted(
                [
                    {
                        "symbol": str(item.get("symbol", "")).strip(),
                        "action": str(item.get("action", "")).strip().lower(),
                        "isolated": bool(item.get("isolated", False)),
                        "isolation_reason": str(item.get("isolation_reason", "")).strip(),
                    }
                    for item in leaders
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ],
                key=lambda item: (
                    str(item.get("symbol", "")),
                    str(item.get("action", "")),
                    str(item.get("isolation_reason", "")),
                ),
            ),
            "anomalies": sorted(
                [
                    {
                        "symbol": str(item.get("symbol", "")).strip(),
                        "types": sorted(_coerce_text_list(item.get("types"))),
                    }
                    for item in anomalies
                    if isinstance(item, dict) and str(item.get("symbol", "")).strip()
                ],
                key=lambda item: (
                    str(item.get("symbol", "")),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                ),
            ),
            "empty_signal": bool(empty_signal.get("triggered", False)),
            "conclusion": conclusion,
            "action": action,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def format_week5_symbol_rows(
    rows: list[dict[str, object]],
    row_type: str,
    limit: int = 3,
) -> str:
    return _format_week5_symbol_rows(rows, row_type=row_type, limit=limit)


def format_week5_anomaly_rows(rows: list[dict[str, object]], limit: int = 3) -> str:
    return _format_week5_anomaly_rows(rows, limit=limit)


def translate_week5_anomaly_type_zh(value: str) -> str:
    return _translate_week5_anomaly_type_zh(value)


def _format_week5_symbol_rows(
    rows: list[dict[str, object]],
    *,
    row_type: str,
    limit: int = 3,
) -> str:
    if not rows:
        return "无"
    rendered: list[str] = []
    for item in rows[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        action_text = _week5_candidate_action_zh(
            action=str(item.get("action", "")).strip(),
            suggested_position=_as_float(item.get("suggested_position"), default=0.0),
            isolated=bool(item.get("isolated", False)),
        )
        suggested_position_pct = _as_float(item.get("suggested_position"), default=0.0) * 100.0
        if row_type == "leader":
            rendered.append(
                f"{symbol}（龙头分={_as_float(item.get('leader_score'), default=0.0):.1f}，"
                f"结论={action_text}，参考仓位={suggested_position_pct:.0f}%）"
            )
            continue
        board_stage = _board_stage_zh(str(item.get("board_stage", "")).strip())
        score = _as_float(item.get("score"), default=0.0)
        rendered.append(
            f"{symbol}（{board_stage}，评分={score:.1f}，"
            f"结论={action_text}，参考仓位={suggested_position_pct:.0f}%）"
        )
    return "；".join(rendered) if rendered else "无"


def _format_week5_anomaly_rows(rows: list[dict[str, object]], limit: int = 3) -> str:
    if not rows:
        return "无"
    rendered: list[str] = []
    for item in rows[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        raw_types = item.get("types", [])
        raw_type_values = _coerce_text_list(raw_types)
        types = (
            [_translate_week5_anomaly_type_zh(str(value).strip()) for value in raw_type_values]
            if raw_type_values
            else []
        )
        if "insufficient_history" in raw_type_values:
            history_days = _as_int(item.get("history_days"), default=0)
            required_days = _as_int(item.get("required_history_days"), default=0)
            rendered.append(f"{symbol}（历史不足：{history_days}<{required_days}）")
            continue
        rendered.append(f"{symbol}（{'/'.join(types) if types else '异常'}）")
    return "；".join(rendered) if rendered else "无"


def _translate_week5_anomaly_type_zh(value: str) -> str:
    mapping = {
        "gap": "跳空",
        "volume_spike": "放量",
        "upper_shadow": "上影偏长",
        "lower_shadow": "下影偏长",
        "insufficient_history": "历史不足",
    }
    normalized = value.strip().lower()
    return mapping.get(normalized, value or "异常")


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _bool_zh(value: bool) -> str:
    return cast(str, _runtime_service_module()._bool_zh(value))


def _coerce_text_list(value: object) -> list[str]:
    return cast(list[str], _runtime_service_module()._coerce_text_list(value))


def _week5_candidate_action_zh(
    *,
    action: str,
    suggested_position: float,
    isolated: bool,
) -> str:
    return cast(
        str,
        _runtime_service_module()._week5_candidate_action_zh(
            action=action,
            suggested_position=suggested_position,
            isolated=isolated,
        ),
    )


def _board_stage_zh(value: str) -> str:
    return cast(str, _runtime_service_module()._board_stage_zh(value))
