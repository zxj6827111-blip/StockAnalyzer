"""Alpha V2 M3：捕获侧的 Data Health 接线（R3 / N-R2-1）。

**为什么需要它**：治理层（``validation_kpis._day_governance``）早就规定
"``data_health != ok`` 的日不进 clean OOS"，但捕获侧从来没有稳定拿到生产
data_health——结果是 Shadow 跑得动、clean 日却永远为 0，20/60/120/250
样本门永不推进（上一轮复核登记为 N-R2-1）。

**不新造口径**：工件载荷 = S08（``stock_analyzer.ops.data_health``）的
``DataHealthReport.to_payload()``（``status`` / ``as_of`` / ``coverage_ratio`` /
``checks`` / ``missing_artifacts``）。本模块只做三件事：

1. 读磁盘上**当天**的工件（路径候选 + CLI 显式指定）；
2. 按"日期对齐 + 状态映射"判定这一天能不能算 clean（写成 :func:`capture_data_health_block`）；
3. 把判定收敛成 :func:`data_health_gate_ok` **唯一一份实现**，捕获侧与 KPI 侧共用
   ——避免"两套口径"再次漂移。

判定规则（fail-closed）：

```text
工件缺失/不可解析          -> status=not_available   （Shadow 仍可写，但 clean=false）
as_of != signal_date       -> status=stale           （昨天的健康不能当今天的 ok）
status 不在 S08 词表内      -> status=invalid
status=healthy/ok          -> status=ok
status=degraded/broken     -> 原样降级（clean=false）
```
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic

CAPTURE_DATA_HEALTH_SCHEMA = "alpha_v2_capture_data_health.v1"
DATA_HEALTH_ARTIFACT_SCHEMA = "alpha_v2_data_health.v1"

# 工件默认落点（与 S08 的运行时产物同目录，便于运维一次挂载）。
DEFAULT_ARTIFACT_RELPATH = Path("runtime") / "data_health.json"
ENV_ARTIFACT_PATH = "ALPHA_V2_DATA_HEALTH_PATH"

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_BROKEN = "broken"
STATUS_STALE = "stale"
STATUS_INVALID = "invalid"
STATUS_NOT_AVAILABLE = "not_available"

# S08 词表 → 捕获侧词表（healthy 是 S08 的"全绿"，等价于可进 clean 的 ok）。
# 非 healthy 的状态原样降级保留，让治理层的 by_date 原因更可读（都进不了 clean）。
_S08_STATUS_MAP: dict[str, str] = {
    "healthy": STATUS_OK,
    "ok": STATUS_OK,
    "degraded": STATUS_DEGRADED,
    "broken": STATUS_BROKEN,
    "stale": STATUS_STALE,
    "not_available": STATUS_NOT_AVAILABLE,
    "missing": STATUS_NOT_AVAILABLE,
    "invalid": STATUS_INVALID,
}

REASON_NOT_AVAILABLE = "data_health_not_available"
REASON_STALE = "data_health_stale"


def _to_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return "not_available"
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def artifact_candidate_paths(root: str | Path | None = None) -> list[Path]:
    """data_health 工件的候选路径（顺序即优先级）。"""
    candidates: list[Path] = []
    configured = os.getenv(ENV_ARTIFACT_PATH, "").strip()
    if configured:
        candidates.append(Path(configured))
    if root:
        candidates.append(Path(root) / DEFAULT_ARTIFACT_RELPATH)
        candidates.append(Path(root) / "data_health.json")
    candidates.append(Path("/app/artifacts") / DEFAULT_ARTIFACT_RELPATH)
    candidates.append(Path.cwd() / "artifacts" / DEFAULT_ARTIFACT_RELPATH)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_data_health_artifact(
    *, path: str | Path | None = None, root: str | Path | None = None
) -> tuple[dict[str, object], str]:
    """读工件；返回 ``(payload, 来源标识)``。读不到就是 ``({}, "missing")``，不猜。"""
    candidates = [Path(path)] if path else artifact_candidate_paths(root)
    for candidate in candidates:
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}, f"{candidate}:(JSONDecodeError)"
        if isinstance(payload, dict):
            return payload, str(candidate)
        return {}, f"{candidate}:(not-an-object)"
    return {}, "missing"


def capture_data_health_block(
    *,
    signal_date: date,
    payload: Mapping[str, object] | None = None,
    source: str = "",
    detail: str = "",
) -> dict[str, object]:
    """把当天工件折成"可直接进快照行"的 data_health 块（含有效状态与对齐结论）。"""
    if not payload:
        return {
            "schema": CAPTURE_DATA_HEALTH_SCHEMA,
            "status": STATUS_NOT_AVAILABLE,
            "source_status": "missing",
            "as_of": "not_available",
            "generated_at": "not_available",
            "coverage": "not_available",
            "source": source or "missing",
            "aligned_to_signal_date": False,
            "detail": detail or "data_health 工件缺失（clean OOS 资格 fail-closed）",
        }
    raw_status = str(payload.get("status", "") or "").strip().lower()
    as_of_text = str(payload.get("as_of", "") or "").strip()
    as_of = _to_date(as_of_text)
    aligned = bool(as_of is not None and as_of == signal_date)
    mapped = _S08_STATUS_MAP.get(raw_status)
    if mapped is None:
        effective = STATUS_INVALID
        why = f"未知 data_health.status={raw_status or '(空)'}（不在 S08 词表内）"
    elif not aligned:
        effective = STATUS_STALE
        why = (
            f"data_health.as_of={as_of_text or '(缺失)'} != signal_date={signal_date}"
            "（昨天的健康不能当今天的 ok）"
        )
    else:
        effective = mapped
        why = detail or f"同日工件可用（source_status={raw_status}）"
    coverage = payload.get("coverage_ratio", payload.get("coverage", "not_available"))
    generated_at = payload.get("generated_at", payload.get("created_at", ""))
    return {
        "schema": CAPTURE_DATA_HEALTH_SCHEMA,
        "status": effective,
        "source_status": raw_status or "missing",
        "as_of": as_of_text or "not_available",
        "generated_at": str(generated_at or "not_available"),
        "coverage": _json_safe(coverage),
        "source": source or "unknown",
        "aligned_to_signal_date": aligned,
        "detail": why,
    }


def data_health_gate_ok(value: object, signal_date: date) -> tuple[bool, str]:
    """**唯一一份** data_health → clean OOS 判定（捕获侧与 KPI 侧共用）。

    返回 ``(ok, reason)``；``reason`` 为空串表示通过。
    """
    if value is None:
        return False, REASON_NOT_AVAILABLE
    if isinstance(value, Mapping):
        status = str(value.get("status", "") or "").strip().lower()
        as_of = value.get("as_of")
        aligned_flag = value.get("aligned_to_signal_date")
    else:
        status = str(value or "").strip().lower()
        if status == "none":
            status = ""
        as_of = None
        aligned_flag = None
    if status == STATUS_STALE:
        return False, REASON_STALE
    if status != STATUS_OK:
        return False, REASON_NOT_AVAILABLE
    if aligned_flag is False:
        return False, REASON_STALE
    if as_of not in (None, "", "not_available"):
        parsed = _to_date(as_of)
        if parsed is None or parsed != signal_date:
            return False, REASON_STALE
    return True, ""


def build_data_health_artifact(
    *, as_of: date, generated_at: str | None = None, **s08_inputs: object
) -> dict[str, object]:
    """按 S08 契约算一份 data_health 工件载荷（复用 ``evaluate_data_health``）。

    ``s08_inputs`` 原样透传（latest_trade_date / universe_snapshot /
    valid_symbol_count / board_coverage / feature_snapshot / model_identity /
    price_contract / breadth_artifact_present ...）——拿不到的输入就不传，
    S08 会把对应检查项标成 degraded（"缺失不得当健康"），本模块不替它兜底。
    """
    from stock_analyzer.ops.data_health import evaluate_data_health

    report = evaluate_data_health(as_of=as_of, **s08_inputs)  # type: ignore[arg-type]
    payload = report.to_payload()
    payload["schema"] = DATA_HEALTH_ARTIFACT_SCHEMA
    payload["generated_at"] = str(
        generated_at or datetime.now().astimezone().isoformat()
    )
    return payload


def write_data_health_artifact(payload: Mapping[str, object], path: str | Path) -> Path:
    """原子落盘（调用方负责决定落点；容器里应指 ``/app/artifacts/runtime/``）。"""
    target = Path(path)
    body = dict(payload)
    body.setdefault("schema", DATA_HEALTH_ARTIFACT_SCHEMA)
    body.setdefault("generated_at", datetime.now().astimezone().isoformat())
    body.setdefault("as_of", "not_available")
    body.setdefault("status", STATUS_NOT_AVAILABLE)
    return write_json_atomic(target, _json_safe(body))  # type: ignore[arg-type]


__all__ = [
    "CAPTURE_DATA_HEALTH_SCHEMA",
    "DATA_HEALTH_ARTIFACT_SCHEMA",
    "DEFAULT_ARTIFACT_RELPATH",
    "ENV_ARTIFACT_PATH",
    "REASON_NOT_AVAILABLE",
    "REASON_STALE",
    "STATUS_BROKEN",
    "STATUS_DEGRADED",
    "STATUS_INVALID",
    "STATUS_NOT_AVAILABLE",
    "STATUS_OK",
    "STATUS_STALE",
    "artifact_candidate_paths",
    "build_data_health_artifact",
    "capture_data_health_block",
    "data_health_gate_ok",
    "load_data_health_artifact",
    "write_data_health_artifact",
]
