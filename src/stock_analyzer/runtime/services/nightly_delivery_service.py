"""正式晚报的逐目标交付：持久化、幂等、重试与崩溃恢复。

为什么不能沿用通用通知链（对应 2026-09-16 v2 方案 §3.3—§3.5）：

- 通用链的成功是"某条渠道返回 success"，主渠道失败会 failover 到 console，而
  console 恒返回 ``success=True``——于是"调度 green"与"用户收到结果"被混为一谈。
  这里逐目标记录状态，且 console 永远不可能产生 ``delivered``。
- 通用链的去重缓存 ``_notify_if_changed`` 在**发送之前**写指纹，首次发送失败后
  同样的内容会被判成"没变化"而永久不再重试。这里的去重是"发成功了才写 delivered"。
- 重试要能在**跨进程重启**后继续：状态全部落在交付记录文件里，靠调度时间推进，
  不在 worker 里 sleep。

存储布局（沿用 artifacts 命名卷）::

    runtime/nightly_delivery/<delivery_id>.json          该报告对某目标的发送状态
    runtime/nightly_delivery/<delivery_id>.lock          交付锁（网络期间持有）
    runtime/nightly_delivery/<delivery_id>.receipt.json  发送后、并账前的回执证据

``delivery_id = <report_id>__<target_key>`` 是稳定键：同一报告同一目标永远映射到
同一条记录，因此"重复触发"天然幂等。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from stock_analyzer.config import NightlyReportConfig
from stock_analyzer.notify.channels import (
    OUTCOME_ACCEPTED,
    OUTCOME_UNKNOWN,
    FeishuAppNotifier,
    NotificationMessage,
    TargetDeliveryOutcome,
    send_explicit,
)
from stock_analyzer.notify.filter import is_quiet_time
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.runtime.notifier_factory import build_channel
from stock_analyzer.runtime.services.nightly_report_service import (
    NOTICE_DEADLINE,
    NOTICE_DELAY,
    SCAN_STATUS_BLOCKED,
    NightlyReportService,
)

STATE_PENDING = "pending"
STATE_SENDING = "sending"
STATE_RETRY_WAIT = "retry_wait"
STATE_DELIVERED = "delivered"
STATE_UNKNOWN = "unknown"
STATE_EXHAUSTED = "exhausted"

AGGREGATE_NOT_ENQUEUED = "not_enqueued"
AGGREGATE_PENDING = "pending"
AGGREGATE_SENDING = "sending"
AGGREGATE_DELIVERED = "delivered"
AGGREGATE_PARTIAL = "partial"
AGGREGATE_UNKNOWN = "unknown"
AGGREGATE_EXHAUSTED = "exhausted"

# 交付锁的兜底失效时间。必须**远大于**单次网络预算（默认 20s）：持有者仍然活着
# 只是请求慢的时候，别的 worker 绝不能把锁判成失联并重复发送。
_LEASE_MIN_SEC = 120
# 线程级总预算兜底：urlopen 的 timeout 覆盖不到 DNS 解析，极端情况下会超过预算。
_BUDGET_BACKSTOP_MARGIN_SEC = 5.0


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    key: str
    required: bool
    notifier: object
    idempotent: bool


class NightlyDeliveryService:
    """待发记录、目标结果、并发控制、重试与恢复。"""

    def __init__(self, service: Any) -> None:
        self._service = service
        self.config: NightlyReportConfig = getattr(service._config, "nightly", None) or (
            NightlyReportConfig()
        )
        self.report_service = NightlyReportService(service)
        self.root = self._resolve_path(self.config.delivery_root)
        self._targets_cache: list[DeliveryTarget] | None = None

    # ------------------------------------------------------------------ 路径

    def _resolve_path(self, raw: str) -> Path:
        resolver = getattr(self._service, "_resolve_evolution_path", None)
        if callable(resolver):
            try:
                return Path(resolver(str(raw)))
            except Exception:  # noqa: BLE001
                pass
        return Path(str(raw))

    def record_path(self, delivery_id: str) -> Path:
        return self.root / f"{_text(delivery_id)}.json"

    def receipt_path(self, delivery_id: str) -> Path:
        return self.root / f"{_text(delivery_id)}.receipt.json"

    def lock_path(self, delivery_id: str) -> Path:
        return self.root / f"{_text(delivery_id)}.lock"

    @staticmethod
    def delivery_id_for(report_id: str, target_key: str) -> str:
        safe_report = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in _text(report_id)
        )
        safe_target = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in _text(target_key)
        )
        return f"{safe_report}__{safe_target}"

    # ---------------------------------------------------------------- 目标

    def _build_targets(self) -> list[DeliveryTarget]:
        """沿用既有主应用目标与企业分发配置，不新建机器人、不改收件人。

        刻意**不**复用 ``build_notifier`` 的 FailoverNotifier：它的备份渠道是
        console，而 console 的 success 会被当成整体成功（9/16 实据）。这里直接
        解析出真实目标，主应用为必需目标，企业分发沿用"可选目标"语义。
        """
        notifications = getattr(self._service._config, "notifications", None)
        if notifications is None:
            return []
        primary_name = str(getattr(notifications, "primary", "") or "").strip().lower()
        if not primary_name:
            return []
        timeout_sec = max(1, int(self.config.request_timeout_sec))
        targets: list[DeliveryTarget] = []

        def _build(channel_name: str) -> Any:
            notifier: Any = build_channel(config=self._service._config, channel_name=channel_name)
            # 单个 HTTP 调用的预算按晚报配置走（默认 20s），而不是白天通知的 5s：
            # 晚报是当天唯一一份结果，值得比"一条普通提醒"多等一会儿。
            if hasattr(notifier, "timeout_sec"):
                try:
                    notifier.timeout_sec = timeout_sec
                except (AttributeError, TypeError):
                    pass
            return notifier

        primary_channel = _build(primary_name)
        targets.append(
            DeliveryTarget(
                key=primary_name,
                required=True,
                notifier=primary_channel,
                idempotent=isinstance(primary_channel, FeishuAppNotifier),
            )
        )
        enterprise_enabled = bool(getattr(notifications, "feishu_enterprise_enabled", False))
        if enterprise_enabled and primary_name != "feishu_enterprise":
            enterprise_channel = _build("feishu_enterprise")
            targets.append(
                DeliveryTarget(
                    key="feishu_enterprise",
                    required=False,
                    notifier=enterprise_channel,
                    # 批量发送接口没有已核实的幂等参数，故不确定结果不自动重试。
                    idempotent=False,
                )
            )
        return targets

    def targets(self) -> list[DeliveryTarget]:
        if self._targets_cache is None:
            self._targets_cache = self._build_targets()
        return self._targets_cache

    def _target_for(self, key: str) -> DeliveryTarget | None:
        normalized = _text(key)
        for target in self.targets():
            if target.key == normalized:
                return target
        return None

    # ---------------------------------------------------------------- 记录

    def load_record(self, delivery_id: str) -> dict[str, object] | None:
        path = self.record_path(delivery_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_record(self, record: Mapping[str, object]) -> None:
        delivery_id = _text(record.get("delivery_id"))
        if not delivery_id:
            raise ValueError("delivery record requires delivery_id")
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.record_path(delivery_id), dict(record))

    def records_for_report(
        self, report_id: str, *, trade_date: str = ""
    ) -> list[dict[str, object]]:
        normalized = _text(report_id)
        if not normalized:
            return []
        wanted = {self.delivery_id_for(normalized, target.key) for target in self.targets()}
        state = (
            self.report_service.read_date_state(trade_date)
            if _text(trade_date)
            else self._state_for_report(normalized)
        )
        known = _string_list(state.get("delivery_ids"))
        candidates = sorted(wanted | {item for item in known if item.startswith(f"{normalized}__")})
        records: list[dict[str, object]] = []
        for delivery_id in candidates:
            record = self.load_record(delivery_id)
            if record is not None and _text(record.get("report_id")) == normalized:
                records.append(record)
        return records

    def _state_for_report(self, report_id: str) -> dict[str, object]:
        for trade_date in self.report_service.recent_trade_dates():
            state = self.report_service.read_date_state(trade_date)
            report_ids = {
                _text(state.get("published_report_id")),
                *(_text(item) for item in _mapping(state.get("notices")).values()),
            }
            delivery_ids = _string_list(state.get("delivery_ids"))
            if report_id in report_ids or any(
                item.startswith(f"{report_id}__") for item in delivery_ids
            ):
                return state
        return {}

    def ensure_records(self, report: Mapping[str, object]) -> list[dict[str, object]]:
        """为该报告建立待发记录（幂等：已存在则原样返回）。

        正文与 uuid 在**首次尝试前**就冻结进记录：重试必须逐字一致，否则"同一报告
        重试"会变成"给用户发了两个版本的正文"，也没法和已发出的回执对账。
        """
        report_id = _text(report.get("report_id"))
        trade_date = _text(report.get("trade_date"))
        if not report_id or not trade_date:
            return []
        rendered = self.report_service.render(report)
        records: list[dict[str, object]] = []
        for target in self.targets():
            delivery_id = self.delivery_id_for(report_id, target.key)
            existing = self.load_record(delivery_id)
            if existing is not None:
                records.append(existing)
                continue
            now_iso = datetime.now().isoformat()
            record: dict[str, object] = {
                "schema_version": 1,
                "delivery_id": delivery_id,
                "report_id": report_id,
                "report_kind": _text(report.get("report_kind")),
                "trade_date": trade_date,
                "revision": report.get("revision", 0),
                "target_key": target.key,
                "required": target.required,
                "idempotent": target.idempotent,
                "state": STATE_PENDING,
                "attempts": 0,
                "max_attempts": max(1, int(self.config.max_delivery_attempts)),
                "request_uuid": uuid4().hex,
                "content_digest": _text(report.get("content_digest")),
                "title": rendered.title,
                "content": rendered.content,
                "message_truncated": rendered.truncated,
                # 空串 = 立即可发。这里刻意不写"当前墙钟"：记录创建时间与调用方的
                # 逻辑时钟（调度时间/测试注入时间）不是同一个时间轴，写死墙钟会让
                # 新建记录在一段时间内被判成"还没到点"而静默漏发。
                "next_retry_at": "",
                "lease_owner": "",
                "lease_until": "",
                "first_attempt_at": "",
                "last_attempt_at": "",
                "accepted_at": "",
                "message_id": "",
                "error_code": "",
                "error_message": "",
                "needs_attention": False,
                "manual_retries": 0,
                "created_at": now_iso,
                "updated_at": now_iso,
                "history": [],
            }
            self.save_record(record)
            self._register_delivery_id(trade_date, delivery_id)
            records.append(record)
        return records

    def _register_delivery_id(self, trade_date: str, delivery_id: str) -> None:
        """把 delivery_id 记进日期状态：交付检查据此定位记录，不必扫描交付目录。"""
        state = self.report_service.read_date_state(trade_date)
        known = _string_list(state.get("delivery_ids"))
        if delivery_id in known:
            return
        known.append(delivery_id)
        self.report_service.update_date_state(
            trade_date,
            {"delivery_ids": known[:200]},
        )

    # ---------------------------------------------------------------- 查询

    def status(self, report_id: str, *, trade_date: str = "") -> dict[str, object]:
        records = self.records_for_report(report_id, trade_date=trade_date)
        return self.summarize(records, report_id=_text(report_id))

    @staticmethod
    def summarize(records: Sequence[Mapping[str, object]], *, report_id: str) -> dict[str, object]:
        if not records:
            return {
                "report_id": report_id,
                "delivery_status": AGGREGATE_NOT_ENQUEUED,
                "required_target_delivered": False,
                "last_delivery_error": "",
                "targets": [],
            }
        required = [item for item in records if bool(item.get("required", False))]
        required_ok = bool(required) and all(
            _text(item.get("state")) == STATE_DELIVERED for item in required
        )
        states = {_text(item.get("state")) for item in records}
        all_delivered = states == {STATE_DELIVERED}
        if all_delivered:
            aggregate = AGGREGATE_DELIVERED
        elif required_ok:
            # 必需目标成功即可判交付成功；可选目标失败独立保留状态与告警。
            aggregate = AGGREGATE_PARTIAL
        elif states & {STATE_PENDING, STATE_RETRY_WAIT}:
            aggregate = AGGREGATE_PENDING
        elif STATE_SENDING in states:
            aggregate = AGGREGATE_SENDING
        elif STATE_UNKNOWN in states:
            aggregate = AGGREGATE_UNKNOWN
        else:
            aggregate = AGGREGATE_EXHAUSTED

        last_error = ""
        for item in records:
            message = _text(item.get("error_message")) or _text(item.get("error_code"))
            if message and _text(item.get("state")) != STATE_DELIVERED:
                last_error = message
        return {
            "report_id": report_id,
            "delivery_status": aggregate,
            "required_target_delivered": required_ok,
            "last_delivery_error": last_error[:300],
            "targets": [
                {
                    "target_key": _text(item.get("target_key")),
                    "required": bool(item.get("required", False)),
                    "state": _text(item.get("state")),
                    "attempts": _int(item.get("attempts")),
                    "message_id": _text(item.get("message_id")),
                    "error_code": _text(item.get("error_code")),
                    "error_message": _text(item.get("error_message"))[:200],
                    "accepted_at": _text(item.get("accepted_at")),
                    "next_retry_at": _text(item.get("next_retry_at")),
                    "needs_attention": bool(item.get("needs_attention", False)),
                }
                for item in sorted(records, key=lambda entry: _text(entry.get("target_key")))
            ],
        }

    # ---------------------------------------------------------------- 补发

    def request_retry(
        self,
        report_id: str,
        *,
        confirm_unknown: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """仅入队，不等待网络结果。只操作已存在的报告，只补未成功的目标。"""
        report = self.report_service.load_report(report_id)
        if report is None:
            return {"queued": False, "reason": "report_not_found", "report_id": _text(report_id)}
        current = self._now(now)
        records = self.ensure_records(report)
        queued: list[str] = []
        conflicts: list[dict[str, object]] = []
        skipped: list[str] = []
        pending_writes: list[dict[str, object]] = []
        for record in records:
            state = _text(record.get("state"))
            if state == STATE_DELIVERED:
                skipped.append(_text(record.get("target_key")))
                continue
            uncertain_outside_window = _text(
                record.get("last_outcome")
            ) == OUTCOME_UNKNOWN and not self._within_unknown_window(record, now=current)
            if uncertain_outside_window and not confirm_unknown:
                # 上一次结果不确定且已经出了去重窗口：再发一次有可能真的重复推送，
                # 必须由人显式确认（例如已经核对过飞书里没有收到）。
                conflicts.append(
                    {
                        "target_key": _text(record.get("target_key")),
                        "reason": "unknown_outside_dedup_window",
                        "error_code": _text(record.get("error_code")),
                    }
                )
                continue
            updated = dict(record)
            updated["state"] = STATE_PENDING
            updated["next_retry_at"] = current.isoformat()
            updated["attempts"] = 0
            updated["manual_retries"] = _int(record.get("manual_retries")) + 1
            updated["needs_attention"] = False
            updated["updated_at"] = current.isoformat()
            pending_writes.append(updated)
            queued.append(_text(record.get("target_key")))
        for updated in pending_writes:
            self.save_record(updated)
        if conflicts:
            return {
                "queued": False,
                "reason": "confirm_required",
                "report_id": _text(report_id),
                "conflicts": conflicts,
                "queued_targets": queued,
                "skipped_targets": skipped,
            }
        return {
            "queued": bool(queued),
            "reason": "queued" if queued else "nothing_to_do",
            "report_id": _text(report_id),
            "queued_targets": queued,
            "skipped_targets": skipped,
            "conflicts": [],
        }

    # ------------------------------------------------------------ 恢复 / 检查

    def recover(self, *, now: datetime | None = None) -> dict[str, object]:
        """修复"报告中途落盘/发送后未并账/持锁进程消失"三类中断状态。

        只回看当前交易日与最近一个交易日；不扫描整个历史目录。
        """
        current = self._now(now)
        applied_receipts: list[str] = []
        recovered_sending: list[str] = []
        created_records: list[str] = []
        for trade_date in self.report_service.recent_trade_dates():
            for report in self.report_service.reports_for(trade_date):
                report_id = _text(report.get("report_id"))
                before = {
                    _text(item.get("delivery_id"))
                    for item in self.records_for_report(report_id, trade_date=trade_date)
                }
                for record in self.ensure_records(report):
                    delivery_id = _text(record.get("delivery_id"))
                    if delivery_id not in before:
                        created_records.append(delivery_id)
                    if self._apply_pending_receipt(delivery_id, now=current):
                        applied_receipts.append(delivery_id)
                    if self._recover_stale_sending(delivery_id, now=current):
                        recovered_sending.append(delivery_id)
        return {
            "created_records": created_records,
            "applied_receipts": applied_receipts,
            "recovered_sending": recovered_sending,
        }

    def _apply_pending_receipt(self, delivery_id: str, *, now: datetime) -> bool:
        """发送成功后、并账前崩溃留下的回执证据：先认它，再谈重试。

        没有这一步，"已发送但状态仍是 sending" 会被当成"从没发过"而重发。
        """
        path = self.receipt_path(delivery_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        record = self.load_record(delivery_id)
        if record is None:
            return False
        if _text(record.get("state")) == STATE_DELIVERED:
            _safe_unlink(path)
            return False
        outcome_text = _text(payload.get("outcome"))
        if not outcome_text:
            _safe_unlink(path)
            return False
        updated = dict(record)
        self._apply_outcome(
            updated,
            outcome=outcome_text,
            message_id=_text(payload.get("message_id")),
            error_code=_text(payload.get("error_code")),
            error_message=_text(payload.get("error_message")),
            accepted_at=_text(payload.get("accepted_at")),
            retryable=bool(payload.get("retryable", False)),
            now=now,
            target_idempotent=bool(record.get("idempotent", False)),
            target_required=bool(record.get("required", False)),
        )
        self.save_record(updated)
        _safe_unlink(path)
        return True

    def _recover_stale_sending(self, delivery_id: str, *, now: datetime) -> bool:
        record = self.load_record(delivery_id)
        if record is None or _text(record.get("state")) != STATE_SENDING:
            return False
        lease_until = _parse_datetime(record.get("lease_until"))
        if lease_until is not None and lease_until > now:
            return False
        lock = DistributedFileLock(self.lock_path(delivery_id), stale_after_sec=self._lease_sec())
        if lock.is_held():
            return False
        updated = dict(record)
        # 持有者进程消失：结果**不确定**，不能当成"从未发送"。按不确定结果恢复，
        # 于是走"同一 uuid 在去重窗内重试、超窗停手"的固定路径。
        self._apply_outcome(
            updated,
            outcome=OUTCOME_UNKNOWN,
            message_id="",
            error_code="lease_expired",
            error_message="发送期间进程消失，结果不确定",
            accepted_at="",
            retryable=True,
            now=now,
            target_idempotent=bool(record.get("idempotent", False)),
            target_required=bool(record.get("required", False)),
        )
        self.save_record(updated)
        return True

    # ------------------------------------------------------------------ 主循环

    def tick(
        self, *, now: datetime | None = None, max_targets: int | None = None
    ) -> dict[str, object]:
        """交付检查：修复中断状态 → 处理到期目标 → 必要时补发过程说明。"""
        current = self._now(now)
        budget = max(1, int(max_targets or self.config.max_delivery_targets_per_tick))
        summary: dict[str, object] = {
            "timestamp": current.isoformat(),
            "processed": 0,
            "delivered": 0,
            "failed": 0,
            "unknown": 0,
            "exhausted": 0,
            "skipped": 0,
            "quiet_window": False,
            "targets": [],
            "recovery": {},
        }
        if not self._is_enabled():
            summary["reason"] = "disabled"
            return summary
        quiet = self._quiet(current)
        summary["quiet_window"] = quiet
        summary["recovery"] = self.recover(now=current)
        summary["notices"] = self._maybe_publish_notices(now=current)
        processed_targets: list[dict[str, object]] = []
        for trade_date in self.report_service.recent_trade_dates():
            for report in self.report_service.reports_for(trade_date):
                for record in self.records_for_report(
                    _text(report.get("report_id")), trade_date=trade_date
                ):
                    parked = self._park_expired_uncertain(record, now=current)
                    if parked is not None:
                        self.save_record(parked)
                        summary["unknown"] = _int(summary["unknown"]) + 1
                        processed_targets.append(parked)
                        continue
                    if not self._is_due(record, now=current):
                        continue
                    if quiet:
                        # 静默窗口内不发送，但状态原样保留：窗口结束后的明确未发送
                        # 结果会作为补发走原日期，不会被丢掉。
                        summary["skipped"] = _int(summary["skipped"]) + 1
                        continue
                    if budget <= 0:
                        continue
                    budget -= 1
                    result = self._attempt(record, now=current)
                    summary["processed"] = _int(summary["processed"]) + 1
                    processed_targets.append(result)
                    state = _text(result.get("state"))
                    if state == STATE_DELIVERED:
                        summary["delivered"] = _int(summary["delivered"]) + 1
                    elif state == STATE_EXHAUSTED:
                        summary["exhausted"] = _int(summary["exhausted"]) + 1
                    elif state == STATE_UNKNOWN:
                        summary["unknown"] = _int(summary["unknown"]) + 1
                    elif state == STATE_RETRY_WAIT:
                        summary["failed"] = _int(summary["failed"]) + 1
                    else:
                        summary["skipped"] = _int(summary["skipped"]) + 1
        summary["targets"] = processed_targets
        return summary

    def _attempt(self, record: Mapping[str, object], *, now: datetime) -> dict[str, object]:
        delivery_id = _text(record.get("delivery_id"))
        lock = DistributedFileLock(self.lock_path(delivery_id), stale_after_sec=self._lease_sec())
        if not lock.acquire():
            # 另一个 worker 正在发同一个目标：**不得**接管，否则就是重复推送。
            return {**dict(record), "state": _text(record.get("state")), "skipped": "locked"}
        try:
            fresh = self.load_record(delivery_id) or dict(record)
            state = _text(fresh.get("state"))
            if state in {STATE_DELIVERED, STATE_EXHAUSTED}:
                return {**fresh, "skipped": f"already_{state}"}
            target = self._target_for(_text(fresh.get("target_key")))
            if target is None:
                return {**fresh, "skipped": "target_unconfigured"}
            attempt_no = _int(fresh.get("attempts")) + 1
            sending = dict(fresh)
            sending.update(
                {
                    "state": STATE_SENDING,
                    "attempts": attempt_no,
                    "first_attempt_at": _text(fresh.get("first_attempt_at")) or now.isoformat(),
                    "last_attempt_at": now.isoformat(),
                    "lease_owner": lock.owner_token,
                    "lease_until": (now + timedelta(seconds=self._lease_sec())).isoformat(),
                    "updated_at": now.isoformat(),
                }
            )
            self.save_record(sending)
            message = NotificationMessage(
                title=_text(sending.get("title")),
                content=_text(sending.get("content")),
                level="info",
                trace_id=_text(sending.get("report_id")),
            )
            outcome = self._send_with_budget(
                target, message, request_uuid=_text(sending.get("request_uuid"))
            )
            # 先把回执落成独立证据：万一下一步并账失败，恢复也能据它认账。
            _write_json_atomic(
                self.receipt_path(delivery_id),
                {
                    "delivery_id": delivery_id,
                    "outcome": outcome.outcome,
                    "message_id": outcome.message_id,
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message,
                    "accepted_at": outcome.accepted_at,
                    "retryable": outcome.retryable,
                    "written_at": datetime.now().isoformat(),
                },
            )
            updated = dict(sending)
            self._apply_outcome(
                updated,
                outcome=outcome.outcome,
                message_id=outcome.message_id,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
                accepted_at=outcome.accepted_at,
                retryable=outcome.retryable,
                now=now,
                target_idempotent=target.idempotent,
                target_required=target.required,
            )
            raw_history = updated.get("history")
            history: list[dict[str, object]] = (
                [dict(item) for item in raw_history if isinstance(item, Mapping)]
                if isinstance(raw_history, Sequence) and not isinstance(raw_history, (str, bytes))
                else []
            )
            history.append(
                {
                    "at": now.isoformat(),
                    "attempt": attempt_no,
                    "outcome": outcome.outcome,
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message[:200],
                }
            )
            updated["history"] = history[-max(1, int(self.config.attempt_history_limit)) :]
            updated["lease_owner"] = ""
            updated["lease_until"] = ""
            self.save_record(updated)
            _safe_unlink(self.receipt_path(delivery_id))
            return updated
        finally:
            lock.release()

    def _send_with_budget(
        self,
        target: DeliveryTarget,
        message: NotificationMessage,
        *,
        request_uuid: str,
    ) -> Any:
        """在总预算内完成一次投递；超预算按"不确定"返回，不当作失败。

        urlopen 的 timeout 是 socket 级超时，覆盖不到 DNS 解析；这里再加一层线程级
        兜底。超预算时被放弃的线程可能仍在后台把消息发出去，所以结果必须是
        ``unknown``（可用同一 uuid 重试）而不是 ``failed``（会被当作没发过）。
        """
        budget = max(1.0, float(self.config.request_timeout_sec))
        result: dict[str, object] = {}

        def _worker() -> None:
            try:
                result["outcome"] = send_explicit(
                    target.notifier,
                    message,
                    request_uuid=request_uuid,
                    target_key=target.key,
                )
            except Exception as exc:  # noqa: BLE001 - send_explicit 本不该抛，兜底
                result["error"] = f"{exc.__class__.__name__}: {exc}"[:200]

        thread = threading.Thread(
            target=_worker, name=f"nightly-delivery-{target.key}", daemon=True
        )
        thread.start()
        thread.join(timeout=budget + _BUDGET_BACKSTOP_MARGIN_SEC)
        if "outcome" in result:
            return result["outcome"]
        return TargetDeliveryOutcome(
            target_key=target.key,
            outcome=OUTCOME_UNKNOWN,
            error_code="call_budget_exceeded",
            error_message=(
                str(result.get("error") or "") or f"单次投递超过 {budget:.0f}s 预算，结果不确定"
            )[:200],
            retryable=True,
        )

    def _apply_outcome(
        self,
        record: dict[str, object],
        *,
        outcome: str,
        message_id: str,
        error_code: str,
        error_message: str,
        accepted_at: str,
        retryable: bool,
        now: datetime,
        target_idempotent: bool,
        target_required: bool,
    ) -> None:
        """把一次投递结果折算成记录状态（重试预算与不确定窗口都在这里收口）。"""
        attempts = _int(record.get("attempts"))
        max_attempts = max(
            1, _int(record.get("max_attempts")) or int(self.config.max_delivery_attempts)
        )
        record["last_attempt_at"] = now.isoformat()
        # 记住"上一次结果是什么性质"：unknown 引发的 retry_wait 和 failed 引发的
        # retry_wait 后续规则不同（前者还要受去重窗口约束），只看 state 分不清。
        record["last_outcome"] = outcome
        record["error_code"] = error_code
        record["error_message"] = error_message
        record["updated_at"] = now.isoformat()
        if outcome == OUTCOME_ACCEPTED:
            record.update(
                {
                    "state": STATE_DELIVERED,
                    "accepted_at": accepted_at or now.isoformat(),
                    "message_id": message_id,
                    "next_retry_at": "",
                    "needs_attention": False,
                }
            )
            return
        if outcome == OUTCOME_UNKNOWN:
            if self._can_retry_unknown(
                record,
                now=now,
                attempts=attempts,
                max_attempts=max_attempts,
                target_idempotent=target_idempotent,
            ):
                record.update(
                    {
                        "state": STATE_RETRY_WAIT,
                        "next_retry_at": self._next_retry_at(attempts, now=now),
                        "needs_attention": False,
                    }
                )
                return
            # 没有幂等能力或已超过去重窗口：停手，保留 unknown 交人工，绝不盲目重发。
            record.update(
                {
                    "state": STATE_UNKNOWN,
                    "next_retry_at": "",
                    "needs_attention": True,
                }
            )
            return
        # 明确失败：可重试的按退避重试；不可重试的（认证/权限/目标/参数）直接记为
        # 需处理状态，不做高频重试。
        if retryable and attempts < max_attempts:
            record.update(
                {
                    "state": STATE_RETRY_WAIT,
                    "next_retry_at": self._next_retry_at(attempts, now=now),
                    "needs_attention": False,
                }
            )
            return
        record.update(
            {
                "state": STATE_EXHAUSTED,
                "next_retry_at": "",
                "needs_attention": not target_required or attempts >= max_attempts,
            }
        )

    def _can_retry_unknown(
        self,
        record: Mapping[str, object],
        *,
        now: datetime,
        attempts: int,
        max_attempts: int,
        target_idempotent: bool,
    ) -> bool:
        if not target_idempotent:
            return False
        if attempts >= max_attempts:
            return False
        return self._within_unknown_window(record, now=now)

    def _within_unknown_window(self, record: Mapping[str, object], *, now: datetime) -> bool:
        first = _parse_datetime(record.get("first_attempt_at"))
        if first is None:
            # 还没尝试过（pending/人工补发）：没有"不确定"这回事。
            return True
        window = max(0, int(self.config.unknown_retry_window_sec))
        return (now - first).total_seconds() <= window

    def _next_retry_at(self, attempts: int, *, now: datetime) -> str:
        delays = [max(1, int(item)) for item in self.config.retry_delays_sec]
        index = max(0, min(attempts - 1, len(delays) - 1))
        return (now + timedelta(seconds=delays[index])).isoformat()

    def _park_expired_uncertain(
        self,
        record: Mapping[str, object],
        *,
        now: datetime,
    ) -> dict[str, object] | None:
        """把"超出重试窗口仍不确定"的记录停手并标成 unknown（需人工核对）。

        没有这一步，unknown 引发的 retry_wait 会在窗口过后仍被当成普通待发记录
        继续发送——那正好是"绕过幂等窗口盲目补发"。
        """
        if _text(record.get("last_outcome")) != OUTCOME_UNKNOWN:
            return None
        state = _text(record.get("state"))
        if state in {STATE_DELIVERED, STATE_EXHAUSTED}:
            return None
        if state == STATE_UNKNOWN and bool(record.get("needs_attention", False)):
            return None
        attempts = _int(record.get("attempts"))
        max_attempts = max(
            1, _int(record.get("max_attempts")) or int(self.config.max_delivery_attempts)
        )
        retryable = (
            bool(record.get("idempotent", False))
            and attempts < max_attempts
            and self._within_unknown_window(record, now=now)
        )
        if retryable:
            return None
        updated = dict(record)
        updated.update(
            {
                "state": STATE_UNKNOWN,
                "next_retry_at": "",
                "needs_attention": True,
                "updated_at": now.isoformat(),
            }
        )
        return updated

    def _is_due(self, record: Mapping[str, object], *, now: datetime) -> bool:
        state = _text(record.get("state"))
        if state in {STATE_DELIVERED, STATE_EXHAUSTED, STATE_SENDING}:
            return False
        if state not in {STATE_PENDING, STATE_RETRY_WAIT, STATE_UNKNOWN}:
            return False
        if state == STATE_UNKNOWN:
            # unknown 只在仍有幂等能力且还在去重窗口内时才继续尝试。
            if not bool(record.get("idempotent", False)):
                return False
            if _int(record.get("attempts")) >= max(1, _int(record.get("max_attempts"))):
                return False
            return self._within_unknown_window(record, now=now)
        if _text(record.get("last_outcome")) == OUTCOME_UNKNOWN and not self._within_unknown_window(
            record, now=now
        ):
            # 双保险：不确定结果出了去重窗口一律不再自动发（正常路径里
            # _park_expired_uncertain 已把这种记录停下并标成 unknown）。
            return False
        next_retry = _parse_datetime(record.get("next_retry_at"))
        return next_retry is None or next_retry <= now

    # ------------------------------------------------------------ 过程说明

    def _maybe_publish_notices(self, *, now: datetime) -> list[dict[str, object]]:
        """22:30 延迟说明 / 23:30 未完成说明：各最多一次，且不挡住正式结果。

        与正式结果使用不同的 report_id 与指针，因此"延迟提醒先发出去"不会导致
        晚到的正式报告被去重掉。
        """
        trade_date = now.date().isoformat()
        if self.report_service.published_report(trade_date) is not None:
            # 已经有正式结果（含 blocked/failed）——用户已经拿到明确结论，
            # 再补一条过程说明只会变成噪声。
            return []
        state = self.report_service.read_date_state(trade_date)
        notices = _mapping(state.get("notices"))
        published: list[dict[str, object]] = []
        # 截止说明优先判定：两条规则同时到期（例如 22:30—23:30 之间进程不在）
        # 时，"截止前仍未形成有效结果"已经完整覆盖"还没完成"，再补一条延迟说明
        # 会变成一分钟内两条内容几乎相同的推送。这里把延迟说明标记为已被吸收，
        # 既不发它、也不会在之后又突然冒出来。
        for notice, hhmm in (
            (NOTICE_DEADLINE, self.config.deadline_time),
            (NOTICE_DELAY, self.config.target_time),
        ):
            if _text(notices.get(notice)):
                continue
            if notice == NOTICE_DELAY and _text(notices.get(NOTICE_DEADLINE)):
                notices[NOTICE_DELAY] = "superseded_by_deadline"
                self.report_service.update_date_state(trade_date, {"notices": notices})
                continue
            if not _at_or_after(now, hhmm):
                continue
            reason = "重型扫描尚未完成" if notice == NOTICE_DELAY else "截止前仍未形成有效结果"
            report = self.report_service.build_notice(
                trade_date=trade_date,
                generated_at=now,
                notice=notice,
                scan_status=SCAN_STATUS_BLOCKED,
                reason=reason,
                date_state=state,
            )
            result = self.report_service.publish(report)
            self.ensure_records(_mapping(result.get("report")))
            published.append({"notice": notice, "report_id": result["report_id"]})
            state = self.report_service.read_date_state(trade_date)
            notices = _mapping(state.get("notices"))
        return published

    # ------------------------------------------------------------------ 工具

    def _is_enabled(self) -> bool:
        return bool(self.config.enabled)

    def _lease_sec(self) -> int:
        return max(
            _LEASE_MIN_SEC,
            int(self.config.request_timeout_sec) * 4,
        )

    def _quiet(self, now: datetime) -> bool:
        windows = list(getattr(self._service._config.notification_filter, "quiet_windows", []))
        if not windows:
            return False
        try:
            return bool(is_quiet_time(windows, now=now))
        except Exception:  # noqa: BLE001 - 静默窗口解析异常不能挡住交付
            return False

    def _now(self, now: datetime | None) -> datetime:
        zone: tzinfo
        try:
            zone = ZoneInfo(str(getattr(self._service._config.app, "timezone", "Asia/Shanghai")))
        except Exception:  # noqa: BLE001
            zone = UTC
        if now is None:
            return datetime.now(zone)
        return now.astimezone(zone) if now.tzinfo is not None else now.replace(tzinfo=zone)


def _at_or_after(now: datetime, hhmm: str) -> bool:
    normalized = _text(hhmm)
    if not normalized:
        return False
    try:
        hours, minutes = normalized.split(":", maxsplit=1)
        target = now.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)
    except (TypeError, ValueError):
        return False
    return now >= target


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _parse_datetime(value: object) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, ensure_ascii=False, separators=(",", ":"), default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
