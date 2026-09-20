"""独立验收 R1—R4 反例的在线复现脚本（2026-09-17）。

用途：在**部署镜像**上、用隔离的临时目录复现独立验收报告提出的四条并发/恢复反例，
确认修复生效。它不写生产 artifacts、不发任何消息、不触网（发送器是假对象）。

为什么保留：这四条都是"看代码看不出来、只有真跑才暴露"的并发/恢复缺陷，以后每次
改动锁或恢复逻辑都应当重跑一遍，而不是重新写一遍探针。

在容器内运行::

    docker exec -i stock-analyzer-api python - < scripts/nightly_review_probe_20260917.py

期望输出（全部为真／符合预期）::

    R1_waited_for_holder / R1_merges_both / R1_busy_raises = true
    R1_busy_wrote_through = false
    R2_requeue_blocked / R2_state_untouched / R2_recovery_blocked / R2_sending_untouched = true
    R3_adopted = ["nr-<日期>-01"], R3_records_created >= 1, R3_delivered = "delivered"
    R4_reason = "notifications_disabled", R4_no_send / R4_stays_pending / R4_resumes = true
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from stock_analyzer.config import NightlyReportConfig
from stock_analyzer.notify.channels import NotificationMessage, TargetDeliveryOutcome
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.runtime.services import nightly_delivery_service as dm
from stock_analyzer.runtime.services.nightly_delivery_service import (
    STATE_SENDING,
    NightlyDeliveryService,
)
from stock_analyzer.runtime.services.nightly_report_service import (
    DateStateBusyError,
    NightlyReportService,
)

TD = "2026-09-17"
NOW = datetime.fromisoformat("2026-09-17T21:50:00+08:00")
out: dict[str, object] = {}


class _App:
    timezone = "Asia/Shanghai"


class _Notifications:
    primary = "feishu_app"
    backup = "console"
    feishu_enterprise_enabled = False
    enabled = True


class _Filter:
    quiet_windows: list[str] = []


class _Cfg:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.nightly = NightlyReportConfig(
            enabled=enabled,
            reports_root=str(root / "reports"),
            delivery_root=str(root / "delivery"),
        )
        self.notifications = _Notifications()
        self.notification_filter = _Filter()
        self.app = _App()


class _Svc:
    def __init__(self, cfg: _Cfg) -> None:
        self._config = cfg

    def _resolve_evolution_path(self, raw: str) -> str:
        return raw


class _Sender:
    """假发送器：记录调用，不触网。"""

    channel = "feishu_app"
    timeout_sec = 5
    script: list[str] = []

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.default = "accepted"

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "",
    ) -> TargetDeliveryOutcome:
        self.calls.append(target_key)
        kind = self.script.pop(0) if self.script else self.default
        if kind == "accepted":
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome="accepted",
                message_id="om_probe",
                accepted_at=NOW.isoformat(),
            )
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome="unknown",
            error_code="request_exception",
            error_message="probe",
            retryable=True,
        )


def _row(symbol: str = "600000") -> dict[str, object]:
    return {
        "symbol": symbol,
        "score": 72.0,
        "shortlist_reasons": ["signal_strength"],
        "reasons": ["high_score"],
        "overextension": {
            "level": "none",
            "reject_new_buy": False,
            "evaluation_status": "evaluated",
            "missing_inputs": [],
            "reasons": [],
            "metrics": {},
        },
        "board_risk": {"reject_new_buy": False, "reasons": []},
    }


def _night_scan(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": "ok" if rows else "empty",
        "trace_id": "t",
        "night_pool": rows,
        "overnight_top5": rows[:5],
        "candidate_data_gate": {"status": "ok", "reasons": []},
        "fallback": {"applied": False, "reason": ""},
        "readiness": {"status": "ready", "allowed": True},
        "source_report": {
            "data_snapshot_id": TD,
            "prefilter": {"universe_count": 100, "eligible_count": 90},
            "funnel": {
                "light_count": 20,
                "deep_count": 10,
                "final_count": 0,
                "final_selection": {"selected_count": 0, "rejected": []},
            },
        },
    }


def _build(rs: NightlyReportService, rows: list[dict[str, object]]) -> dict[str, object]:
    return rs.build_formal_report(
        night_scan=_night_scan(rows),
        trade_date=TD,
        generated_at=NOW,
        run_id="probe",
        data_snapshot_id=TD,
        name_resolver=lambda _s: "",
    )


def _env(root: Path) -> tuple[NightlyReportService, NightlyDeliveryService, _Sender, _Svc]:
    cfg = _Cfg(root)
    svc = _Svc(cfg)
    sender = _Sender()
    dm.build_channel = lambda *, config, channel_name: sender  # type: ignore[assignment]
    dm._force_console_notifier = lambda: False  # type: ignore[assignment]
    rs = NightlyReportService(svc)
    return rs, NightlyDeliveryService(svc), sender, svc


# --- R1：持锁时应等待/失败，不得穿透写 ---------------------------------------
root = Path(tempfile.mkdtemp(prefix="probe_r1_"))
try:
    rs, _, _, _ = _env(root)
    rs.update_date_state(TD, {"seed": 1})
    holder = rs._date_lock(TD)  # noqa: SLF001
    holder.acquire()
    released = []

    def _release() -> None:
        time.sleep(1.0)
        holder.release()
        released.append(1)

    thread = threading.Thread(target=_release, daemon=True)
    thread.start()
    rs._date_lock_timeout_sec = 10.0  # noqa: SLF001
    rs.update_date_state(TD, {"late": 1})
    out["R1_waited_for_holder"] = bool(released)
    state = rs.read_date_state(TD)
    out["R1_merges_both"] = state.get("seed") == 1 and state.get("late") == 1
    thread.join(timeout=5)

    holder2 = rs._date_lock(TD)  # noqa: SLF001
    holder2.acquire()
    rs._date_lock_timeout_sec = 0.2  # noqa: SLF001
    try:
        rs.update_date_state(TD, {"should_not_land": 1})
        out["R1_busy_raises"] = False
    except DateStateBusyError:
        out["R1_busy_raises"] = True
    finally:
        holder2.release()
    out["R1_busy_wrote_through"] = "should_not_land" in rs.read_date_state(TD)
finally:
    shutil.rmtree(root, ignore_errors=True)

# --- R2：补发/恢复不得改写在途记录 -------------------------------------------
root = Path(tempfile.mkdtemp(prefix="probe_r2_"))
try:
    rs, dv, sender, _ = _env(root)
    report = rs.publish(_build(rs, [_row()]))["report"]
    records = dv.ensure_records(report)
    did = records[0]["delivery_id"]

    mid = dict(records[0])
    mid.update({"state": "retry_wait", "attempts": 1})
    dv.save_record(mid)
    holder = DistributedFileLock(dv.lock_path(did), stale_after_sec=120)
    holder.acquire()
    try:
        res = dv.request_retry(report["report_id"], now=NOW)
        out["R2_requeue_blocked"] = res["queued"] is False and res["reason"] == "in_progress"
        out["R2_state_untouched"] = dv.load_record(did)["state"] == "retry_wait"
    finally:
        holder.release()

    expired = dict(records[0])
    expired.update(
        {
            "state": STATE_SENDING,
            "attempts": 1,
            "first_attempt_at": NOW.isoformat(),
            "lease_until": (NOW - timedelta(seconds=1)).isoformat(),
        }
    )
    dv.save_record(expired)
    holder2 = DistributedFileLock(dv.lock_path(did), stale_after_sec=120)
    holder2.acquire()
    try:
        rec = dv.recover(now=NOW)
        out["R2_recovery_blocked"] = rec["recovered_sending"] == []
        out["R2_sending_untouched"] = dv.load_record(did)["state"] == STATE_SENDING
    finally:
        holder2.release()
finally:
    shutil.rmtree(root, ignore_errors=True)

# --- R3：冻结后未写指针也能被恢复采用 ----------------------------------------
root = Path(tempfile.mkdtemp(prefix="probe_r3_"))
try:
    rs, dv, sender, _ = _env(root)
    report = _build(rs, [_row()])
    report["revision"] = 1
    report["report_id"] = "nr-20260917-01"
    rs.freeze_report(report)
    out["R3_pointer_before"] = rs.read_date_state(TD).get("published_report_id") or ""
    rec = dv.recover(now=NOW)
    out["R3_adopted"] = rec["adopted_reports"]
    out["R3_records_created"] = len(rec["created_records"])
    dv.tick(now=NOW)
    out["R3_delivered"] = dv.status("nr-20260917-01")["delivery_status"]
finally:
    shutil.rmtree(root, ignore_errors=True)

# --- R4：全局停发开关 --------------------------------------------------------
root = Path(tempfile.mkdtemp(prefix="probe_r4_"))
try:
    rs, dv, sender, svc = _env(root)
    report = rs.publish(_build(rs, [_row()]))["report"]
    dv.ensure_records(report)
    svc._config.notifications.enabled = False
    summary = dv.tick(now=NOW)
    out["R4_reason"] = summary.get("reason")
    out["R4_no_send"] = sender.calls == []
    out["R4_stays_pending"] = dv.status(report["report_id"])["delivery_status"] == "pending"
    svc._config.notifications.enabled = True
    dv.tick(now=NOW + timedelta(minutes=1))
    out["R4_resumes"] = len(sender.calls) == 1
finally:
    shutil.rmtree(root, ignore_errors=True)

print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
