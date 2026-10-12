"""留档时间的**带版本解释规则**：旧记录原样保留，但新口径不自动适用（改进计划 §3.1）。

§3.1 要求"修复新记录的时区、交易日、去重和标签成熟时间；旧记录保留原始值，通过带版本
的解释规则兼容"。写侧已经修好（``funnel_trace.write_trace`` 落带时区的 ``written_at``
并声明 ``written_at_timezone``）。这个模块补的是读侧最容易糊过去的一步：

同一目录里会同时躺着**修复前**和**修复后**的留档。裸时间戳（``2026-10-09T14:45:00``）
没有偏移信息，把它当成 Asia/Shanghai 是一个**猜测** —— 而留档是影子验证唯一的证据
来源，猜错就意味着跨库对不上账。所以这里的规则是：

- 解释结果只**附加**在读取出来的字典上，绝不改写被存储的原始值；
- 裸时间戳 → ``record_time_v1``，不给 instant（不发明时区）、不当成可用证据；
- 带偏移 → ``record_time_v2``，可用；但若声明的时区名与该时刻的真实偏移矛盾，
  判为 ``timezone_declaration_mismatch`` 并撤销证据资格（矛盾比缺失更危险）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_INTERPRETATION_V2 = "record_time_v2"
TIME_INTERPRETATION_V1 = "record_time_v1"
#: 旧留档：时间戳没有偏移，时区不可证。
LEGACY_TIME_CAVEAT = "legacy_time_basis_unverified"


@dataclass(frozen=True, slots=True)
class RecordTimeSemantics:
    raw_written_at: str
    declared_timezone: str
    interpretation_version: str
    instant_iso: str
    evidence_eligible: bool
    caveats: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_written_at": self.raw_written_at,
            "declared_timezone": self.declared_timezone,
            "interpretation_version": self.interpretation_version,
            "instant": self.instant_iso,
            "evidence_eligible": self.evidence_eligible,
            "caveats": list(self.caveats),
        }


def _parse(raw: str) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def interpret_record_time(
    payload: Mapping[str, Any],
    *,
    expected_timezone: str = "",
) -> RecordTimeSemantics:
    """按记录自身的证据决定它是 v2 还是 v1；不猜测、不改写原值。"""
    raw = str(payload.get("written_at", "") or "").strip()
    declared = str(payload.get("written_at_timezone", "") or "").strip()
    caveats: list[str] = []

    if not raw:
        return RecordTimeSemantics(
            raw_written_at=raw, declared_timezone=declared,
            interpretation_version=TIME_INTERPRETATION_V1, instant_iso="",
            evidence_eligible=False, caveats=("written_at_absent",),
        )
    parsed = _parse(raw)
    if parsed is None:
        # 裸时间戳或读不出来：保留原值，但不给它发明时区。
        try:
            naive = datetime.fromisoformat(raw)
        except ValueError:
            naive = None
        if naive is not None and naive.tzinfo is None:
            caveats.append(LEGACY_TIME_CAVEAT)
        else:
            caveats.append("written_at_unparseable")
        return RecordTimeSemantics(
            raw_written_at=raw, declared_timezone=declared,
            interpretation_version=TIME_INTERPRETATION_V1, instant_iso="",
            evidence_eligible=False, caveats=tuple(caveats),
        )

    if not declared:
        caveats.append("timezone_name_undeclared")
    else:
        try:
            declared_offset = ZoneInfo(declared).utcoffset(parsed)
        except (ZoneInfoNotFoundError, ValueError):
            declared_offset = None
            caveats.append(f"timezone_name_unknown:{declared}")
        if declared_offset is not None and parsed.utcoffset() != declared_offset:
            # 声明与实际偏移矛盾：这条留档的时间证据不可信，不是"差不多就行"。
            caveats.append("timezone_declaration_mismatch")
    if expected_timezone and declared and declared != expected_timezone:
        caveats.append(f"timezone_not_expected:{declared}!={expected_timezone}")

    eligible = not any(item.startswith("timezone_declaration_mismatch")
                       or item.startswith("timezone_name_unknown")
                       for item in caveats)
    return RecordTimeSemantics(
        raw_written_at=raw, declared_timezone=declared,
        interpretation_version=TIME_INTERPRETATION_V2,
        instant_iso=parsed.isoformat(timespec="seconds"),
        evidence_eligible=eligible, caveats=tuple(caveats),
    )


def annotate_time_semantics(
    payload: Mapping[str, Any],
    *,
    expected_timezone: str = "",
) -> dict[str, Any]:
    """把解释结果作为**新字段**挂上去；被解释的原始字段一字不改。"""
    annotated = dict(payload)
    annotated["time_interpretation"] = interpret_record_time(
        payload, expected_timezone=expected_timezone
    ).as_dict()
    return annotated


__all__ = [
    "LEGACY_TIME_CAVEAT",
    "RecordTimeSemantics",
    "TIME_INTERPRETATION_V1",
    "TIME_INTERPRETATION_V2",
    "annotate_time_semantics",
    "interpret_record_time",
]
