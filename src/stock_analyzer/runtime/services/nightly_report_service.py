"""晚间正式报告：唯一事实来源、冻结存储、四类结果与统一消息口径。

设计要点（对应 2026-09-16 v2 方案 §2）：

- **唯一来源**：正式晚报只能由一次明确完成的夜扫结果构造，落盘后冻结。发送/补发
  一律读冻结报告，绝不回头去读可能已被进化复扫、盘中雷达改写的通用 ``latest`` /
  ``watchlist`` / 共享候选状态——那正是当晚"进化复扫 20 只 / 观察池 1 只 / 信号 0 个"
  三个口径混在一起、无法验收的成因。
- **四类结果**：``completed`` / ``empty`` / ``blocked`` / ``failed``。数据没准备好导致
  没选出来，不能被渲染成"今天没有机会"。
- **版本**：同一交易日内容确有修正才产生新版本（标题标"修订版"）；普通重启、补发
  只要内容一致就复用同一 ``report_id``，不刷屏。
- **正文与报告分离**：正文按固定顺序在 ~3000 字符内截断，完整内容留在报告文件里。

存储布局（沿用 artifacts 命名卷）::

    runtime/nightly_reports/<trade_date>/<report_id>.json   不可变业务结果
    runtime/nightly_reports/<trade_date>/state.json         日期状态（阶段/指针/尝试次数）
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from stock_analyzer.config import NightlyReportConfig
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.risk.overextension import (
    EVALUATION_EVALUATED,
    EVALUATION_INSUFFICIENT_INPUT,
)

SCHEMA_VERSION = 1

SCAN_STATUS_COMPLETED = "completed"
SCAN_STATUS_EMPTY = "empty"
SCAN_STATUS_BLOCKED = "blocked"
SCAN_STATUS_FAILED = "failed"
SCAN_STATUSES: tuple[str, ...] = (
    SCAN_STATUS_COMPLETED,
    SCAN_STATUS_EMPTY,
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_FAILED,
)

REPORT_KIND_FORMAL = "formal"
REPORT_KIND_NOTICE = "notice"
# 验收回放：用历史夜扫产物生成、明确标注"非当日结果"的报告。它只用于交付链路
# 灰度（方案 §5.4 第 1、2 步），不占用正式报告的指针，也不可能被误当成当天结论。
REPORT_KIND_REPLAY = "replay"

# 延迟说明 / 截止未完成说明：两条固定行为各最多发一次，去重键与正式结果分开。
NOTICE_DELAY = "delay"
NOTICE_DEADLINE = "deadline"

# 夜扫阶段（写进日期状态，供交付检查判定"今晚到底走到哪一步"）
PHASE_PENDING = "pending"
PHASE_WAITING_DATA = "waiting_data"
PHASE_SCANNING = "scanning"
PHASE_PUBLISHED = "published"
PHASE_FAILED = "failed"

# 风险评估状态复用 risk.overextension 的唯一定义：只有 evaluated 才算"通过完整
# 风险检查"。报告层额外需要一个 unknown——旧产物根本没有这个字段，既不能当成
# evaluated（等于默认已评估），也不该冒充 insufficient（那是明确的缺输入）。
EVALUATION_INCOMPLETE = EVALUATION_INSUFFICIENT_INPUT
EVALUATION_UNKNOWN = "unknown"

# 理由/风险的中文口径。未知代号原样保留——宁可露出代号，也不编一个更好听的解释。
_SHORTLIST_REASON_LABELS: dict[str, str] = {
    "signal_strength": "信号强度高",
    "capital_confirmation": "资金面确认",
    "trend_alignment": "趋势一致",
    "price_volume_support": "量价配合",
    "execution_ready": "流动性可执行",
    "risk_capped": "风险项已扣分",
}

_OVEREXTENSION_REASON_LABELS: dict[str, str] = {
    "bias_or_atr_distance_warn": "偏离 MA5 进入警告档",
    "bias_or_atr_distance_reject": "偏离 MA5 超过警戒线",
    "ret5_high": "5 日涨幅偏大",
    "large_gap": "当日跳空偏大",
    "volume_divergence": "量价背离",
    "insufficient_input": "风险指标输入不足",
}

# final selector 的拒因代号（`final_selection.rejected[].reject_reasons`）。
# 空结果时正文要展示的"主要过滤原因"就是这些计数，直接用代号对用户没有信息量。
_REJECT_REASON_LABELS: dict[str, str] = {
    "below_min_threshold": "低于最低分门槛",
    "cross_review_failed": "交叉复核未通过",
    "risk_gate_failed": "风险门未通过",
    "board_risk_reject_new_buy": "连板风险拒绝新建仓",
    "overextension_reject_new_buy": "过热拒绝新建仓",
    "overextension_insufficient_input": "风控输入不足",
    "risk_gate_reject_new_buy": "风险门拒绝新建仓",
    "predictor_rejected": "模型否决",
    "news_risk_veto": "新闻风险否决",
}

_BLOCKING_REASON_LABELS: dict[str, str] = {
    "intraday_freshness_missing": "分钟数据新鲜度证据缺失",
    "intraday_freshness_below_80pct": "分钟数据新鲜率低于 80%",
    "eligible_universe_coverage_below_80pct": "合格股票池覆盖率低于 80%",
    "overnight_advisory_only": "夜间仅生成隔夜观察池",
    "realtime_gate_not_run": "未执行盘中实时确认",
}

_MAX_CANDIDATE_REASONS = 3
_MAX_CANDIDATE_RISKS = 2
_MAX_EXCLUSION_REASONS = 6


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    title: str
    content: str
    truncated: bool


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        {str(key): item for key, item in item.items()}
        for item in value
        if isinstance(item, Mapping)
    ]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _format_score(value: object) -> str:
    score = _as_float(value, default=float("nan"))
    if score != score:  # NaN
        return "—"
    return f"{score:.2f}"


def _format_trade_date(value: object) -> str:
    raw = _text(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


class NightlyReportService:
    """构造、冻结并渲染正式晚报。不负责发送（见 NightlyDeliveryService）。"""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.config: NightlyReportConfig = getattr(service._config, "nightly", None) or (
            NightlyReportConfig()
        )
        self.root = self._resolve_path(self.config.reports_root)

    # ------------------------------------------------------------------ 路径

    def _resolve_path(self, raw: str) -> Path:
        resolver = getattr(self._service, "_resolve_evolution_path", None)
        if callable(resolver):
            try:
                return Path(resolver(str(raw)))
            except Exception:  # noqa: BLE001 - 路径解析失败退回相对路径
                pass
        return Path(str(raw))

    def date_dir(self, trade_date: str) -> Path:
        return self.root / _text(trade_date)

    def date_state_path(self, trade_date: str) -> Path:
        return self.date_dir(trade_date) / "state.json"

    def report_path(self, trade_date: str, report_id: str) -> Path:
        return self.date_dir(trade_date) / f"{_text(report_id)}.json"

    # -------------------------------------------------------------- 状态读写

    def read_date_state(self, trade_date: str) -> dict[str, object]:
        path = self.date_state_path(trade_date)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def update_date_state(
        self,
        trade_date: str,
        patch: Mapping[str, object],
        *,
        updated_at: datetime | None = None,
    ) -> dict[str, object]:
        """加锁 + 原子替换地合并日期状态（读-改-写全程持锁）。"""
        path = self.date_state_path(trade_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = DistributedFileLock(path.with_suffix(".lock"), stale_after_sec=60)
        lock.acquire()
        try:
            state = self.read_date_state(trade_date)
            state.update({str(key): value for key, value in patch.items()})
            state["schema_version"] = SCHEMA_VERSION
            state["trade_date"] = _text(trade_date)
            state["updated_at"] = (updated_at or datetime.now()).isoformat()
            _write_json_atomic(path, state)
            return state
        finally:
            lock.release()

    # ------------------------------------------------------------ 报告读写

    def freeze_report(self, report: Mapping[str, object]) -> tuple[Path, bool]:
        """原子写入报告文件，返回 ``(路径, 是否写入)``。

        报告是**不可变**的：同 ``report_id`` 已存在时不覆盖（覆盖会让已经发出去的
        正文与磁盘内容不一致，回执就再也对不上账）。

        返回"是否写入"而不是静默返回路径：调用方必须能区分"写成功了"和"因为
        已存在而没写"——后者配上"要写的内容不同"就是指针指向不存在的版本。
        """
        trade_date = _text(report.get("trade_date"))
        report_id = _text(report.get("report_id"))
        if not trade_date or not report_id:
            raise ValueError("report requires trade_date and report_id")
        path = self.report_path(trade_date, report_id)
        existing = self.load_report(report_id, trade_date=trade_date)
        if existing is not None:
            return path, False
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, dict(report))
        return path, True

    def load_report(self, report_id: str, *, trade_date: str = "") -> dict[str, object] | None:
        normalized_id = _text(report_id)
        if not normalized_id:
            return None
        if _text(trade_date):
            candidates = [self.report_path(trade_date, normalized_id)]
        else:
            # 未指定交易日时只在最近两个交易日目录里找，不做全目录扫描。
            candidates = [
                self.report_path(day, normalized_id) for day in self._recent_trade_date_dirs()
            ]
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def recent_trade_dates(self) -> list[str]:
        """最近 N 个交易日目录名（按目录名倒序，N=recovery_lookback_days）。

        恢复只回看当前交易日与最近一个交易日：禁止扫描整个历史目录，否则目录一旦
        累积，交付检查的每分钟一次读取会越来越慢。
        """
        try:
            names = sorted(
                (item.name for item in self.root.iterdir() if item.is_dir()),
                reverse=True,
            )
        except OSError:
            return []
        return names[: max(1, int(self.config.recovery_lookback_days))]

    def _recent_trade_date_dirs(self) -> list[str]:
        return self.recent_trade_dates()

    def reports_for(self, trade_date: str) -> list[dict[str, object]]:
        """该交易日需要交付的报告（正式报告 + 已发布的说明）。"""
        state = self.read_date_state(trade_date)
        report_ids: list[str] = []
        published_id = _text(state.get("published_report_id"))
        if published_id:
            report_ids.append(published_id)
        notices = _mapping(state.get("notices"))
        for report_id in notices.values():
            normalized = _text(report_id)
            if normalized and normalized not in report_ids:
                report_ids.append(normalized)
        reports: list[dict[str, object]] = []
        for report_id in report_ids:
            report = self.load_report(report_id, trade_date=trade_date)
            if report is not None:
                reports.append(report)
        return reports

    # ------------------------------------------------------------ 报告构造

    def build_formal_report(
        self,
        *,
        night_scan: Mapping[str, object],
        trade_date: str,
        generated_at: datetime,
        run_id: str = "",
        trace_id: str = "",
        data_snapshot_id: str = "",
        data_gate: Mapping[str, object] | None = None,
        readiness: Mapping[str, object] | None = None,
        fallback_source_date: str = "",
        scan_status: str = "",
        failure_stage: str = "",
        failure_reason: str = "",
        code_commit: str = "",
        config_hash: str = "",
        display_top_k: int | None = None,
        name_resolver: Callable[[str], str] | None = None,
    ) -> dict[str, object]:
        """由一次夜扫结果构造正式报告（未落盘、未发布）。"""
        limit = max(1, int(display_top_k or self.config.display_top_k))
        source_report = _mapping(night_scan.get("source_report"))
        rows = _mapping_list(night_scan.get("night_pool"))
        resolved_gate = dict(data_gate or {}) or _mapping(night_scan.get("candidate_data_gate"))
        fallback = _mapping(night_scan.get("fallback"))
        resolved_status = self._derive_scan_status(
            night_scan=night_scan,
            fallback=fallback,
            data_gate=resolved_gate,
            has_rows=bool(rows),
            override=scan_status,
        )
        observation, incomplete = self._split_candidates(
            rows,
            limit=limit,
            name_resolver=name_resolver,
        )
        funnel_counts = self._funnel_counts(
            source_report, rows=rows, observation=observation, incomplete=incomplete
        )
        exclusion_summary = self._exclusion_summary(
            source_report,
            data_gate=resolved_gate,
            fallback=fallback,
            status=resolved_status,
        )
        resolved_fallback_date = _text(fallback_source_date)
        if not resolved_fallback_date and bool(fallback.get("applied", False)):
            resolved_fallback_date = self._fallback_source_date(night_scan)

        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "report_kind": REPORT_KIND_FORMAL,
            "report_id": "",
            "revision": 0,
            "trade_date": _text(trade_date),
            "generated_at": generated_at.isoformat(),
            "run_id": _text(run_id),
            "trace_id": _text(trace_id) or _text(night_scan.get("trace_id")),
            "code_commit": _text(code_commit),
            "config_hash": _text(config_hash),
            "data_snapshot_id": _text(data_snapshot_id)
            or _text(source_report.get("data_snapshot_id"))
            or _text(source_report.get("data_version")),
            "scan_status": resolved_status,
            "data_gate": resolved_gate,
            "readiness": dict(readiness or {}) or _mapping(night_scan.get("readiness")),
            "funnel_counts": funnel_counts,
            "observation_candidates": observation,
            "incomplete_candidates": incomplete,
            "night_pool_count": len(rows),
            "final_selection_count": _optional_int(funnel_counts.get("final_count")) or 0,
            "exclusion_summary": exclusion_summary,
            "fallback_source_date": resolved_fallback_date,
            "failure_stage": _text(failure_stage),
            "failure_reason": _text(failure_reason),
            "display_top_k": limit,
            "actionable": False,
            "signal_mode": "overnight_advisory",
        }
        report["content_digest"] = self.content_digest(report)
        return report

    def build_notice(
        self,
        *,
        trade_date: str,
        generated_at: datetime,
        notice: str,
        scan_status: str,
        reason: str = "",
        date_state: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """延迟说明 / 截止未完成说明（kind=notice，不占用正式报告指针）。"""
        normalized_notice = NOTICE_DELAY if notice == NOTICE_DELAY else NOTICE_DEADLINE
        state = dict(date_state or {})
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "report_kind": REPORT_KIND_NOTICE,
            "notice": normalized_notice,
            "report_id": self.notice_report_id(trade_date, normalized_notice),
            "revision": 1,
            "trade_date": _text(trade_date),
            "generated_at": generated_at.isoformat(),
            "scan_status": scan_status if scan_status in SCAN_STATUSES else SCAN_STATUS_BLOCKED,
            "reason": _text(reason),
            "scan_phase": _text(state.get("scan_phase")),
            "scan_attempts": _optional_int(state.get("scan_attempts")) or 0,
            "waiting_reason": _text(state.get("waiting_reason")),
            "last_scan_status": _text(state.get("last_scan_status")),
            "display_top_k": max(1, int(self.config.display_top_k)),
            "observation_candidates": [],
            "incomplete_candidates": [],
            "funnel_counts": {},
            "exclusion_summary": {},
            "fallback_source_date": "",
            "actionable": False,
            "signal_mode": "overnight_advisory",
        }
        report["content_digest"] = self.content_digest(report)
        return report

    @staticmethod
    def notice_report_id(trade_date: str, notice: str) -> str:
        suffix = "delay" if notice == NOTICE_DELAY else "deadline"
        return f"nn-{_text(trade_date).replace('-', '')}-{suffix}"

    def build_replay_report(
        self,
        *,
        night_scan: Mapping[str, object],
        trade_date: str,
        generated_at: datetime,
        source_label: str = "",
        display_top_k: int | None = None,
        name_resolver: Callable[[str], str] | None = None,
    ) -> dict[str, object]:
        """由**历史**夜扫产物构造验收回放报告（离线回放 + 通道冒烟共用）。

        与正式报告共用同一套构造与渲染，只改三件事：`report_kind=replay`、
        `report_id` 前缀 `rp-`、正文首行加"非当日结果"的醒目标注。这样冒烟消息
        不可能被误读成当天结论，回放本身也走的是真实交付链路。
        """
        report = self.build_formal_report(
            night_scan=night_scan,
            trade_date=trade_date,
            generated_at=generated_at,
            run_id="replay",
            trace_id="nightly-replay",
            data_snapshot_id=str(night_scan.get("data_snapshot_id", "") or trade_date),
            display_top_k=display_top_k,
            name_resolver=name_resolver,
        )
        report["report_kind"] = REPORT_KIND_REPLAY
        report["replay_source"] = _text(source_label)
        # report_id 由 publish() 按实际版本号生成（rp-<日期>-<版本>）：回放会按需
        # 重建，版本号必须体现在文件名上，否则新内容写不进去（见 publish 注释）。
        report["report_id"] = ""
        report["content_digest"] = self.content_digest(report)
        return report

    def latest_report_id(self, trade_date: str) -> str:
        return _text(self.read_date_state(trade_date).get("published_report_id"))

    @staticmethod
    def content_digest(report: Mapping[str, object]) -> str:
        """业务内容摘要：**不含** generated_at / run_id / trace_id。

        这是"普通重启、补发不得产生新报告版本"的实现前提——重启必然换 run_id，
        若把它算进摘要，每次重启都会伪造出一个"修订版"。
        """
        business_keys = (
            "report_kind",
            "notice",
            "replay_source",
            "trade_date",
            "scan_status",
            "data_snapshot_id",
            "funnel_counts",
            "observation_candidates",
            "incomplete_candidates",
            "final_selection_count",
            "exclusion_summary",
            "fallback_source_date",
            "failure_stage",
            "reason",
            "actionable",
        )
        payload = {key: report.get(key) for key in business_keys}
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------ 发布（版本）

    def publish(self, report: Mapping[str, object]) -> dict[str, object]:
        """冻结报告 + 发布指针。返回 ``{"published": bool, "report_id":..., "report":...}``。

        顺序不可颠倒：先落报告文件，成功后才动指针。消费者只认指针，因此"指针指向
        一份已存在的报告"始终成立；反过来（先写指针后写报告）会出现悬空指针。
        """
        trade_date = _text(report.get("trade_date"))
        state = self.read_date_state(trade_date)
        kind = _text(report.get("report_kind"))
        # 三类报告各有各的指针：正式结果绝不会被过程说明或验收回放顶掉。
        if kind == REPORT_KIND_FORMAL:
            current_id = _text(state.get("published_report_id"))
            current = self.load_report(current_id, trade_date=trade_date) if current_id else None
        elif kind == REPORT_KIND_REPLAY:
            current_id = _text(_mapping(state.get("notices")).get("replay"))
            current = self.load_report(current_id, trade_date=trade_date) if current_id else None
        else:
            notices = _mapping(state.get("notices"))
            notice_key = "delay" if report.get("notice") == NOTICE_DELAY else "deadline"
            current_id = _text(notices.get(notice_key))
            current = self.load_report(current_id, trade_date=trade_date) if current_id else None

        if current is not None and _text(current.get("content_digest")) == _text(
            report.get("content_digest")
        ):
            return {
                "published": False,
                "reason": "unchanged",
                "report_id": _text(current.get("report_id")),
                "report": current,
            }

        revision = max(1, (_optional_int(current.get("revision")) or 0) + 1) if current else 1
        finalized = dict(report)
        finalized["revision"] = revision
        if kind == REPORT_KIND_FORMAL:
            finalized["report_id"] = f"nr-{trade_date.replace('-', '')}-{revision:02d}"
        elif kind == REPORT_KIND_REPLAY:
            # 版本号必须进 report_id。回放是按需重建的，固定 id 会在内容变化时撞上
            # "同 id 不覆盖"的保护：既写不进新内容，又照样把指针指过去，于是"盘上
            # 是第 1 版、声称已发布第 N 版"，而且不报错（2026-09-17 实测踩到）。
            finalized["report_id"] = f"rp-{trade_date.replace('-', '')}-{revision:02d}"
        else:
            finalized["report_id"] = _text(finalized.get("report_id")) or self.notice_report_id(
                trade_date, _text(finalized.get("notice"))
            )
        finalized["content_digest"] = self.content_digest(finalized)
        resolved_id = _text(finalized.get("report_id"))
        _, written = self.freeze_report(finalized)
        if not written:
            stored = self.load_report(resolved_id, trade_date=trade_date)
            if stored is None:
                # 文件路径存在但读不出来：存储故障，不能假装发布成功。
                raise RuntimeError(f"report {resolved_id} exists but cannot be read")
            if _text(stored.get("content_digest")) != _text(finalized.get("content_digest")):
                # 同一个 id 已经被冻结成**另一份内容**（id 不带版本号的那些报告才会
                # 走到这里）。此时既不能声称发布了新版本，也不能把指针挪向一份没写
                # 进磁盘的内容——以磁盘上那份为准，并把指针校准回它，保持幂等。
                self.update_date_state(
                    trade_date,
                    self._pointer_patch(
                        trade_date=trade_date,
                        kind=kind,
                        report_id=resolved_id,
                        report=stored,
                        revision=_optional_int(stored.get("revision")) or 1,
                    ),
                )
                return {
                    "published": False,
                    "reason": "already_frozen_with_different_content",
                    "report_id": resolved_id,
                    "report": stored,
                }
        self.update_date_state(
            trade_date,
            self._pointer_patch(
                trade_date=trade_date,
                kind=kind,
                report_id=resolved_id,
                report=finalized,
                revision=revision,
            ),
        )
        return {
            "published": True,
            "reason": "new_revision" if revision > 1 else "created",
            "report_id": resolved_id,
            "report": finalized,
        }

    def _pointer_patch(
        self,
        *,
        trade_date: str,
        kind: str,
        report_id: str,
        report: Mapping[str, object],
        revision: int,
    ) -> dict[str, object]:
        """构造"把日期状态的指针挪到这份报告"所需的 patch。

        三类报告各有各的指针：正式结果绝不会被过程说明或验收回放顶掉。
        """
        patch: dict[str, object] = {
            "last_published_report_id": report_id,
            "last_published_at": report.get("generated_at", ""),
        }
        if kind == REPORT_KIND_FORMAL:
            patch.update(
                {
                    "published_report_id": report_id,
                    "published_at": report.get("generated_at", ""),
                    "published_scan_status": _text(report.get("scan_status")),
                    "published_revision": revision,
                }
            )
            return patch
        state = self.read_date_state(trade_date)
        notices = _mapping(state.get("notices"))
        if kind == REPORT_KIND_REPLAY:
            notice_key = "replay"
        else:
            notice_key = "delay" if report.get("notice") == NOTICE_DELAY else "deadline"
        notices[notice_key] = report_id
        patch["notices"] = notices
        return patch

    def published_report(self, trade_date: str) -> dict[str, object] | None:
        state = self.read_date_state(trade_date)
        report_id = _text(state.get("published_report_id"))
        if not report_id:
            return None
        return self.load_report(report_id, trade_date=trade_date)

    # -------------------------------------------------------------- 消息渲染

    def render(self, report: Mapping[str, object]) -> RenderedMessage:
        """渲染飞书正文（纯函数：同一份冻结报告永远渲染出同一段文字）。"""
        if _text(report.get("report_kind")) == REPORT_KIND_NOTICE:
            return self._render_notice(report)
        limit = max(200, int(self.config.message_max_chars))
        top_k = max(1, int(self.config.display_top_k))
        title = self._title(report)
        for level in range(6):
            content = self._render_body(report, level=level, top_k=top_k)
            if len(content) <= limit:
                return RenderedMessage(title=title, content=content, truncated=level > 0)
        return RenderedMessage(
            title=title,
            content=self._render_body(report, level=5, top_k=top_k),
            truncated=True,
        )

    def _title(self, report: Mapping[str, object]) -> str:
        trade_date = _text(report.get("trade_date"))
        kind = _text(report.get("report_kind"))
        if kind == REPORT_KIND_NOTICE:
            label = "延迟说明" if report.get("notice") == NOTICE_DELAY else "未完成说明"
            return f"【晚间选股报告】{trade_date}（{label}）"
        if kind == REPORT_KIND_REPLAY:
            return f"【晚间选股报告·验收回放】{trade_date}"
        revision = _optional_int(report.get("revision")) or 1
        suffix = "（修订版）" if revision > 1 else ""
        return f"【晚间选股报告】{trade_date}{suffix}"

    def _render_notice(self, report: Mapping[str, object]) -> RenderedMessage:
        trade_date = _text(report.get("trade_date"))
        notice = _text(report.get("notice"))
        label = "延迟说明" if notice == NOTICE_DELAY else "未完成说明"
        lines = [f"【晚间选股报告】{trade_date}（{label}）"]
        if notice == NOTICE_DELAY:
            lines.append("结果：截至目标时间尚未完成有效选股")
        else:
            lines.append("结果：截至截止时间仍未形成有效结果")
        lines.append(f"扫描阶段：{_text(report.get('scan_phase')) or '未知'}")
        lines.append(f"已执行重型扫描次数：{_optional_int(report.get('scan_attempts')) or 0}")
        waiting = _text(report.get("waiting_reason"))
        if waiting:
            lines.append(f"等待/阻断原因：{_localize_reason(waiting)}")
        reason = _text(report.get("reason"))
        if reason:
            lines.append(f"说明：{reason}")
        last_status = _text(report.get("last_scan_status"))
        if last_status:
            lines.append(f"最近一次扫描状态：{last_status}")
        lines.append("")
        lines.append("本条为过程说明，不是选股结果；数据或流程恢复后会另行发送正式报告。")
        lines.append("本报告用于隔夜观察；是否满足盘中买入条件需另行确认，不构成买入指令。")
        return RenderedMessage(title=self._title(report), content="\n".join(lines), truncated=False)

    def _render_body(self, report: Mapping[str, object], *, level: int, top_k: int) -> str:
        trade_date = _text(report.get("trade_date"))
        status = _text(report.get("scan_status")) or SCAN_STATUS_BLOCKED
        observation = _mapping_list(report.get("observation_candidates"))
        incomplete = _mapping_list(report.get("incomplete_candidates"))
        funnel = _mapping(report.get("funnel_counts"))
        lines: list[str] = [f"【晚间选股报告】{trade_date}"]
        if _text(report.get("report_kind")) == REPORT_KIND_REPLAY:
            source = _text(report.get("replay_source")) or "历史夜扫产物"
            lines = [
                f"【晚间选股报告·验收回放】{trade_date}",
                f"【验收回放】本消息内容取自 {source}，是历史数据，不是当日结果；",
                "仅用于验证晚间选股结果能否可靠送达，请不要据此做任何交易判断。",
            ]

        if status == SCAN_STATUS_COMPLETED:
            if not observation and incomplete:
                # "完成 + 0 只观察"单独说会读成"今天没有机会"，但真实原因是候选
                # 根本没通过完整风险检查——必须说清是哪一种原因。
                descriptor = _incomplete_descriptor(incomplete)
                lines.append(
                    f"结果：今日选股完成，无通过完整风险检查的隔夜观察候选"
                    f"（{len(incomplete)} 只{descriptor}）"
                )
            else:
                lines.append(f"结果：今日选股完成，隔夜观察候选 {len(observation)} 只")
        elif status == SCAN_STATUS_EMPTY:
            lines.append("结果：今日正常完成，无合格候选")
        elif status == SCAN_STATUS_BLOCKED:
            lines.append("结果：未完成有效选股（数据或必要检查未通过）")
        else:
            lines.append("结果：扫描失败")

        data_snapshot = _text(report.get("data_snapshot_id"))
        fallback_date = _text(report.get("fallback_source_date"))
        lines.append(f"数据日期：{fallback_date or data_snapshot or trade_date or '未知'}")

        if status in {SCAN_STATUS_BLOCKED, SCAN_STATUS_FAILED}:
            reason_lines = self._block_reason_lines(report)
            if reason_lines:
                lines.extend(reason_lines)
            elif status == SCAN_STATUS_FAILED:
                lines.append("失败原因：扫描异常或超时，详见报告")

        if fallback_date:
            lines.append(f"旧池回退：以下为 {fallback_date} 的结果，仅供回顾，不计入今日入选数量")

        if level <= 3 and observation:
            lines.append("")
            lines.extend(self._candidate_lines(observation, level=level, top_k=top_k))

        if level <= 3 and incomplete:
            lines.append("")
            lines.extend(self._incomplete_lines(incomplete, level=level))

        if level <= 2:
            funnel_line = self._funnel_line(funnel)
            if funnel_line:
                lines.append("")
                lines.append(funnel_line)
            exclusion = self._exclusion_line(report)
            if exclusion:
                lines.append(exclusion)
            final_count = _optional_int(report.get("final_selection_count")) or 0
            lines.append(f"最终筛选数量（审计字段，非买入信号）：{final_count}")
        elif level <= 4:
            lines.append("")
            lines.append(f"观察候选 {len(observation)} 只；完整清单与筛选过程见报告。")

        lines.append("")
        lines.append("本报告用于隔夜观察；是否满足盘中买入条件需另行确认，不构成买入指令。")
        return "\n".join(lines)

    def _candidate_lines(
        self,
        observation: list[dict[str, object]],
        *,
        level: int,
        top_k: int,
    ) -> list[str]:
        lines: list[str] = []
        # 正文最多展示 display_top_k 只；只有 1 只就展示 1 只，不补足数量。
        # 全部候选仍在报告的 observation_candidates 里，正文截断不影响业务事实。
        shown = observation[:top_k] if level <= 1 else observation[:1]
        for index, item in enumerate(shown, start=1):
            name = _text(item.get("name")) or "名称暂缺"
            symbol = _text(item.get("symbol"))
            score = _format_score(item.get("score"))
            if level >= 2:
                lines.append(f"{index}. {symbol}｜{name}｜评分 {score}")
                continue
            lines.append(f"{index}. {symbol}｜{name}｜评分 {score}")
            reasons = _string_list(item.get("reasons"))[:_MAX_CANDIDATE_REASONS]
            if reasons:
                lines.append(f"   入选依据：{'、'.join(reasons)}")
            risks = _string_list(item.get("risks"))[:_MAX_CANDIDATE_RISKS]
            lines.append(f"   风险说明：{'、'.join(risks) if risks else '未发现明显风险项'}")
        remaining = len(observation) - len(shown)
        if remaining > 0:
            lines.append(f"（另有 {remaining} 只候选未在正文展示，完整清单见报告）")
        return lines

    def _incomplete_lines(
        self,
        incomplete: list[dict[str, object]],
        *,
        level: int,
    ) -> list[str]:
        """按原因分组列出未通过完整风险检查的候选。

        两种原因的处置完全不同，混在一句话里会误导：
        - ``insufficient_input``：**确实**算不出指标（行情太短/数值无效/ATR 为 0），
          是真缺数据；
        - ``unknown``：旧版本产物没有写评估状态字段，风控其实跑过，只是无法确认。
          把它说成"数据待补全"会让人以为指标算不出来。
        """
        groups = (
            (EVALUATION_INCOMPLETE, "数据待补全（风险指标输入不足，不计入观察候选）："),
            (EVALUATION_UNKNOWN, "评估状态未标注（旧版本产物，不计入观察候选）："),
        )
        limit = _max_incomplete_lines(level)
        handled: set[str] = set()
        lines: list[str] = []
        for status, heading in groups:
            handled.add(status)
            items = [item for item in incomplete if _text(item.get("evaluation_status")) == status]
            if not items:
                continue
            lines.append(heading)
            lines.extend(self._incomplete_line(item, level=level) for item in items[:limit])
        rest = [item for item in incomplete if _text(item.get("evaluation_status")) not in handled]
        if rest:
            lines.append("其他未通过完整风险检查（不计入观察候选）：")
            lines.extend(self._incomplete_line(item, level=level) for item in rest[:limit])
        return lines

    def _incomplete_line(self, item: dict[str, object], *, level: int) -> str:
        symbol = _text(item.get("symbol"))
        name = _text(item.get("name")) or "名称暂缺"
        score = _format_score(item.get("score"))
        missing = _string_list(item.get("missing_inputs"))
        detail = f"｜缺 {'/'.join(missing)}" if missing else ""
        if level >= 2:
            return f"- {symbol}｜评分 {score}{detail}"
        return f"- {symbol}｜{name}｜评分 {score}{detail}"

    def _funnel_line(self, funnel: Mapping[str, object]) -> str:
        pool_stages: tuple[tuple[str, str], ...] = (
            ("input_count", "输入"),
            ("eligible_count", "质量硬筛"),
            ("quality_pool_count", "质量池"),
        )
        if not any(key in funnel for key, _ in pool_stages):
            # 质量选择器没跑时这三个位阶并不存在。此时退回展示候选域，并且换一个
            # 标签——否则"输入"在两次运行里会表示完全不同的东西。
            pool_stages = (("candidate_universe_count", "候选域"),)
        stages = pool_stages + (
            ("light_count", "轻筛"),
            ("deep_count", "深评"),
            ("observation_count", "观察"),
        )
        parts = [f"{label}{funnel[key]}" for key, label in stages if key in funnel]
        if not parts:
            return ""
        return "筛选过程：" + " → ".join(parts)

    def _exclusion_line(self, report: Mapping[str, object]) -> str:
        summary = _mapping(report.get("exclusion_summary"))
        counts = _mapping(summary.get("reject_reason_counts"))
        ordered = sorted(
            ((str(key), _optional_int(value) or 0) for key, value in counts.items()),
            key=lambda item: (-item[1], item[0]),
        )[:_MAX_EXCLUSION_REASONS]
        if not ordered:
            return ""
        rendered = "、".join(f"{_localize_reason(key)} {value}" for key, value in ordered)
        return f"主要过滤原因：{rendered}"

    def _block_reason_lines(self, report: Mapping[str, object]) -> list[str]:
        lines: list[str] = []
        gate = _mapping(report.get("data_gate"))
        reasons = _string_list(gate.get("reasons"))
        blocking = [
            reason for reason in reasons if reason not in {"eligible_universe_coverage_degraded"}
        ]
        if blocking:
            lines.append("阻断原因：" + "、".join(_localize_reason(item) for item in blocking[:5]))
        summary = _mapping(report.get("exclusion_summary"))
        fallback_reason = _text(summary.get("fallback_reason"))
        if fallback_reason:
            lines.append(f"回退原因：{_localize_reason(fallback_reason)}")
        failure_stage = _text(report.get("failure_stage"))
        failure_reason = _text(report.get("failure_reason"))
        if failure_stage:
            lines.append(f"失败阶段：{failure_stage}")
        if failure_reason:
            lines.append(f"失败原因：{failure_reason}")
        return lines

    # ------------------------------------------------------------ 内部构造

    def _derive_scan_status(
        self,
        *,
        night_scan: Mapping[str, object],
        fallback: Mapping[str, object],
        data_gate: Mapping[str, object],
        has_rows: bool,
        override: str,
    ) -> str:
        if _text(override) in SCAN_STATUSES:
            return _text(override)
        raw_status = _text(night_scan.get("status")).lower()
        if bool(fallback.get("applied", False)):
            # 旧池回退不是"今天没有机会"，是"今天没算出来"。
            return SCAN_STATUS_BLOCKED
        if raw_status in {"failed", "error", "shadow_failed", "unavailable"}:
            return SCAN_STATUS_FAILED
        if raw_status in {"blocked", "blocked_data_gate"}:
            return SCAN_STATUS_BLOCKED
        if _text(data_gate.get("status")).lower() == "blocked":
            return SCAN_STATUS_BLOCKED
        return SCAN_STATUS_COMPLETED if has_rows else SCAN_STATUS_EMPTY

    def _fallback_source_date(self, night_scan: Mapping[str, object]) -> str:
        pool = _mapping_list(night_scan.get("overnight_top5")) or _mapping_list(
            night_scan.get("night_pool")
        )
        for item in pool:
            updated = _text(item.get("night_pool_trade_date")) or _text(item.get("trade_date"))
            if updated:
                return updated
        return ""

    def _split_candidates(
        self,
        rows: list[dict[str, object]],
        *,
        limit: int,
        name_resolver: Callable[[str], str] | None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        observation: list[dict[str, object]] = []
        incomplete: list[dict[str, object]] = []
        resolve_names = self._name_resolver(name_resolver)
        for row in rows:
            entry = self._candidate_entry(row)
            status = _text(entry.get("evaluation_status"))
            if status == EVALUATION_EVALUATED:
                observation.append(entry)
            else:
                incomplete.append(entry)
        # 名称只为本就展示的条目解析（正文最多 limit 只），避免为几十只候选逐只
        # 回源拉名称把报告构造拖成分钟级；解析失败只写"名称暂缺"，不阻断报告。
        for entry in [*observation[:limit], *incomplete[:limit]]:
            symbol = _text(entry.get("symbol"))
            if not symbol:
                continue
            entry["name"] = resolve_names(symbol)
        return observation, incomplete

    def _name_resolver(self, override: Callable[[str], str] | None) -> Callable[[str], str]:
        if override is not None:
            return override
        resolver = getattr(self._service, "_resolve_symbol_display_name", None)
        if not callable(resolver):
            return lambda _symbol: ""

        def _resolve(symbol: str) -> str:
            try:
                return _text(resolver(symbol))
            except Exception:  # noqa: BLE001 - 名称查询绝不阻断报告
                return ""

        return _resolve

    def _candidate_entry(self, row: Mapping[str, object]) -> dict[str, object]:
        overextension = _mapping(row.get("overextension"))
        board_risk = _mapping(row.get("board_risk"))
        evaluation_status = _text(overextension.get("evaluation_status"))
        if evaluation_status not in {EVALUATION_EVALUATED, EVALUATION_INCOMPLETE}:
            # 旧产物没有这个字段：不能默认"已经完整评估"，否则缺输入的候选会被
            # 当成通过检查的观察候选混进正文。
            evaluation_status = EVALUATION_UNKNOWN
        # 入选依据优先用漏斗自己声明的 shortlist_reasons（已是可读短句）；只有在
        # 它为空时才回落到原始信号代号——把 soup_entry / news_component:0.5 这类
        # 内部代号堆在用户面前，等于什么都没说。
        reasons: list[str] = []
        for code in _string_list(row.get("shortlist_reasons")):
            label = _SHORTLIST_REASON_LABELS.get(code, code)
            if label not in reasons:
                reasons.append(label)
        if not reasons:
            for raw in _string_list(row.get("reasons")):
                if raw.startswith(("board_component:", "completion_component:")):
                    continue
                reasons.append(raw)
        risks: list[str] = []
        for code in _string_list(overextension.get("reasons")):
            label = _OVEREXTENSION_REASON_LABELS.get(code, code)
            if label not in risks:
                risks.append(label)
        for code in _string_list(board_risk.get("reasons")):
            label = f"连板风险：{code}"
            if label not in risks:
                risks.append(label)
        metrics = _mapping(overextension.get("metrics"))
        atr_distance = metrics.get("atr_distance")
        if isinstance(atr_distance, (int, float)) and float(atr_distance) >= 2.0:
            risks.append(f"距 MA5 约 {float(atr_distance):.1f} 倍 ATR")
        return {
            "symbol": _text(row.get("symbol")),
            "name": _text(row.get("name")),
            "score": _as_float(row.get("score")),
            "rank": _optional_int(row.get("rank")) or 0,
            "evaluation_status": evaluation_status,
            "missing_inputs": _string_list(overextension.get("missing_inputs")),
            "overextension_level": _text(overextension.get("level")),
            "reasons": reasons,
            "risks": risks,
            "actionable": False,
        }

    def _funnel_counts(
        self,
        source_report: Mapping[str, object],
        *,
        rows: list[dict[str, object]],
        observation: list[dict[str, object]],
        incomplete: list[dict[str, object]],
    ) -> dict[str, object]:
        prefilter = _mapping(source_report.get("prefilter"))
        selection = _mapping(prefilter.get("universe_quality_selection"))
        funnel = _mapping(source_report.get("funnel"))
        counts = {
            # 全市场输入 / 质量硬筛 / 质量池只取自**质量选择器自己的账**
            # （universe_quality_selection）。prefilter.universe_count 与
            # eligible_count 是质量池裁完之后的候选域（实测两者都等于 300），
            # 拿它当"输入"会把 5487 只显示成 300 只——2026-09-16 首条回放消息
            # 就是这么错的。
            "input_count": _optional_int(selection.get("input_count")),
            "eligible_count": _optional_int(selection.get("hard_eligible_count")),
            "quality_pool_count": _optional_int(selection.get("selected_count"))
            or _optional_int(selection.get("target_size")),
            # 质量选择器没跑时的退路：候选域（报告里保留作审计，正文按需展示）。
            "candidate_universe_count": _optional_int(prefilter.get("universe_count")),
            "light_count": _optional_int(funnel.get("light_count")),
            "deep_count": _optional_int(funnel.get("deep_count")),
            "final_count": _optional_int(funnel.get("final_count")),
            "night_pool_count": len(rows),
            "observation_count": len(observation),
            "incomplete_count": len(incomplete),
        }
        return {key: value for key, value in counts.items() if value is not None}

    def _exclusion_summary(
        self,
        source_report: Mapping[str, object],
        *,
        data_gate: Mapping[str, object],
        fallback: Mapping[str, object],
        status: str,
    ) -> dict[str, object]:
        funnel = _mapping(source_report.get("funnel"))
        final_selection = _mapping(funnel.get("final_selection"))
        counts: dict[str, int] = {}
        for item in _mapping_list(final_selection.get("rejected")):
            for reason in _string_list(item.get("reject_reasons")):
                counts[reason] = counts.get(reason, 0) + 1
        return {
            "reject_reason_counts": counts,
            "candidate_gate_reasons": _string_list(data_gate.get("reasons")),
            "fallback_reason": _text(fallback.get("reason"))
            if status == SCAN_STATUS_BLOCKED
            else "",
            "data_gate_status": _text(data_gate.get("status")),
        }


def _max_incomplete_lines(level: int) -> int:
    return 5 if level <= 1 else 2


def _incomplete_descriptor(incomplete: Sequence[Mapping[str, object]]) -> str:
    """一句话概括"为什么这些候选没进观察池"（供状态行使用）。"""
    statuses = {_text(item.get("evaluation_status")) for item in incomplete}
    if statuses == {EVALUATION_INCOMPLETE}:
        return "数据待补全"
    if statuses == {EVALUATION_UNKNOWN}:
        return "评估状态未标注"
    return "未通过完整风险检查"


def _localize_reason(reason: str) -> str:
    normalized = _text(reason)
    if normalized in _BLOCKING_REASON_LABELS:
        return _BLOCKING_REASON_LABELS[normalized]
    if normalized in _REJECT_REASON_LABELS:
        return _REJECT_REASON_LABELS[normalized]
    if normalized.startswith("data_gate:"):
        # data_gate 的拒因带状态后缀（如 data_gate:blocked），前缀可读化即可
        return f"数据门禁未通过（{normalized.split(':', 1)[1]}）"
    return normalized


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, ensure_ascii=False, separators=(",", ":"), default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
