"""trend 尾盘影子链路的运行时接线验收（改进计划 §3.4 + §4 影子验证）。

钉住的是"影子不接管旧输出、算不出来就 0 只且原因可见"这几条，
而不是"能不能凑出几只推荐"。
"""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    TrendContractError,
)
from stock_analyzer.labels.tail_net_profit import (
    TAIL_SCHEMA_VERSION,
    register_tail_label_policy,
    tail_label_policy_record,
)
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRegistry,
    build_label_policy_record,
)
from stock_analyzer.runtime.services.trend_tail_shadow_service import (
    TrendTailShadowService,
)

CONTRACT = DEFAULT_TREND_CONTRACT
#: 当前契约推导出的标签口径 id；留档里的 label_policy_id 必须能在 registry 里查到它。
TAIL_POLICY_ID = tail_label_policy_record(CONTRACT).label_policy_id
NOW = datetime(2026, 10, 9, 14, 45, 0)


class FakeService:
    """镜像真实 runtime service 的注入面。

    真实 service 一定带 ``_label_policy_registry``（``_configure_learning_protocol_runtime``
    里建的），所以这里默认也给一份真的、并已注册当前净盈利标签契约的 registry；
    想模拟"没接线"或"口径漂移"时显式传 ``registry=`` 覆盖。
    """

    def __init__(self, *, manifest: dict | None, tmp_path: Path, registry: object = "default",
                 ) -> None:
        self._manifest = manifest
        self._tmp = tmp_path
        self._config = type("C", (), {"training": type("T", (), {
            "serving_manifest_path": "artifacts/model_serving_manifest.json"})()})()
        if registry == "default":
            registry = LabelPolicyRegistry(tmp_path / "learning_protocol.duckdb")
            register_tail_label_policy(registry)
        self._label_policy_registry = registry

    def _resolve_evolution_path(self, value):
        return str(self._tmp / str(value).replace("artifacts/", ""))

    def _read_serving_manifest(self):
        return dict(self._manifest) if self._manifest else {}

    def _runtime_code_commit(self):
        return "cafe123"


def _tail_manifest() -> dict:
    return {
        "model_id": "trend-tail-lgbm-2026q4",
        "artifact_content_hash": "sha256:abcdef",
        "code_commit": "cafe123",
        "label_policy_id": TAIL_POLICY_ID,
    }


def _bars(price: float = 10.0, *, day: date | None = None, lock_at: int | None = None) -> list:
    """14:20–14:44 的已完成分钟 bar。

    必须一直铺到接近 NOW：14:45 的确认在实盘上看到的最新已完成 bar 就是 14:44，
    只给到 14:31 的话先撞上的会是行情陈旧度门而不是被测规则本身。
    """
    day = day or NOW.date()
    up_limit = round(price * 1.1, 2)
    out = []
    for minute in range(20, 45):
        value = up_limit if minute == lock_at else price
        out.append((
            datetime(day.year, day.month, day.day, 14, minute),
            {"open": value, "high": value, "low": value, "close": value,
             "up_limit": up_limit, "trade_status": "normal"},
        ))
    return out


def _pool(symbols: list[str], *, features: bool = True) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "risk_state": "",
            "features": ({"excess_ret_20": 0.02, "atr14_pct": 0.02} if features else {}),
        }
        for symbol in symbols
    ]


def _run(tmp_path, *, watch_pool, probabilities, bars=None, service=None, **kwargs):
    service = service or FakeService(manifest=_tail_manifest(), tmp_path=tmp_path)
    svc = TrendTailShadowService(service, report_dir=tmp_path / "shadow")
    minute_bars = bars if bars is not None else {
        str(row["symbol"]): _bars() for row in watch_pool
    }
    return svc.run(
        timestamp=kwargs.pop("timestamp", NOW),
        watch_pool=watch_pool,
        minute_bars=minute_bars,
        probabilities=probabilities,
        **kwargs,
    )


def test_no_tail_probability_yields_zero_and_says_why(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]), probabilities={})
    assert report["mode"] == "shadow"
    assert report["final_symbols"] == []
    assert report["blocking_reason"] == "no_tail_probability_available"
    assert report["model_identity"]["recorded"] is False


def test_serving_model_from_another_label_policy_is_not_usable(tmp_path) -> None:
    service = FakeService(
        manifest={**_tail_manifest(), "label_policy_id": "label_policy_v2_soup"},
        tmp_path=tmp_path,
    )
    report = _run(
        tmp_path,
        watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9},
        service=service,
    )
    assert report["blocking_reason"] == "serving_model_is_not_tail_label_policy"
    assert report["final_symbols"] == []


def _policy_failures(report: dict) -> list[str]:
    return [item for item in report["model_identity"]["recording_failures"]
            if str(item).startswith("label_policy_")]


def test_registered_label_policy_is_positively_verified(tmp_path) -> None:
    """id 只是字符串时不算绑定：必须在 registry 里查到且逐字段对得上才算 verified。"""
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.9})
    assert _policy_failures(report) == []
    assert report["model_identity"]["label_policy_verified"] is True


def test_declared_label_policy_absent_from_registry_is_named(tmp_path) -> None:
    """registry 里查不到这个 id → 说清"没注册"，而不是留一个没人能核的字符串。"""
    service = FakeService(manifest=_tail_manifest(), tmp_path=tmp_path,
                          registry=LabelPolicyRegistry(tmp_path / "empty.duckdb"))
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.9}, service=service)
    assert _policy_failures(report) == [f"label_policy_not_registered:{TAIL_POLICY_ID}"]
    assert report["model_identity"]["label_policy_verified"] is False
    # 影子链路不因为口径未绑定就伪造阻塞：它照样出结果，但失败原因必须留名。
    assert report["blocking_reason"] in (None, "")


def test_unwired_registry_is_reported_as_unavailable(tmp_path) -> None:
    service = FakeService(manifest=_tail_manifest(), tmp_path=tmp_path, registry=None)
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.9}, service=service)
    assert _policy_failures(report) == ["label_policy_registry_unavailable"]


def test_label_policy_with_other_tp_sl_is_drift_not_a_match(tmp_path) -> None:
    """v4 前缀对得上、id 也真存在，但那是另一套 TP/SL 的标签口径 → 必须报漂移。

    这是最危险的一种：留档看起来完全正常，样本却会被另一个持有规则解释。
    """
    other = build_label_policy_record(
        label_name="net_profit_5d_tail_tp10_sl5", take_profit_pct=0.10,
        stop_loss_pct=0.05, horizon_days=5, price_basis="tail_confirm_next_bar",
        exclude_untradable=True, conflict_policy="stop_loss_first",
        conflict_soft_label_value=0.0, schema_version=TAIL_SCHEMA_VERSION,
    )
    registry = LabelPolicyRegistry(tmp_path / "learning_protocol.duckdb")
    registry.register(other)
    service = FakeService(
        manifest={**_tail_manifest(), "label_policy_id": other.label_policy_id},
        tmp_path=tmp_path, registry=registry,
    )
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.9}, service=service)
    failures = _policy_failures(report)
    assert len(failures) == 1 and failures[0].startswith("label_policy_drifts_from_tail_contract:")
    for field in ("take_profit_pct", "label_name", "label_policy_hash"):
        assert field in failures[0]
    assert report["model_identity"]["label_policy_verified"] is False


def test_missing_serving_manifest_is_visible_not_defaulted(tmp_path) -> None:
    service = FakeService(manifest=None, tmp_path=tmp_path)
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9}, service=service,
    )
    assert report["blocking_reason"] == "serving_manifest_missing"
    # "读不到"要说清是哪一种读不到，而不是只留一个空 dict。
    assert report["model_identity"]["recording_failures"] == ["serving_manifest_empty"]


def test_raising_manifest_reader_is_named_not_swallowed(tmp_path) -> None:
    class Boom(FakeService):
        def _read_serving_manifest(self):
            raise OSError("manifest file is gone")

    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9}, service=Boom(manifest={}, tmp_path=tmp_path),
    )
    assert report["blocking_reason"] == "serving_manifest_missing"
    assert report["model_identity"]["recording_failures"] == [
        "serving_manifest_reader_raised:OSError",
    ]
    assert report["final_symbols"] == []


def _v1_manifest(tmp_path, **overrides) -> dict:
    """用真实构造器出 ``model_serving_manifest.v1`` 的原样分层，再填尾盘链路的字段。

    顶层只有 schema/generated_at/source —— 按顶层读身份会永远读空。
    """
    from stock_analyzer.models.serving_manifest import build_serving_manifest

    payload = build_serving_manifest(artifact_path=str(tmp_path / "artifact.json"))
    payload["serving"].update({
        "label_policy_id": TAIL_POLICY_ID,
        "artifact_content_hash": "sha256:feedface",
        "dataset_manifest_id": "dataset_manifest_2026q4_5f3c",
    })
    payload["registry"].update({"model_id": "trend-tail-lgbm-2026q4"})
    payload["serving"].update(overrides)
    return payload


def test_identity_binds_from_the_real_v1_manifest_sections(tmp_path) -> None:
    """§3.1"绑定实际加载的模型、训练 manifest"必须对 v1 的真实形状成立，不只在扁平 fixture 里。"""
    manifest = _v1_manifest(tmp_path, code_commit="cafe123")
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.71},
        service=FakeService(manifest=manifest, tmp_path=tmp_path),
    )
    identity = report["model_identity"]
    assert identity["recorded"] is True, identity
    assert identity["error"] == ""
    assert identity["training_manifest_id"] == "dataset_manifest_2026q4_5f3c"
    assert report["final_symbols"] == ["600000.SH"]


def test_v1_manifest_without_a_code_commit_blocks_and_names_the_cause(tmp_path) -> None:
    """v1 清单本身不带 code_commit：这时宁可 0 只，也要把"为什么绑不上"写成事实。"""
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.71},
        service=FakeService(manifest=_v1_manifest(tmp_path), tmp_path=tmp_path),
    )
    assert report["blocking_reason"] == "training_commit_unknown"
    assert report["final_symbols"] == []
    failures = report["model_identity"]["recording_failures"]
    assert "training_commit_absent_from_serving_manifest" in failures
    # dataset_manifest_id 在清单里，所以这一项不该被点名。
    assert "training_manifest_id_absent_from_serving_manifest" not in failures


def test_recommendations_follow_threshold_and_cap(tmp_path) -> None:
    pool = _pool(["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600004.SH"])
    probs = {
        "600000.SH": 0.71, "600001.SH": 0.66, "600002.SH": 0.65,
        "600003.SH": 0.64, "600004.SH": 0.40,
    }
    report = _run(tmp_path, watch_pool=pool, probabilities=probs)
    assert report["final_symbols"] == ["600000.SH", "600001.SH", "600002.SH"]
    assert report["filled"] == 5
    assert report["counts"]["rejected"] == 2  # 4. 达标但超名额 + 5. 低于阈值
    assert report["max_recommendations_effective"] == 3


def test_zero_selection_is_allowed_when_nothing_clears_the_bar(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH", "600001.SH"]),
        probabilities={"600000.SH": 0.3, "600001.SH": 0.55},
    )
    assert report["final_symbols"] == []
    assert report["filled"] == 2
    assert report["final_rejections"]["below_threshold"] == ["600000.SH", "600001.SH"]
    # 确认与成交都过了，只是没达阈值 —— 所以不能出现在确认层的拒绝原因里
    assert "below_threshold" not in report["rejected_reasons"]


def test_same_probability_orders_by_symbol(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600009.SH", "600002.SH"]),
        probabilities={"600009.SH": 0.8, "600002.SH": 0.8},
    )
    assert report["final_symbols"] == ["600002.SH", "600009.SH"]


def test_missing_minute_bars_are_reported_not_replaced_by_open_price(tmp_path) -> None:
    """计划 §5：分钟行情不足 → 记为阻塞，不得用开盘口径顶替。"""
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9}, bars={},
    )
    assert report["rejected_reasons"]["minute_bars_unavailable"] == ["600000.SH"]
    assert report["filled"] == 0
    assert report["final_symbols"] == []


def test_stale_live_snapshot_blocks_confirmation(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9},
        timestamp=datetime(2026, 10, 9, 14, 59),
    )
    assert report["rejected_reasons"]["realtime_snapshot_stale"] == ["600000.SH"]
    assert report["final_symbols"] == []


def test_limit_up_locked_entry_is_recorded_as_no_fill(tmp_path) -> None:
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]),
        probabilities={"600000.SH": 0.9},
        bars={"600000.SH": _bars(lock_at=31)},
    )
    assert report["rejected_reasons"]["limit_up_locked"] == ["600000.SH"]


def test_capital_budget_can_only_tighten_the_cap(tmp_path) -> None:
    pool = _pool(["600000.SH", "600001.SH", "600002.SH"])
    probs = {"600000.SH": 0.9, "600001.SH": 0.85, "600002.SH": 0.8}
    one = _run(tmp_path, watch_pool=pool, probabilities=probs,
               capital_budget=CONTRACT.reference_notional)
    assert one["final_symbols"] == ["600000.SH"]
    assert one["max_recommendations_effective"] == 1

    broke = _run(tmp_path, watch_pool=pool, probabilities=probs, capital_budget=1000.0)
    assert broke["final_symbols"] == []
    assert broke["blocking_reason"] == "capital_budget_exhausted"

    generous = _run(tmp_path, watch_pool=pool, probabilities=probs,
                    capital_budget=10_000_000.0)
    assert len(generous["final_symbols"]) == 3


def test_archive_rows_carry_probability_meaning_and_fill_state(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.77})
    row = report["final_recommendations"][0]
    assert row["probability"] == pytest.approx(0.77)
    assert row["probability_field"] == NET_PROFIT_PROBABILITY_FIELD
    assert row["strategy"] == "trend"
    assert row["reference_notional"] == pytest.approx(10_000.0)
    assert row["contract_digest"] == CONTRACT.digest()
    assert row["data_as_of"].startswith("2026-10-09T14:31")  # 确认 14:30 → 成交 14:31
    assert row["fill"]["filled"] is True and row["fill"]["quantity"] == 1000
    assert row["feature_snapshot"]["excess_ret_20"] == pytest.approx(0.02)
    assert row["caveats"] == []


def test_missing_feature_snapshot_is_a_visible_caveat(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"], features=False),
                  probabilities={"600000.SH": 0.77})
    assert "feature_snapshot_missing" in report["final_recommendations"][0]["caveats"]


def test_shadow_artifacts_are_written_per_trade_date(tmp_path) -> None:
    report = _run(tmp_path, watch_pool=_pool(["600000.SH"]),
                  probabilities={"600000.SH": 0.77})
    paths = report["artifact_paths"]
    trace = json.loads(Path(paths["funnel_trace"]).read_text(encoding="utf-8"))
    assert trace["trade_date"] == "2026-10-09"
    assert trace["stages"][0]["stage"] == "night_watch_pool"
    assert trace["stages"][0]["kind"] == "predictive"
    assert trace["stages"][0]["model_identity"]["identity_recorded"] is True
    assert trace["final_recommendations"][0]["symbol"] == "600000.SH"
    stored = json.loads(Path(paths["shadow_report"]).read_text(encoding="utf-8"))
    assert stored["mode"] == "shadow"


def test_history_mode_requires_an_explicit_trade_date(tmp_path) -> None:
    """历史重算不给交易日就抛错：**不接受 date.today() 兜底**。

    旧实现一边文档写着"历史模式"，一边把留档盖成今天，时间语义静默失真。
    """
    with pytest.raises(TrendContractError):
        _run(tmp_path, watch_pool=_pool(["600000.SH"]),
             probabilities={"600000.SH": 0.9}, timestamp=None)


def test_history_mode_stamps_the_explicit_trade_date(tmp_path) -> None:
    day = date(2026, 9, 18)
    report = _run(
        tmp_path, watch_pool=_pool(["600000.SH"]), timestamp=None, trade_date=day,
        bars={"600000.SH": _bars(day=day)}, probabilities={"600000.SH": 0.9},
    )
    assert report["trade_date"] == "2026-09-18"
    # 历史模式没有时钟，数据时间只能取最后一根已完成 bar，而不是"今天"。
    assert report["data_as_of"] == "2026-09-18T14:44:00"
    stored = Path(report["artifact_paths"]["shadow_report"])
    assert stored.name == "tail_shadow_report_2026-09-18.json"


def test_live_and_history_chain_paths_agree_on_identical_bars(tmp_path) -> None:
    """§4：同一份输入，线上与历史两条路径给出一致的筛选与交易判定。

    比的是判定本身（谁入选、成交多少、谁被为什么挡下），不是留档时间戳——两条路径
    的数据时间本就不同：线上读时钟，历史读最后一根已完成 bar。
    """
    pool = _pool(["600000.SH", "600001.SH", "600002.SH"])
    probabilities = {"600000.SH": 0.9, "600001.SH": 0.8, "600002.SH": 0.55}

    def _bars_on(target: date) -> dict:
        return {row["symbol"]: _bars(day=target) for row in pool}

    def _fingerprint(report: dict) -> dict:
        return {
            "final_symbols": report["final_symbols"],
            "counts": report["counts"],
            "confirmed": report["confirmed"],
            "filled": report["filled"],
            "rejected_reasons": report["rejected_reasons"],
            "final_rejections": report["final_rejections"],
            "blocking_reason": report["blocking_reason"],
            "trades": [
                (row["symbol"], row["fill"]["filled"], row["fill"]["quantity"],
                 row["fill"]["entry_amount"], row["fill"]["fill_time"][11:])
                for row in report["final_recommendations"]
            ],
        }

    live = _run(tmp_path / "live", watch_pool=pool, probabilities=probabilities,
                bars=_bars_on(NOW.date()))
    history = _run(tmp_path / "history", watch_pool=pool, probabilities=probabilities,
                   bars=_bars_on(date(2026, 9, 18)), timestamp=None,
                   trade_date=date(2026, 9, 18))

    assert _fingerprint(history) == _fingerprint(live)
    assert live["final_symbols"] == ["600000.SH", "600001.SH"]
    assert live["data_as_of"] == "2026-10-09T14:45:00"
    assert history["data_as_of"] == "2026-09-18T14:44:00"


# ---------------------------------------------------------------------------
# 接线守卫：影子链路必须在 live runtime 里被调用，且不动 actionable_signals
# ---------------------------------------------------------------------------

AUTOMATION_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "src/stock_analyzer/runtime/services/week5_automation_service.py"
)


def _function_source(name: str) -> str:
    tree = ast.parse(AUTOMATION_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(
                AUTOMATION_SOURCE.read_text(encoding="utf-8"), node
            ) or ""
    raise AssertionError(f"{name} not found in week5_automation_service.py")


def test_live_runtime_calls_the_tail_shadow_service() -> None:
    body = _function_source("run_live_runtime")
    assert "_trend_tail_shadow_report(" in body
    assert '"trend_tail_shadow"' in body


def test_tail_shadow_never_touches_the_legacy_actionable_list() -> None:
    """影子就是影子：接线点只能新增键，不能改写 actionable_signals。"""
    body = _function_source("run_live_runtime")
    tail_index = body.index("_trend_tail_shadow_report(")
    assert 'actionable = ' not in body[tail_index:]
    assert 'report["actionable_signals"]' not in body[tail_index:]


def test_shadow_failures_are_reported_not_swallowed() -> None:
    body = _function_source("_trend_tail_shadow_report")
    assert '"ok": False' in body and '"error":' in body
