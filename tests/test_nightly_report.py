"""晚间正式报告：四类结果、来源一致、版本冻结与消息口径。

对应 2026-09-16 v2 方案 §2 与 §4.2 的报告内容/异常语义/来源一致三组场景。

这些测试**不经过任何真实通知通道**：报告服务不发送，只构造/冻结/渲染。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from stock_analyzer.config import NightlyReportConfig
from stock_analyzer.runtime.services.nightly_report_service import (
    NOTICE_DEADLINE,
    NOTICE_DELAY,
    REPORT_KIND_NOTICE,
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_COMPLETED,
    SCAN_STATUS_EMPTY,
    SCAN_STATUS_FAILED,
    DateStateBusyError,
    NightlyReportService,
)

_TRADE_DATE = "2026-09-16"
_NOW = datetime.fromisoformat("2026-09-16T21:45:04+08:00")


class _FakeService:
    """只实现报告服务真正用到的那几个接口。"""

    def __init__(self, root: Path, *, names: dict[str, str] | None = None) -> None:
        self._config = _config_for(root)
        self._names = dict(names or {})
        self.name_calls: list[str] = []
        self.name_failures: set[str] = set()

    def _resolve_evolution_path(self, raw: str) -> str:
        return raw

    def _resolve_symbol_display_name(self, symbol: str) -> str:
        self.name_calls.append(symbol)
        if symbol in self.name_failures:
            raise RuntimeError("name lookup exploded")
        return self._names.get(symbol, "")


def _config_for(root: Path) -> object:
    class _Cfg:
        nightly = NightlyReportConfig(reports_root=str(root / "nightly_reports"))

    return _Cfg()


@pytest.fixture()
def service(tmp_path: Path) -> _FakeService:
    return _FakeService(
        tmp_path,
        names={"600000": "浦发银行", "000001": "平安银行", "600519": "贵州茅台"},
    )


def _report_service(service: _FakeService) -> NightlyReportService:
    return NightlyReportService(service)


def _row(
    symbol: str,
    *,
    score: float = 70.0,
    evaluation_status: str = "evaluated",
    missing_inputs: list[str] | None = None,
    shortlist_reasons: list[str] | None = None,
    overextension_reasons: list[str] | None = None,
    board_reasons: list[str] | None = None,
    metrics: dict[str, float] | None = None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "score": score,
        "action": "watch",
        "shortlist_score": score,
        "shortlist_reasons": list(shortlist_reasons or ["signal_strength"]),
        "reasons": ["high_score"],
        "overextension": {
            "level": "reject" if overextension_reasons else "none",
            "reject_new_buy": bool(overextension_reasons),
            "evaluation_status": evaluation_status,
            "missing_inputs": list(missing_inputs or []),
            "reasons": list(overextension_reasons or []),
            "metrics": dict(metrics or {}),
        },
        "board_risk": {"reject_new_buy": False, "reasons": list(board_reasons or [])},
    }


def _night_scan(
    rows: list[dict[str, object]],
    *,
    status: str = "ok",
    gate_status: str = "ok",
    gate_reasons: list[str] | None = None,
    fallback: dict[str, object] | None = None,
    funnel: dict[str, object] | None = None,
    prefilter: dict[str, object] | None = None,
) -> dict[str, object]:
    source_report: dict[str, object] = {
        "data_snapshot_id": _TRADE_DATE,
        # 字段形状必须与**生产实测**一致（2026-09-16 夜扫产物核对）：
        # 全市场输入/质量硬筛在质量选择器自己的账里；prefilter.universe_count 与
        # eligible_count 是质量池裁完之后的候选域，两者都等于 300。早期夹具把
        # 5486/3678 填进了 prefilter，掩盖了"把候选域当输入"这个真实缺陷。
        "prefilter": {
            "universe_count": 300,
            "eligible_count": 300,
            "universe_quality_selection": {
                "input_count": 5487,
                "hard_eligible_count": 3678,
                "target_size": 300,
                "selected_count": 300,
            },
            **(prefilter or {}),
        },
        "funnel": {
            "light_count": 100,
            "deep_count": 50,
            "final_count": 0,
            "final_selection": {"rejected": [], "selected_count": 0},
            **(funnel or {}),
        },
    }
    return {
        "status": status,
        "trace_id": "week5-night-scan-20260916214504",
        "night_pool": rows,
        "overnight_top5": rows[:5],
        "candidate_data_gate": {"status": gate_status, "reasons": list(gate_reasons or [])},
        "fallback": fallback or {"applied": False, "reason": ""},
        "readiness": {"status": "ready", "allowed": True},
        "source_report": source_report,
    }


def _build(
    report_service: NightlyReportService,
    night_scan: dict[str, object],
    **kwargs: object,
) -> dict[str, object]:
    params: dict[str, object] = {
        "night_scan": night_scan,
        "trade_date": _TRADE_DATE,
        "generated_at": _NOW,
        "run_id": "run-1",
        "data_snapshot_id": _TRADE_DATE,
    }
    params.update(kwargs)
    return report_service.build_formal_report(**params)  # type: ignore[arg-type]


# --------------------------------------------------------------------- 报告内容


def test_single_candidate_is_shown_as_one(service: _FakeService) -> None:
    """只有 1 只就展示 1 只，不补足数量。"""
    report_service = _report_service(service)
    report = _build(report_service, _night_scan([_row("600000", score=65.89)]))
    report = report_service.publish(report)["report"]

    assert report["scan_status"] == SCAN_STATUS_COMPLETED
    assert len(report["observation_candidates"]) == 1
    content = report_service.render(report).content
    assert "隔夜观察候选 1 只" in content
    assert "600000" in content
    assert "浦发银行" in content
    assert "65.89" in content
    assert "不构成买入指令" in content


def test_five_candidates_are_all_shown(service: _FakeService) -> None:
    report_service = _report_service(service)
    rows = [_row(f"60000{index}", score=70.0 - index) for index in range(5)]
    report = _build(report_service, _night_scan(rows))
    content = report_service.render(report).content
    for index in range(5):
        assert f"60000{index}" in content
    assert "未在正文展示" not in content


def test_more_than_five_candidates_truncate_body_but_keep_all_in_report(
    service: _FakeService,
) -> None:
    """正文最多 5 只；完整清单保留在报告里（不丢业务事实）。"""
    report_service = _report_service(service)
    rows = [_row(f"6000{index:02d}", score=80.0 - index) for index in range(9)]
    report = _build(report_service, _night_scan(rows))
    assert len(report["observation_candidates"]) == 9

    content = report_service.render(report).content
    assert "600000" in content and "600004" in content
    assert "600005" not in content
    assert "另有 4 只候选未在正文展示" in content


def test_zero_candidates_is_empty_not_completed(service: _FakeService) -> None:
    report_service = _report_service(service)
    report = _build(report_service, _night_scan([]))
    assert report["scan_status"] == SCAN_STATUS_EMPTY
    content = report_service.render(report).content
    assert "今日正常完成，无合格候选" in content
    assert "隔夜观察候选" not in content


def test_body_carries_required_facts_and_funnel(service: _FakeService) -> None:
    report_service = _report_service(service)
    scan = _night_scan(
        [
            _row(
                "600000", shortlist_reasons=["trend_alignment"], overextension_reasons=["ret5_high"]
            )
        ],
        funnel={
            "final_selection": {
                "selected_count": 0,
                "rejected": [
                    {"symbol": "000001", "reject_reasons": ["below_min_threshold"]},
                    {"symbol": "000002", "reject_reasons": ["below_min_threshold"]},
                    {"symbol": "000003", "reject_reasons": ["cross_review_failed"]},
                ],
            }
        },
    )
    report = _build(report_service, scan)
    content = report_service.render(report).content
    assert "【晚间选股报告】2026-09-16" in content
    assert "数据日期：2026-09-16" in content
    assert "入选依据：趋势一致" in content
    assert "风险说明：5 日涨幅偏大" in content
    assert "筛选过程：输入5487 → 质量硬筛3678 → 质量池300 → 轻筛100 → 深评50 → 观察1" in content
    assert "主要过滤原因：" in content and "低于门槛" not in content
    assert "最终筛选数量（审计字段，非买入信号）：0" in content


# --------------------------------------------------------------------- 异常语义


def test_blocked_data_gate_is_not_reported_as_no_opportunity(service: _FakeService) -> None:
    report_service = _report_service(service)
    scan = _night_scan(
        [],
        status="blocked_data_gate",
        gate_status="blocked",
        gate_reasons=["intraday_freshness_below_80pct"],
    )
    report = _build(report_service, scan)
    assert report["scan_status"] == SCAN_STATUS_BLOCKED
    content = report_service.render(report).content
    assert "未完成有效选股" in content
    assert "分钟数据新鲜率低于 80%" in content
    assert "今日正常完成" not in content


def test_scan_exception_is_failed(service: _FakeService) -> None:
    report_service = _report_service(service)
    scan = _night_scan([], status="failed")
    report = _build(
        report_service,
        scan,
        failure_stage="night_scan",
        failure_reason="night_scan_failed:RuntimeError:boom",
    )
    assert report["scan_status"] == SCAN_STATUS_FAILED
    content = report_service.render(report).content
    assert "扫描失败" in content
    assert "失败阶段：night_scan" in content
    assert "RuntimeError" in content


def test_timeout_is_failed_with_stage(service: _FakeService) -> None:
    report_service = _report_service(service)
    report = _build(
        report_service,
        _night_scan([]),
        scan_status=SCAN_STATUS_FAILED,
        failure_stage="timeout",
        failure_reason="重型扫描超时",
    )
    assert report["scan_status"] == SCAN_STATUS_FAILED
    assert "失败阶段：timeout" in report_service.render(report).content


def test_previous_pool_fallback_is_blocked_and_labelled(service: _FakeService) -> None:
    """旧池回退不得包装成今天的 completed，必须标原交易日且不计入今日入选。"""
    report_service = _report_service(service)
    stale_row = _row("600000", score=66.1)
    stale_row["night_pool_trade_date"] = "2026-09-15"
    scan = _night_scan(
        [stale_row],
        status="fallback",
        gate_status="blocked",
        gate_reasons=["nightly_data_not_ready"],
        fallback={"applied": True, "reason": "nightly_data_not_ready"},
    )
    report = _build(report_service, scan)
    assert report["scan_status"] == SCAN_STATUS_BLOCKED
    assert report["fallback_source_date"] == "2026-09-15"
    content = report_service.render(report).content
    assert "旧池回退" in content
    assert "2026-09-15" in content
    assert "不计入今日入选数量" in content


# --------------------------------------------------------------------- 来源一致


def test_evolution_shortlist_difference_does_not_leak_into_the_report(
    service: _FakeService,
) -> None:
    """正文只来自送入的这一次夜扫结果：其它阶段的短名单不能冒充它。

    实现上不是"比较大小"，而是报告里根本没有其它来源的入口——候选集完全由
    ``night_pool`` 决定，签名里也拿不到进化任务的状态。
    """
    report_service = _report_service(service)
    report = _build(report_service, _night_scan([_row("600000")]))
    assert [item["symbol"] for item in report["observation_candidates"]] == ["600000"]
    assert "20 只" not in report_service.render(report).content


def test_missing_name_degrades_to_placeholder_without_blocking(
    service: _FakeService,
) -> None:
    report_service = _report_service(service)
    report = _build(report_service, _night_scan([_row("601999")]))
    content = report_service.render(report).content
    assert "601999" in content
    assert "名称暂缺" in content


def test_name_lookup_failure_does_not_block_the_report(service: _FakeService) -> None:
    service.name_failures.add("600000")
    report_service = _report_service(service)
    report = _build(report_service, _night_scan([_row("600000")]))
    content = report_service.render(report).content
    assert "600000" in content
    assert "名称暂缺" in content


def test_names_are_only_resolved_for_displayed_entries(service: _FakeService) -> None:
    """为几十只候选逐只回源拉名称会把报告构造拖成分钟级——只解析要展示的。"""
    report_service = _report_service(service)
    rows = [_row(f"6000{index:02d}") for index in range(9)]
    _build(report_service, _night_scan(rows))
    assert len(service.name_calls) <= 2 * report_service.config.display_top_k


def test_candidates_are_split_by_evaluation_status(service: _FakeService) -> None:
    """风险评估输入不足的条目单独列为"数据待补全"，不混入观察候选。"""
    report_service = _report_service(service)
    rows = [
        _row("600000", score=72.0),
        _row(
            "000001",
            score=68.0,
            evaluation_status="insufficient_input",
            missing_inputs=["ma5", "atr14"],
        ),
    ]
    report = _build(report_service, _night_scan(rows))
    assert [item["symbol"] for item in report["observation_candidates"]] == ["600000"]
    assert [item["symbol"] for item in report["incomplete_candidates"]] == ["000001"]
    content = report_service.render(report).content
    assert "数据待补全" in content
    assert "缺 ma5/atr14" in content


def test_legacy_row_without_evaluation_status_is_not_assumed_complete(
    service: _FakeService,
) -> None:
    """旧产物缺 evaluation_status：不得默认"已完整评估"放进观察候选。"""
    report_service = _report_service(service)
    row = _row("600000")
    del row["overextension"]["evaluation_status"]  # type: ignore[index]
    report = _build(report_service, _night_scan([row]))
    assert report["observation_candidates"] == []
    assert [item["symbol"] for item in report["incomplete_candidates"]] == ["600000"]


# --------------------------------------------------------------------- 版本冻结


def test_republishing_identical_content_reuses_the_same_report_id(
    service: _FakeService,
) -> None:
    """普通重启/补发不得产生新版本：内容一致就复用同一 report_id。"""
    report_service = _report_service(service)
    scan = _night_scan([_row("600000")])
    first = report_service.publish(_build(report_service, scan))
    # 重启：换了 run_id / trace_id / 生成时间，业务内容完全一样
    second = report_service.publish(
        _build(
            report_service,
            scan,
            run_id="run-2",
            generated_at=datetime.fromisoformat("2026-09-16T22:05:00+08:00"),
        )
    )
    assert first["published"] is True
    assert second["published"] is False
    assert second["reason"] == "unchanged"
    assert second["report_id"] == first["report_id"]


def test_changed_content_creates_a_revision_with_label(service: _FakeService) -> None:
    report_service = _report_service(service)
    first = report_service.publish(_build(report_service, _night_scan([_row("600000")])))
    second = report_service.publish(
        _build(report_service, _night_scan([_row("600000"), _row("000001")]))
    )
    assert second["published"] is True
    assert second["report_id"] != first["report_id"]
    assert second["report"]["revision"] == 2
    assert "（修订版）" in report_service.render(second["report"]).title
    # 旧版本仍在磁盘上，回执对得上账
    assert report_service.load_report(first["report_id"], trade_date=_TRADE_DATE) is not None


def test_frozen_report_is_immutable(service: _FakeService) -> None:
    report_service = _report_service(service)
    published = report_service.publish(_build(report_service, _night_scan([_row("600000")])))
    path = report_service.report_path(_TRADE_DATE, published["report_id"])
    original = path.read_text(encoding="utf-8")

    tampered = _build(report_service, _night_scan([_row("000001"), _row("000002")]))
    tampered["report_id"] = published["report_id"]
    tampered["revision"] = published["report"]["revision"]
    report_service.freeze_report(tampered)

    assert path.read_text(encoding="utf-8") == original


def test_date_state_records_phase_and_pointer(service: _FakeService) -> None:
    report_service = _report_service(service)
    published = report_service.publish(_build(report_service, _night_scan([_row("600000")])))
    report_service.update_date_state(
        _TRADE_DATE,
        {"scan_phase": "published", "scan_attempts": 1},
    )
    state = report_service.read_date_state(_TRADE_DATE)
    assert state["published_report_id"] == published["report_id"]
    assert state["scan_phase"] == "published"
    assert state["scan_attempts"] == 1
    assert report_service.published_report(_TRADE_DATE)["report_id"] == published["report_id"]


def test_notice_does_not_take_over_the_formal_pointer(service: _FakeService) -> None:
    """延迟/未完成说明是过程说明，不能顶掉正式报告的指针。"""
    report_service = _report_service(service)
    published = report_service.publish(_build(report_service, _night_scan([_row("600000")])))
    notice = report_service.build_notice(
        trade_date=_TRADE_DATE,
        generated_at=_NOW,
        notice=NOTICE_DELAY,
        scan_status=SCAN_STATUS_BLOCKED,
        reason="扫描仍在进行",
        date_state={"scan_phase": "scanning", "scan_attempts": 1},
    )
    assert notice["report_kind"] == REPORT_KIND_NOTICE
    published_notice = report_service.publish(notice)
    assert published_notice["published"] is True
    state = report_service.read_date_state(_TRADE_DATE)
    assert state["published_report_id"] == published["report_id"]
    assert state["notices"] == {NOTICE_DELAY: notice["report_id"]}


def test_notice_is_sent_at_most_once_per_kind(service: _FakeService) -> None:
    report_service = _report_service(service)
    first = report_service.publish(
        report_service.build_notice(
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
            notice=NOTICE_DEADLINE,
            scan_status=SCAN_STATUS_BLOCKED,
            reason="数据未就绪",
        )
    )
    second = report_service.publish(
        report_service.build_notice(
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
            notice=NOTICE_DEADLINE,
            scan_status=SCAN_STATUS_BLOCKED,
            reason="数据未就绪",
        )
    )
    assert first["published"] is True
    assert second["published"] is False
    delay = report_service.publish(
        report_service.build_notice(
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
            notice=NOTICE_DELAY,
            scan_status=SCAN_STATUS_BLOCKED,
        )
    )
    assert delay["published"] is True  # 不同 notice 用不同去重键


# ------------------------------------------------------------------- 正文长度


def test_long_reasons_are_truncated_in_a_fixed_order(service: _FakeService) -> None:
    """超长正文按固定顺序截断：日期、状态、股票代码与关键风险优先保留。"""
    report_service = _report_service(service)
    report_service.config.message_max_chars = 300
    rows = [
        _row(
            f"6000{index:02d}",
            shortlist_reasons=[f"理由{'长' * 200}{index}"],
            overextension_reasons=[f"风险{'长' * 200}{index}"],
        )
        for index in range(5)
    ]
    report = _build(report_service, _night_scan(rows))
    rendered = report_service.render(report)

    assert rendered.truncated is True
    assert len(rendered.content) <= 300
    assert "2026-09-16" in rendered.content
    assert "600000" in rendered.content
    assert "不构成买入指令" in rendered.content


def test_report_keeps_full_content_even_when_message_is_truncated(
    service: _FakeService,
) -> None:
    report_service = _report_service(service)
    report_service.config.message_max_chars = 300
    rows = [_row(f"6000{index:02d}", shortlist_reasons=[f"理由{'长' * 200}"]) for index in range(5)]
    report = _build(report_service, _night_scan(rows))
    assert len(report["observation_candidates"]) == 5
    assert len(report["observation_candidates"][0]["reasons"][0]) > 200


def test_render_is_deterministic(service: _FakeService) -> None:
    report_service = _report_service(service)
    report = _build(report_service, _night_scan([_row("600000"), _row("000001")]))
    first = report_service.render(report)
    second = report_service.render(json.loads(json.dumps(report)))
    assert (first.title, first.content) == (second.title, second.content)


# ------------------------------------------------------------------ 验收回放


def test_replay_report_is_labelled_and_isolated_from_the_formal_pointer(
    service: _FakeService,
) -> None:
    """回放报告必须自带"历史数据、非当日结果"的标注，且不占用正式报告指针。

    回放是交付链路的灰度手段（方案 §5.4 第 1、2 步）：既不能和正式结果混淆，
    也不能因为做过一次回放就顶掉当天的正式报告。
    """
    report_service = _report_service(service)
    formal = report_service.publish(_build(report_service, _night_scan([_row("600000")])))

    replay = report_service.build_replay_report(
        night_scan=_night_scan([_row("000001", score=66.0)]),
        trade_date=_TRADE_DATE,
        generated_at=_NOW,
        source_label="2026-09-16 历史夜扫产物",
    )
    published = report_service.publish(replay)
    assert published["published"] is True
    assert published["report"]["report_kind"] == "replay"
    assert published["report_id"].startswith("rp-")

    rendered = report_service.render(published["report"])
    assert "验收回放" in rendered.title
    assert "是历史数据，不是当日结果" in rendered.content
    assert "000001" in rendered.content

    # 正式报告指针没有被回放顶掉
    state = report_service.read_date_state(_TRADE_DATE)
    assert state["published_report_id"] == formal["report_id"]
    assert state["notices"]["replay"] == published["report_id"]
    assert report_service.published_report(_TRADE_DATE)["report_id"] == formal["report_id"]


def test_all_incomplete_candidates_do_not_read_as_no_opportunity(
    service: _FakeService,
) -> None:
    """ "完成 + 0 只观察"必须说清真实原因，不能被读成"今天没有机会"。"""
    report_service = _report_service(service)
    rows = [
        _row("600000", evaluation_status="insufficient_input", missing_inputs=["ma5"]),
        _row("000001", evaluation_status="insufficient_input", missing_inputs=["atr14"]),
    ]
    report = _build(report_service, _night_scan(rows))
    assert report["scan_status"] == SCAN_STATUS_COMPLETED
    content = report_service.render(report).content
    assert "无通过完整风险检查的隔夜观察候选（2 只数据待补全）" in content


def test_exclusion_reasons_are_localized_for_readers(service: _FakeService) -> None:
    """主要过滤原因要给人看：不能直接暴露 below_min_threshold 这类内部代号。"""
    report_service = _report_service(service)
    scan = _night_scan(
        [_row("600000")],
        funnel={
            "final_selection": {
                "selected_count": 0,
                "rejected": [
                    {"symbol": "000001", "reject_reasons": ["below_min_threshold"]},
                    {"symbol": "000002", "reject_reasons": ["below_min_threshold"]},
                    {"symbol": "000003", "reject_reasons": ["cross_review_failed"]},
                    {"symbol": "000004", "reject_reasons": ["overextension_reject_new_buy"]},
                ],
            }
        },
    )
    content = report_service.render(_build(report_service, scan)).content
    assert "低于最低分门槛 2" in content
    assert "交叉复核未通过 1" in content
    assert "过热拒绝新建仓 1" in content
    assert "below_min_threshold" not in content
    assert "cross_review_failed" not in content


def test_entry_reasons_prefer_human_readable_shortlist_labels(service: _FakeService) -> None:
    """入选依据优先用漏斗声明的可读短句，而不是内部信号代号。"""
    report_service = _report_service(service)
    row = _row("600000", shortlist_reasons=["trend_alignment", "capital_confirmation"])
    row["reasons"] = ["soup_entry", "news_component_unavailable"]
    content = report_service.render(_build(report_service, _night_scan([row]))).content
    assert "入选依据：趋势一致、资金面确认" in content
    assert "soup_entry" not in content


def test_funnel_uses_the_quality_selector_ledger_not_the_trimmed_universe(
    service: _FakeService,
) -> None:
    """漏斗必须取自质量选择器的账，不能拿"质量池裁完之后的候选域"当全市场输入。

    2026-09-17 实测首条回放消息把 5487 只显示成 300 只：prefilter.universe_count 与
    eligible_count 都等于 300（裁完之后的候选域），真正的输入/硬筛数在
    universe_quality_selection 里。这条测试用与生产同形的字段把该语义钉住。
    """
    report_service = _report_service(service)
    scan = _night_scan(
        [_row("600000")],
        prefilter={"universe_count": 300, "eligible_count": 300},
    )
    report = _build(report_service, scan)
    assert report["funnel_counts"]["input_count"] == 5487
    assert report["funnel_counts"]["eligible_count"] == 3678
    assert report["funnel_counts"]["quality_pool_count"] == 300
    # 候选域作为审计字段保留，但不顶替"输入"
    assert report["funnel_counts"]["candidate_universe_count"] == 300
    content = report_service.render(report).content
    assert "输入5487" in content
    assert "输入300" not in content


def test_funnel_degrades_to_candidate_universe_when_selector_did_not_run(
    service: _FakeService,
) -> None:
    """质量选择器没跑时不得硬凑"输入/质量硬筛"：换标签展示候选域。"""
    report_service = _report_service(service)
    scan = _night_scan([_row("600000")])
    source = scan["source_report"]  # type: ignore[index]
    source["prefilter"].pop("universe_quality_selection")  # type: ignore[union-attr]
    content = report_service.render(_build(report_service, scan)).content
    assert "筛选过程：候选域300 → 轻筛100 → 深评50 → 观察1" in content
    assert "质量硬筛" not in content
    assert "输入" not in content


def test_legacy_unlabelled_candidates_are_not_described_as_missing_data(
    service: _FakeService,
) -> None:
    """旧产物缺评估状态 ≠ 指标算不出来，措辞必须分开，否则会误导读者。"""
    report_service = _report_service(service)
    legacy = _row("600000")
    del legacy["overextension"]["evaluation_status"]  # type: ignore[index]
    content = report_service.render(_build(report_service, _night_scan([legacy]))).content
    assert "评估状态未标注（旧版本产物，不计入观察候选）：" in content
    assert "数据待补全" not in content
    assert "（1 只评估状态未标注）" in content


def test_insufficient_input_keeps_the_missing_data_wording(service: _FakeService) -> None:
    """确实算不出指标时保留"数据待补全"，并列出缺哪些指标。"""
    report_service = _report_service(service)
    row = _row("600000", evaluation_status="insufficient_input", missing_inputs=["ma5", "atr14"])
    content = report_service.render(_build(report_service, _night_scan([row]))).content
    assert "数据待补全（风险指标输入不足，不计入观察候选）：" in content
    assert "缺 ma5/atr14" in content
    assert "评估状态未标注" not in content


def test_changed_replay_content_gets_a_new_version_file(service: _FakeService) -> None:
    """回放内容变化必须**真的写出新版本文件**，不能只把指针挪到不存在的版本。

    2026-09-17 实测踩到：回放 report_id 固定为 rp-<日期>，第二次内容变化时撞上
    "同 id 不覆盖"，publish 仍报告 new_revision、指针也照挪，但盘上还是第 1 版。
    """
    report_service = _report_service(service)
    first = report_service.publish(
        report_service.build_replay_report(
            night_scan=_night_scan([_row("600000")]),
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
        )
    )
    assert first["report"]["revision"] == 1
    assert first["report_id"].endswith("-01")

    second = report_service.publish(
        report_service.build_replay_report(
            night_scan=_night_scan([_row("600000"), _row("000001")]),
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
        )
    )
    assert second["report"]["revision"] == 2
    assert second["report_id"].endswith("-02")
    assert second["report_id"] != first["report_id"]

    # 两个版本都真实落盘，且内容各不相同
    stored_v1 = report_service.load_report(first["report_id"], trade_date=_TRADE_DATE)
    stored_v2 = report_service.load_report(second["report_id"], trade_date=_TRADE_DATE)
    assert stored_v1 is not None and stored_v2 is not None
    assert len(stored_v1["observation_candidates"]) == 1
    assert len(stored_v2["observation_candidates"]) == 2
    assert stored_v2["content_digest"] == second["report"]["content_digest"]
    # 指针指向的版本必须确实存在
    state = report_service.read_date_state(_TRADE_DATE)
    assert state["notices"]["replay"] == second["report_id"]


def test_same_id_with_different_content_never_claims_a_new_version(
    service: _FakeService,
) -> None:
    """同 id 已冻结成另一份内容时：以磁盘为准，不声称新版本、指针校准回它。

    只有 id 不带版本号的报告（延迟/未完成说明）可能走到这里。真正的风险不是报错缺
    失，而是**静默声称一个新版本、把指针挪向没写进磁盘的内容**——那会让"报告文件
    写成功之后才发布指针"的契约失效。
    """
    report_service = _report_service(service)
    first = report_service.publish(
        report_service.build_notice(
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
            notice=NOTICE_DELAY,
            scan_status=SCAN_STATUS_BLOCKED,
            reason="重型扫描尚未完成",
        )
    )
    # 模拟日期状态丢失（指针没了但报告文件还在）
    state = report_service.read_date_state(_TRADE_DATE)
    state.pop("notices", None)
    report_service.update_date_state(_TRADE_DATE, {"notices": {}})

    second = report_service.publish(
        report_service.build_notice(
            trade_date=_TRADE_DATE,
            generated_at=_NOW,
            notice=NOTICE_DELAY,
            scan_status=SCAN_STATUS_BLOCKED,
            reason="另一段不同的说明文字",
        )
    )
    assert second["published"] is False
    assert second["reason"] == "already_frozen_with_different_content"
    assert second["report_id"] == first["report_id"]
    assert second["report"]["revision"] == 1
    # 指针被校准回磁盘上那份，避免交付检查每分钟重复尝试
    assert report_service.read_date_state(_TRADE_DATE)["notices"]["delay"] == first["report_id"]
    # 磁盘内容没有被覆盖
    assert "重型扫描尚未完成" in str(
        report_service.load_report(first["report_id"], trade_date=_TRADE_DATE)["reason"]
    )


# --------------------------------------------------- R1 日期状态锁 / 发布事务


def test_date_state_update_waits_for_the_lock_instead_of_writing_through(
    service: _FakeService,
) -> None:
    """别人持着锁时**必须等待**，不能照写。

    2026-09-17 独立验收复现：update_date_state 调用 acquire() 后忽略返回值，
    而 acquire() 被占用时返回 False 不抛异常 → 锁等于没加，
    `{"lock_owner_alive":true,"state":"OVERWRITTEN_WITHOUT_LOCK"}`。
    """
    report_service = _report_service(service)
    report_service.update_date_state(_TRADE_DATE, {"seed": 1})

    holder = report_service._date_lock(_TRADE_DATE)  # noqa: SLF001
    assert holder.acquire() is True
    released: list[float] = []
    import threading
    import time

    def _release_later() -> None:
        time.sleep(0.4)
        holder.release()
        released.append(1.0)

    thread = threading.Thread(target=_release_later, daemon=True)
    thread.start()
    try:
        # 等待上限给足：应当等到持有者释放后完成写入，而不是穿透写进去
        report_service._date_lock_timeout_sec = 5.0  # noqa: SLF001
        report_service.update_date_state(_TRADE_DATE, {"late": True})
        # 关键是**顺序**：返回时持锁者必须已经释放。旧行为会在锁仍被持有时就写完返回。
        assert released, "更新函数在持锁者仍持锁时就写入了（锁没起作用）"
    finally:
        thread.join(timeout=5)
    state = report_service.read_date_state(_TRADE_DATE)
    assert state["seed"] == 1
    assert state["late"] is True


def test_date_state_update_fails_loudly_when_the_lock_stays_busy(
    service: _FakeService,
) -> None:
    """等不到锁必须明确失败，不能裸写。"""
    report_service = _report_service(service)
    report_service._date_lock_timeout_sec = 0.2  # noqa: SLF001
    holder = report_service._date_lock(_TRADE_DATE)  # noqa: SLF001
    assert holder.acquire() is True
    try:
        with pytest.raises(DateStateBusyError):
            report_service.update_date_state(_TRADE_DATE, {"should_not_land": True})
    finally:
        holder.release()
    assert "should_not_land" not in report_service.read_date_state(_TRADE_DATE)


def test_concurrent_publishes_keep_both_writers_updates(service: _FakeService) -> None:
    """两个写者分别更新不同字段时，后写者不得把前者的更新整段丢掉。"""
    report_service = _report_service(service)
    report_service.update_date_state(_TRADE_DATE, {"scan_phase": "scanning"})
    published = report_service.publish(_build(report_service, _night_scan([_row("600000")])))

    state = report_service.read_date_state(_TRADE_DATE)
    assert state["scan_phase"] == "scanning"  # 未被发布流程抹掉
    assert state["published_report_id"] == published["report_id"]
    # 交付登记与报告指针也不该互相覆盖
    report_service.update_date_state(_TRADE_DATE, {"delivery_ids": ["d1"]})
    state = report_service.read_date_state(_TRADE_DATE)
    assert state["scan_phase"] == "scanning"
    assert state["published_report_id"] == published["report_id"]
    assert state["delivery_ids"] == ["d1"]


def test_publish_holds_the_date_lock_for_the_whole_transaction(service: _FakeService) -> None:
    """发布期间日期锁必须被持有：否则两个发布者会分到同一个 revision。"""
    report_service = _report_service(service)
    holder = report_service._date_lock(_TRADE_DATE)  # noqa: SLF001
    assert holder.acquire() is True
    report_service._date_lock_timeout_sec = 0.2  # noqa: SLF001
    try:
        with pytest.raises(DateStateBusyError):
            report_service.publish(_build(report_service, _night_scan([_row("600000")])))
    finally:
        holder.release()
    # 失败时不得留下任何"已发布"的痕迹
    assert report_service.read_date_state(_TRADE_DATE).get("published_report_id") in (None, "")


def test_concurrent_publishes_do_not_share_a_revision(service: _FakeService) -> None:
    """两个发布者并发时不得分到同一个 revision、也不得互相覆盖冻结文件。

    版本分配与冻结/指针发布必须在一个日期级事务里。旧实现是"锁外读当前版本 →
    分配 revision → 冻结 → 写指针"，两个发布者会各自读到同一版、各自分配同一个
    revision，最后一个指针覆盖前一个。
    """
    report_service = _report_service(service)
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []
    import threading

    def _publish(rows: list[dict[str, object]]) -> None:
        try:
            results.append(report_service.publish(_build(report_service, _night_scan(rows))))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_publish, args=([_row("600000")],), daemon=True),
        threading.Thread(target=_publish, args=([_row("600000"), _row("000001")],), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    ids = sorted(str(item["report_id"]) for item in results)
    assert len(set(ids)) == 2, f"并发发布分到了同一个 report_id: {ids}"
    # 两份文件都真实落盘，且各自的内容摘要与自身一致
    for report_id in ids:
        stored = report_service.load_report(report_id, trade_date=_TRADE_DATE)
        assert stored is not None, f"{report_id} 只被声称发布，磁盘上不存在"
        assert stored["content_digest"] == report_service.content_digest(stored)
    # 指针指向版本号最大的那一份
    pointer = report_service.read_date_state(_TRADE_DATE)["published_report_id"]
    assert pointer == max(ids)
    assert report_service.published_report(_TRADE_DATE)["report_id"] == pointer
