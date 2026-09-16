"""正式晚报的逐目标交付：判定、多目标、幂等、恢复、并发、重试。

对应 2026-09-16 v2 方案 §3.2—§3.5 与 §4.2 的交付判定/多目标/幂等/恢复/并发/
重试/日期七组场景。

全部使用隔离状态目录与假 notifier：**不向真实飞书发送任何消息**。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.config import NightlyReportConfig
from stock_analyzer.notify.channels import (
    ConsoleNotifier,
    FeishuAppNotifier,
    NotificationMessage,
    TargetDeliveryOutcome,
)
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.runtime.services import nightly_delivery_service as delivery_module
from stock_analyzer.runtime.services.nightly_delivery_service import (
    STATE_DELIVERED,
    STATE_EXHAUSTED,
    STATE_PENDING,
    STATE_RETRY_WAIT,
    STATE_SENDING,
    STATE_UNKNOWN,
    NightlyDeliveryService,
)
from stock_analyzer.runtime.services.nightly_report_service import (
    NOTICE_DEADLINE,
    NOTICE_DELAY,
    SCAN_STATUS_COMPLETED,
    SCAN_STATUS_EMPTY,
    NightlyReportService,
)

_TRADE_DATE = "2026-09-16"
_NOW = datetime.fromisoformat("2026-09-16T21:50:00+08:00")


# --------------------------------------------------------------------- 测试替身


class _App:
    timezone = "Asia/Shanghai"


class _Notifications:
    def __init__(self, *, primary: str = "feishu_app", enterprise: bool = False) -> None:
        self.primary = primary
        self.feishu_enterprise_enabled = enterprise


class _NotificationFilter:
    quiet_windows: list[str] = []


class _Config:
    def __init__(
        self, root: Path, *, primary: str = "feishu_app", enterprise: bool = False
    ) -> None:
        self.nightly = NightlyReportConfig(
            enabled=True,
            reports_root=str(root / "nightly_reports"),
            delivery_root=str(root / "nightly_delivery"),
        )
        self.notifications = _Notifications(primary=primary, enterprise=enterprise)
        self.notification_filter = _NotificationFilter()
        self.app = _App()


def _init_scripted(
    target: object,
    channel: str,
    script: list[object] | None,
    *,
    default: str = "accepted",
) -> None:
    target.channel = channel  # type: ignore[attr-defined]
    target.timeout_sec = 5  # type: ignore[attr-defined]
    target.script = list(script or [])  # type: ignore[attr-defined]
    target.calls = []  # type: ignore[attr-defined]
    # script 用尽后的兜底结果：需要"一直失败"的场景必须显式传 default，
    # 否则重置成 accepted 会把重试预算的测试变成一次成功。
    target.default = default  # type: ignore[attr-defined]
    target.started = threading.Event()  # type: ignore[attr-defined]
    target.release = None  # type: ignore[attr-defined]


def _scripted_send(
    target: object,
    message: NotificationMessage,
    *,
    request_uuid: str,
    target_key: str,
) -> TargetDeliveryOutcome:
    """按 script 逐次返回结果，并记录每次实际发出的正文。"""
    target.calls.append(  # type: ignore[attr-defined]
        {
            "uuid": request_uuid,
            "target_key": target_key,
            "title": message.title,
            "content": message.content,
        }
    )
    target.started.set()  # type: ignore[attr-defined]
    release = target.release  # type: ignore[attr-defined]
    if release is not None:
        release.wait(timeout=10)
    script = target.script  # type: ignore[attr-defined]
    step = script.pop(0) if script else target.default  # type: ignore[attr-defined]
    if isinstance(step, Exception):
        raise step
    return _outcome(target_key or target.channel, str(step))  # type: ignore[attr-defined]


class _FakeNotifier:
    """通用假目标：**非幂等**，对应企业分发这类没有 uuid 能力的渠道。"""

    def __init__(
        self,
        channel: str,
        script: list[object] | None = None,
        *,
        default: str = "accepted",
    ) -> None:
        _init_scripted(self, channel, script, default=default)

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "",
    ) -> TargetDeliveryOutcome:
        return _scripted_send(self, message, request_uuid=request_uuid, target_key=target_key)


class _FakeAppNotifier(FeishuAppNotifier):
    """飞书应用目标替身：**是** FeishuAppNotifier 的实例，因此被判定为有幂等能力。

    这一点很重要——"不确定结果可以自动重试"整个前提就建立在主目标支持同 uuid
    幂等之上；用普通假对象测就会把这个前提悄悄测没了。
    """

    def __init__(self, script: list[object] | None = None, *, default: str = "accepted") -> None:
        FeishuAppNotifier.__init__(self, app_id="cli_x", app_secret="sec", receive_id="ou_x")
        _init_scripted(self, "feishu_app", script, default=default)

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "",
    ) -> TargetDeliveryOutcome:
        return _scripted_send(self, message, request_uuid=request_uuid, target_key=target_key)


def _fake_app(
    script: list[object] | None = None,
    *,
    default: str = "accepted",
) -> _FakeAppNotifier:
    return _FakeAppNotifier(script, default=default)


def _outcome(target_key: str, kind: str) -> TargetDeliveryOutcome:
    if kind == "accepted":
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome="accepted",
            message_id="om_test_1",
            accepted_at=_NOW.isoformat(),
        )
    if kind == "failed_retryable":
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome="failed",
            error_code="http_503",
            error_message="服务端临时错误",
            retryable=True,
        )
    if kind == "failed_permanent":
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome="failed",
            error_code="99991663",
            error_message="app_id 或 app_secret 错误",
            retryable=False,
        )
    if kind == "console":
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome="failed",
            error_code="console_not_a_delivery",
            error_message="console 输出不能作为送达依据",
        )
    return TargetDeliveryOutcome(
        target_key=target_key,
        outcome="unknown",
        error_code="request_exception",
        error_message="读超时",
        retryable=True,
    )


class _FakeService:
    def __init__(self, config: _Config) -> None:
        self._config = config

    def _resolve_evolution_path(self, raw: str) -> str:
        return raw


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """返回 (service, notifier 注册表, 按目标名安装假 notifier 的函数)。"""
    config = _Config(tmp_path)
    service = _FakeService(config)
    notifiers: dict[str, _FakeNotifier] = {}

    def _install(channel_name: str, notifier: object) -> None:
        notifiers[channel_name] = notifier  # type: ignore[assignment]

    def _fake_build_channel(*, config: object, channel_name: str) -> object:  # noqa: ARG001
        return notifiers.get(channel_name) or _FakeNotifier(channel_name)

    monkeypatch.setattr(delivery_module, "build_channel", _fake_build_channel)
    yield service, notifiers, _install


def _report_row(symbol: str = "600000", *, score: float = 72.0) -> dict[str, object]:
    return {
        "symbol": symbol,
        "score": score,
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


def _publish(
    report_service: NightlyReportService,
    *,
    rows: list[dict[str, object]] | None = None,
    trade_date: str = _TRADE_DATE,
) -> dict[str, object]:
    night_scan = {
        "status": "ok" if rows else "empty",
        "trace_id": "t",
        "night_pool": rows or [],
        "overnight_top5": (rows or [])[:5],
        "candidate_data_gate": {"status": "ok", "reasons": []},
        "fallback": {"applied": False, "reason": ""},
        "readiness": {"status": "ready", "allowed": True},
        "source_report": {
            "data_snapshot_id": trade_date,
            "prefilter": {"universe_count": 100, "eligible_count": 90},
            "funnel": {
                "light_count": 20,
                "deep_count": 10,
                "final_count": 0,
                "final_selection": {"selected_count": 0, "rejected": []},
            },
        },
    }
    report = report_service.build_formal_report(
        night_scan=night_scan,
        trade_date=trade_date,
        generated_at=datetime.fromisoformat(f"{trade_date}T21:45:04+08:00"),
        run_id="run-1",
        data_snapshot_id=trade_date,
        name_resolver=lambda _symbol: "",
    )
    return report_service.publish(report)["report"]


def _delivery(service: _FakeService) -> NightlyDeliveryService:
    return NightlyDeliveryService(service)


# --------------------------------------------------------------------- 交付判定


def test_accepted_outcome_marks_delivered_with_message_id(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    install("feishu_app", _fake_app())
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    summary = delivery.tick(now=_NOW)
    assert summary["delivered"] == 1

    status = delivery.status(report["report_id"])
    assert status["delivery_status"] == "delivered"
    assert status["required_target_delivered"] is True
    assert status["targets"][0]["message_id"] == "om_test_1"
    assert status["targets"][0]["state"] == STATE_DELIVERED


def test_console_only_target_never_counts_as_delivered(env) -> None:  # type: ignore[no-untyped-def]
    """console 的 success 只证明"日志写出去了"——必须判未送达。"""
    service, _, install = env
    install("console", ConsoleNotifier())
    service._config.notifications.primary = "console"
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    status = delivery.status(report["report_id"])
    assert status["required_target_delivered"] is False
    assert status["targets"][0]["error_code"] == "console_not_a_delivery"
    assert status["delivery_status"] == STATE_EXHAUSTED


def test_required_target_success_with_optional_failure_is_partial(env) -> None:  # type: ignore[no-untyped-def]
    """主目标成功即可判交付成功；可选目标失败独立保留状态，不重发主目标。"""
    service, _, install = env
    primary = _fake_app()
    optional = _FakeNotifier("feishu_enterprise", script=["failed_retryable"])
    install("feishu_app", primary)
    install("feishu_enterprise", optional)
    service._config.notifications.feishu_enterprise_enabled = True
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW, max_targets=2)
    status = delivery.status(report["report_id"])
    assert status["required_target_delivered"] is True
    assert status["delivery_status"] == "partial"
    optional_state = next(
        item for item in status["targets"] if item["target_key"] == "feishu_enterprise"
    )
    assert optional_state["state"] == STATE_RETRY_WAIT
    assert optional_state["required"] is False
    # 主目标只发了一次：可选失败绝不触发重发已成功的主目标
    assert len(primary.calls) == 1


def test_required_failure_with_optional_success_is_not_delivered(env) -> None:  # type: ignore[no-untyped-def]
    """不能因为字符串里出现 feishu_app+feishu_enterprise 就推断两路都成功。"""
    service, _, install = env
    install("feishu_app", _fake_app(["failed_permanent"]))
    install("feishu_enterprise", _FakeNotifier("feishu_enterprise"))
    service._config.notifications.feishu_enterprise_enabled = True
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW, max_targets=2)
    status = delivery.status(report["report_id"])
    assert status["required_target_delivered"] is False
    assert status["delivery_status"] == STATE_EXHAUSTED
    assert status["last_delivery_error"]


# ------------------------------------------------------------------- 正文冻结


def test_frozen_body_is_used_even_if_report_file_later_changes(env) -> None:  # type: ignore[no-untyped-def]
    """发送/补发不得重新读取可能已被改写的状态来拼接正文。

    这里直接篡改磁盘上的报告文件（模拟"通用 latest/候选状态被进化复扫或盘中雷达
    改写"），交付记录里的正文必须仍是首次尝试前冻结的那一份——否则飞书收到的
    内容与已发出的回执就对不上账。
    """
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row("600000")])
    delivery.ensure_records(report)
    frozen_body = notifier.calls or None
    assert frozen_body is None  # 还没发过

    path = delivery.report_service.report_path(_TRADE_DATE, report["report_id"])
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["observation_candidates"] = [
        {"symbol": "999999", "name": "伪造", "score": 99.0, "reasons": [], "risks": []}
    ]
    tampered["content_digest"] = "tampered"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")

    delivery.tick(now=_NOW)
    assert len(notifier.calls) == 1
    assert "600000" in notifier.calls[0]["content"]
    assert "999999" not in notifier.calls[0]["content"]


def test_revision_creates_new_records_and_keeps_old_body(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    first = _publish(delivery.report_service, rows=[_report_row("600000")])
    delivery.tick(now=_NOW)

    second = _publish(delivery.report_service, rows=[_report_row("600000"), _report_row("000001")])
    assert second["report_id"] != first["report_id"]
    delivery.tick(now=_NOW + timedelta(minutes=1))

    assert len(notifier.calls) == 2
    assert "000001" not in notifier.calls[0]["content"]
    assert "000001" in notifier.calls[1]["content"]


# ------------------------------------------------------------------------- 幂等


def test_second_tick_does_not_resend_a_delivered_target(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    delivery.tick(now=_NOW + timedelta(minutes=1))
    delivery.tick(now=_NOW + timedelta(minutes=2))
    assert len(notifier.calls) == 1
    assert delivery.status(report["report_id"])["delivery_status"] == "delivered"


def test_ensure_records_is_idempotent_and_keeps_uuid(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    install("feishu_app", _fake_app())
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    first = delivery.ensure_records(report)
    second = delivery.ensure_records(report)
    assert first[0]["request_uuid"] == second[0]["request_uuid"]
    assert first[0]["delivery_id"] == second[0]["delivery_id"]


def test_request_retry_requeues_only_unsuccessful_targets(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    primary = _fake_app()
    optional = _FakeNotifier("feishu_enterprise", script=["failed_permanent", "accepted"])
    install("feishu_app", primary)
    install("feishu_enterprise", optional)
    service._config.notifications.feishu_enterprise_enabled = True
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    delivery.tick(now=_NOW, max_targets=2)

    queued = delivery.request_retry(report["report_id"], now=_NOW + timedelta(minutes=5))
    assert queued["queued"] is True
    assert queued["queued_targets"] == ["feishu_enterprise"]
    assert queued["skipped_targets"] == ["feishu_app"]

    delivery.tick(now=_NOW + timedelta(minutes=6), max_targets=2)
    assert len(primary.calls) == 1
    assert len(optional.calls) == 2


def test_request_retry_on_delivered_report_is_a_noop(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    install("feishu_app", _fake_app())
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    delivery.tick(now=_NOW)

    result = delivery.request_retry(report["report_id"], now=_NOW)
    assert result["queued"] is False
    assert result["reason"] == "nothing_to_do"


def test_request_retry_rejects_unknown_report(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    install("feishu_app", _fake_app())
    delivery = _delivery(service)
    assert delivery.request_retry("nr-nope", now=_NOW)["reason"] == "report_not_found"


# ------------------------------------------------------------------------- 重试


def test_retryable_failure_backs_off_then_succeeds(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app(["failed_retryable", "accepted"])
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    status = delivery.status(report["report_id"])
    assert status["targets"][0]["state"] == STATE_RETRY_WAIT
    # 退避为调度时间，不是 worker 内 sleep：到点前不该重试
    delivery.tick(now=_NOW + timedelta(seconds=30))
    assert len(notifier.calls) == 1
    delivery.tick(now=_NOW + timedelta(seconds=61))
    assert len(notifier.calls) == 2
    assert delivery.status(report["report_id"])["delivery_status"] == "delivered"


def test_retry_reuses_the_same_uuid(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app(["failed_retryable", "accepted"])
    install("feishu_app", notifier)
    delivery = _delivery(service)
    _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    delivery.tick(now=_NOW + timedelta(seconds=61))
    assert notifier.calls[0]["uuid"] == notifier.calls[1]["uuid"]
    assert notifier.calls[0]["uuid"]


def test_retry_budget_exhausts_after_four_attempts(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app(["failed_retryable"], default="failed_retryable")
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    offsets = [0, 61, 301, 901, 1801]
    for minutes in offsets:
        delivery.tick(now=_NOW + timedelta(seconds=minutes))
    assert len(notifier.calls) == 4  # 首次 + 3 次重试
    status = delivery.status(report["report_id"])
    assert status["targets"][0]["state"] == STATE_EXHAUSTED
    assert status["targets"][0]["needs_attention"] is True


def test_permanent_failure_is_not_retried_at_high_frequency(env) -> None:  # type: ignore[no-untyped-def]
    """认证/权限/目标类错误重试不会变好，直接记为需处理状态。"""
    service, _, install = env
    notifier = _fake_app(["failed_permanent"], default="failed_permanent")
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    for minutes in (1, 5, 15, 60):
        delivery.tick(now=_NOW + timedelta(minutes=minutes))
    assert len(notifier.calls) == 1
    status = delivery.status(report["report_id"])
    assert status["targets"][0]["state"] == STATE_EXHAUSTED
    assert status["targets"][0]["error_code"] == "99991663"


def test_unknown_within_window_retries_with_the_same_uuid(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app(["unknown", "accepted"])
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    delivery.tick(now=_NOW + timedelta(seconds=61))
    assert len(notifier.calls) == 2
    assert notifier.calls[0]["uuid"] == notifier.calls[1]["uuid"]
    assert delivery.status(report["report_id"])["delivery_status"] == "delivered"


def test_unknown_outside_dedup_window_stops_and_needs_attention(env) -> None:  # type: ignore[no-untyped-def]
    """超出去重窗口仍不确定：停止自动重发，保留 unknown 交人工。"""
    service, _, install = env
    notifier = _fake_app(["unknown"], default="unknown")
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW)
    # 3000s 窗口内会重试；把时间推过窗口后必须停手
    delivery.tick(now=_NOW + timedelta(seconds=61))
    assert len(notifier.calls) == 2
    delivery.tick(now=_NOW + timedelta(seconds=3060))
    assert len(notifier.calls) == 2
    status = delivery.status(report["report_id"])
    assert status["targets"][0]["state"] == STATE_UNKNOWN
    assert status["targets"][0]["needs_attention"] is True


def test_unknown_on_non_idempotent_target_is_not_retried(env) -> None:  # type: ignore[no-untyped-def]
    """可选渠道没有已核实的幂等能力时，不确定结果不自动重试。"""
    service, _, install = env
    install("feishu_app", _fake_app())
    optional = _FakeNotifier("feishu_enterprise", script=["unknown"])
    install("feishu_enterprise", optional)
    service._config.notifications.feishu_enterprise_enabled = True
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    delivery.tick(now=_NOW, max_targets=2)
    delivery.tick(now=_NOW + timedelta(seconds=61), max_targets=2)
    assert len(optional.calls) == 1
    status = delivery.status(report["report_id"])
    optional_state = next(
        item for item in status["targets"] if item["target_key"] == "feishu_enterprise"
    )
    assert optional_state["state"] == STATE_UNKNOWN
    assert optional_state["needs_attention"] is True
    # 必需目标已成功，整体仍判交付成功
    assert status["required_target_delivered"] is True


def test_request_retry_requires_confirmation_outside_dedup_window(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    install("feishu_app", _fake_app(["unknown"], default="unknown"))
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    delivery.tick(now=_NOW)
    delivery.tick(now=_NOW + timedelta(seconds=61))

    later = _NOW + timedelta(seconds=7200)
    blocked = delivery.request_retry(report["report_id"], now=later)
    assert blocked["queued"] is False
    assert blocked["reason"] == "confirm_required"
    assert blocked["conflicts"][0]["reason"] == "unknown_outside_dedup_window"

    confirmed = delivery.request_retry(report["report_id"], confirm_unknown=True, now=later)
    assert confirmed["queued"] is True
    assert (
        delivery.load_record(delivery.delivery_id_for(report["report_id"], "feishu_app"))["state"]
        == STATE_PENDING
    )


# ------------------------------------------------------------------------- 恢复


def test_recovery_creates_records_for_a_published_report(env) -> None:  # type: ignore[no-untyped-def]
    """报告已保存、待发记录尚未建立（写入途中崩溃）必须能被恢复任务补上。"""
    service, _, install = env
    install("feishu_app", _fake_app())
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    recovery = delivery.recover(now=_NOW)
    assert (
        delivery.delivery_id_for(report["report_id"], "feishu_app") in recovery["created_records"]
    )
    assert delivery.status(report["report_id"])["delivery_status"] == STATE_PENDING


def test_receipt_written_after_send_is_acknowledged_on_recovery(env) -> None:  # type: ignore[no-untyped-def]
    """发送成功但回执并账前崩溃：恢复必须认账为已送达，不得重发。"""
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    records = delivery.ensure_records(report)
    delivery_id = records[0]["delivery_id"]

    # 手工构造"已发出、回执已写、记录还停在 sending"的中断现场
    stuck = dict(records[0])
    stuck.update({"state": STATE_SENDING, "attempts": 1, "first_attempt_at": _NOW.isoformat()})
    delivery.save_record(stuck)
    delivery.receipt_path(delivery_id).write_text(
        json.dumps(
            {
                "delivery_id": delivery_id,
                "outcome": "accepted",
                "message_id": "om_recovered",
                "accepted_at": _NOW.isoformat(),
                "retryable": False,
            }
        ),
        encoding="utf-8",
    )

    delivery.recover(now=_NOW + timedelta(minutes=5))
    assert delivery.load_record(delivery_id)["state"] == STATE_DELIVERED
    # 恢复后不再重发
    delivery.tick(now=_NOW + timedelta(minutes=6))
    assert notifier.calls == []


def test_stale_sending_is_recovered_as_unknown_not_as_never_sent(env) -> None:  # type: ignore[no-untyped-def]
    """持锁进程消失：遗留 sending 按**不确定**恢复，不当成从未发送。"""
    service, _, install = env
    notifier = _fake_app(["accepted"])
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    records = delivery.ensure_records(report)
    delivery_id = records[0]["delivery_id"]

    stuck = dict(records[0])
    stuck.update(
        {
            "state": STATE_SENDING,
            "attempts": 1,
            "first_attempt_at": _NOW.isoformat(),
            "lease_until": (_NOW - timedelta(seconds=1)).isoformat(),
        }
    )
    delivery.save_record(stuck)

    recovery = delivery.recover(now=_NOW)
    assert delivery_id in recovery["recovered_sending"]
    recovered = delivery.load_record(delivery_id)
    assert recovered["state"] == STATE_RETRY_WAIT
    assert recovered["error_code"] == "lease_expired"

    # 重试用同一 uuid（幂等键不因恢复而改变）
    delivery.tick(now=_NOW + timedelta(seconds=61))
    assert notifier.calls[0]["uuid"] == records[0]["request_uuid"]


def test_live_holder_is_not_taken_over(env) -> None:  # type: ignore[no-untyped-def]
    """持有者仍存活时其他 worker 不得接管：否则就是重复推送。"""
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    records = delivery.ensure_records(report)
    delivery_id = records[0]["delivery_id"]

    # 记录已到期（pending），但锁被另一个 worker 持有
    holder = DistributedFileLock(delivery.lock_path(delivery_id), stale_after_sec=120)
    assert holder.acquire() is True
    try:
        summary = delivery.tick(now=_NOW)
        assert notifier.calls == []
        assert summary["delivered"] == 0
    finally:
        holder.release()


def test_slow_in_flight_request_blocks_the_second_worker(env) -> None:  # type: ignore[no-untyped-def]
    """持锁慢请求：第二个 worker 必须等，不能并发发出第二条。"""
    service, _, install = env
    notifier = _fake_app()
    notifier.release = threading.Event()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    _publish(delivery.report_service, rows=[_report_row()])

    worker = threading.Thread(target=lambda: delivery.tick(now=_NOW), daemon=True)
    worker.start()
    assert notifier.started.wait(timeout=10) is True
    try:
        second = delivery.tick(now=_NOW)
        assert second["processed"] == 0
        assert len(notifier.calls) == 1
    finally:
        notifier.release.set()
        worker.join(timeout=10)
    assert len(notifier.calls) == 1


# --------------------------------------------------------------------- 静默窗口


def test_quiet_window_defers_delivery_without_losing_it(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    service._config.notification_filter.quiet_windows = ["00:30-08:30"]
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    during_window = datetime.fromisoformat(f"{_TRADE_DATE}T02:00:00+08:00")
    summary = delivery.tick(now=during_window)
    assert summary["quiet_window"] is True
    assert notifier.calls == []

    # 窗口结束后按原日期补发，状态没有被丢掉
    after_window = datetime.fromisoformat("2026-09-17T08:31:00+08:00")
    delivery.tick(now=after_window)
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["title"] == f"【晚间选股报告】{_TRADE_DATE}"
    assert delivery.status(report["report_id"])["delivery_status"] == "delivered"


# ------------------------------------------------------------------- 过程说明


def test_delay_notice_fires_once_and_does_not_block_late_report(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    delivery.report_service.update_date_state(
        _TRADE_DATE,
        {"scan_phase": "scanning", "scan_attempts": 1},
    )

    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T22:31:00+08:00"))
    assert len(notifier.calls) == 1
    assert "延迟说明" in notifier.calls[0]["title"]
    assert "不是选股结果" in notifier.calls[0]["content"]

    # 同一条延迟说明只发一次
    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T22:40:00+08:00"))
    assert len(notifier.calls) == 1

    # 晚到的正式结果仍能发出（独立去重键，不被延迟提醒挡住）
    report = _publish(delivery.report_service, rows=[_report_row()])
    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T22:50:00+08:00"))
    assert len(notifier.calls) == 2
    assert notifier.calls[1]["title"] == f"【晚间选股报告】{_TRADE_DATE}"
    assert delivery.status(report["report_id"])["delivery_status"] == "delivered"


def test_deadline_notice_fires_once_when_nothing_was_produced(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    delivery.report_service.update_date_state(
        _TRADE_DATE,
        {
            "scan_phase": "waiting_data",
            "scan_attempts": 0,
            "waiting_reason": "nightly_data_not_ready",
        },
    )

    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T23:31:00+08:00"))
    assert len(notifier.calls) == 1
    assert "未完成说明" in notifier.calls[0]["title"]
    assert "nightly_data_not_ready" in notifier.calls[0]["content"]

    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T23:45:00+08:00"))
    assert len(notifier.calls) == 1


def test_no_notice_when_a_formal_report_already_exists(env) -> None:  # type: ignore[no-untyped-def]
    """已经拿到明确结论（哪怕是 blocked/failed）就不该再补过程说明。"""
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    _publish(delivery.report_service, rows=[])

    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T23:31:00+08:00"))
    assert len(notifier.calls) == 1
    assert "延迟说明" not in notifier.calls[0]["title"]
    assert "未完成说明" not in notifier.calls[0]["title"]
    assert delivery.report_service.read_date_state(_TRADE_DATE).get("notices", {}) == {}


# ------------------------------------------------------------------------- 开关


def test_tick_is_a_noop_when_disabled(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    service._config.nightly.enabled = False
    delivery = _delivery(service)
    _publish(delivery.report_service, rows=[_report_row()])

    summary = delivery.tick(now=_NOW)
    assert summary["reason"] == "disabled"
    assert notifier.calls == []


# ------------------------------------------------------- 通道层判定（HTTP 语义）


class _FakeResponse:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _app_notifier() -> object:
    from stock_analyzer.notify.channels import FeishuAppNotifier

    notifier = FeishuAppNotifier(app_id="cli_x", app_secret="sec", receive_id="ou_x")
    notifier._tenant_access_token = "t"  # noqa: SLF001 - 跳过取 token 的真实网络调用
    notifier._tenant_access_token_expire_at = 9_999_999_999.0  # noqa: SLF001
    return notifier


def _send_with_response(monkeypatch: pytest.MonkeyPatch, response: object):  # type: ignore[no-untyped-def]
    def _urlopen(*_args: object, **_kwargs: object) -> object:
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("stock_analyzer.notify.channels.request.urlopen", _urlopen)
    return _app_notifier().send_explicit(  # type: ignore[attr-defined]
        NotificationMessage(title="t", content="c"),
        request_uuid="uuid-1",
        target_key="feishu_app",
    )


def test_http_200_with_business_code_zero_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"code": 0, "msg": "success", "data": {"message_id": "om_1"}}).encode()
    outcome = _send_with_response(monkeypatch, _FakeResponse(200, payload))
    assert outcome.outcome == "accepted"
    assert outcome.message_id == "om_1"
    assert outcome.accepted_at


def test_http_200_with_business_error_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """飞书业务失败同样返回 HTTP 200：只看状态码会把失败当成功。"""
    payload = json.dumps({"code": 99991663, "msg": "app not found"}).encode()
    outcome = _send_with_response(monkeypatch, _FakeResponse(200, payload))
    assert outcome.outcome == "failed"
    assert outcome.error_code == "99991663"
    assert outcome.retryable is False


def test_http_200_without_business_status_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 200 且响应无法解析出业务状态：绝不能默认成功。"""
    for payload in (b"not-json", b"{}", b'{"msg":"no code field"}'):
        outcome = _send_with_response(monkeypatch, _FakeResponse(200, payload))
        assert outcome.outcome == "unknown"
        assert outcome.error_code == "unparseable_response"


def test_http_4xx_is_failed_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _send_with_response(monkeypatch, _FakeResponse(403, b""))
    assert outcome.outcome == "failed"
    assert outcome.error_code == "http_403"
    assert outcome.retryable is False


def test_http_5xx_is_retryable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _send_with_response(monkeypatch, _FakeResponse(503, b""))
    assert outcome.outcome == "failed"
    assert outcome.retryable is True


def test_timeout_is_unknown_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时可能已经送达：必须按不确定处理，不能当作"没发出去"直接重发。"""
    outcome = _send_with_response(monkeypatch, TimeoutError("timed out"))
    assert outcome.outcome == "unknown"
    assert outcome.retryable is True


def test_uuid_is_included_in_the_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _urlopen(req: object, **_kwargs: object) -> object:  # noqa: ARG001
        captured["body"] = json.loads(bytes(req.data).decode("utf-8"))  # type: ignore[attr-defined]
        return _FakeResponse(200, json.dumps({"code": 0}).encode())

    monkeypatch.setattr("stock_analyzer.notify.channels.request.urlopen", _urlopen)
    outcome = _app_notifier().send_explicit(  # type: ignore[attr-defined]
        NotificationMessage(title="t", content="c"),
        request_uuid="uuid-stable-1",
        target_key="feishu_app",
    )
    assert outcome.outcome == "accepted"
    assert captured["body"]["uuid"] == "uuid-stable-1"  # type: ignore[index]


def test_empty_candidates_are_also_delivered(env) -> None:  # type: ignore[no-untyped-def]
    """0 只是正常业务结果，同样是必须送达的报告。"""
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[])
    assert report["scan_status"] == SCAN_STATUS_EMPTY

    delivery.tick(now=_NOW)
    assert len(notifier.calls) == 1
    assert "今日正常完成，无合格候选" in notifier.calls[0]["content"]
    assert delivery.status(report["report_id"])["delivery_status"] == "delivered"


def test_status_does_not_leak_receiver_identity_or_credentials(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    service._config.notifications.receive_id = "ou_should_not_leak"
    install("feishu_app", _fake_app())
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    delivery.tick(now=_NOW)

    blob = json.dumps(delivery.status(report["report_id"]), ensure_ascii=False)
    assert "ou_should_not_leak" not in blob
    assert "app_secret" not in blob
    assert "Bearer" not in blob


def test_completed_status_is_preserved_while_delivery_fails(env) -> None:  # type: ignore[no-untyped-def]
    """扫描完成但交付失败：报告状态仍是 completed，交付状态另列。"""
    service, _, install = env
    install("feishu_app", _fake_app(["failed_permanent"], default="failed_permanent"))
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])
    delivery.tick(now=_NOW)

    assert report["scan_status"] == SCAN_STATUS_COMPLETED
    status = delivery.status(report["report_id"])
    assert status["delivery_status"] == STATE_EXHAUSTED
    assert status["required_target_delivered"] is False
    # 报告状态没有被交付失败改写
    assert delivery.report_service.published_report(_TRADE_DATE)["scan_status"] == (
        SCAN_STATUS_COMPLETED
    )


def test_notice_kinds_are_independent(env) -> None:  # type: ignore[no-untyped-def]
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    delivery = _delivery(service)
    delivery.report_service.update_date_state(_TRADE_DATE, {"scan_phase": "pending"})

    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T22:31:00+08:00"))
    delivery.tick(now=datetime.fromisoformat(f"{_TRADE_DATE}T23:31:00+08:00"))
    titles = [call["title"] for call in notifier.calls]
    assert any("延迟说明" in title for title in titles)
    assert any("未完成说明" in title for title in titles)
    assert len({call["uuid"] for call in notifier.calls}) == 2

    state = delivery.report_service.read_date_state(_TRADE_DATE)
    assert NOTICE_DELAY in state["notices"]
    assert NOTICE_DEADLINE in state["notices"]


def test_manual_delivery_works_while_the_automatic_chain_is_disabled(env) -> None:  # type: ignore[no-untyped-def]
    """开关控制"自动链要不要跑"，不控制"能不能手动发一份明确指定的报告"。

    验收回放必须能在开关关闭（即 NAS 尚未启用自动链）时走完真实交付路径，
    否则"先冒烟、后启用"这个顺序根本走不通。
    """
    service, _, install = env
    notifier = _fake_app()
    install("feishu_app", notifier)
    service._config.nightly.enabled = False
    delivery = _delivery(service)
    report = _publish(delivery.report_service, rows=[_report_row()])

    assert delivery.tick(now=_NOW)["reason"] == "disabled"

    summary = delivery.deliver_now(report, now=_NOW)
    assert summary["delivered"] == 1
    assert len(notifier.calls) == 1
    assert delivery.status(report["report_id"])["required_target_delivered"] is True
