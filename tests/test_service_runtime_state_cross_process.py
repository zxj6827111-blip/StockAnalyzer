"""runtime_state history sidecar 读缓存 / 跳写在真实 StockAnalyzerService 上的
跨进程端到端回归测试。

背景：提交 ``607a41f`` 给 sidecar 读侧加了 ``(st_mtime_ns, st_size)`` 读缓存，
给写侧加了「内容未变跳过重写」的逻辑。单元测试
（``tests/test_service_runtime_state_sidecar_cache.py``）已用最小 stub 覆盖了
读写函数本身，但没有验证「两个真实 service 实例指向同一 runtime_state 路径」
时缓存会不会导致跨进程状态丢失或读到过期数据——这正是本次改动最容易踩雷的
地方（读缓存漏检文件变化 = 读到过期数据）。

这里用 ``tests/test_service_runtime_state_persistence.py`` 里已经跑通的
``_load_test_config`` / ``_new_service`` 隔离方式，构造 A/B/C 三个 service 实例，
模拟 api 与 scheduler 两个进程共享同一状态文件，覆盖任务要求的四类场景：

1. 跨进程可见性：B 写入历史并落盘，A 重载后必须读到 B 写的记录；
2. 反复重载不丢数据：A 多次重载（有的文件未变、有的 B 又写了新记录）都要正确；
3. 跳写不影响持久化语义：A 落盘两次（第二次内容未变），全新实例 C 从磁盘加载
   必须与 A 内存一致；
4. history_limit 收敛后的截断行为：写超过 limit 条后落盘并重载，保留最新 limit 条。

真实 service 上，其它进程改写 ``runtime_state.json`` 会把主文件 mtime 推进，
读侧 ``sla_report`` / ``audit_events`` 会经
``_refresh_runtime_state_from_disk_if_changed`` 触发全量重载，重载再经带缓存的
``_load_runtime_state_history_sidecar`` 读取各 sidecar。因此用这些公开只读接口
就能端到端地把「跨进程写 -> 读缓存」这条链路跑通。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from stock_analyzer.runtime.service import StockAnalyzerService

# 直接复用持久化测试里的隔离辅助，保持与既有测试完全一致的构造方式。
from tests.test_service_runtime_state_persistence import (  # noqa: E402
    _load_test_config,
    _new_service,
)


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


# 每次 seed 递增计数，用于生成不重复的时间戳 / duration。
# 因为 latency_history_ms sidecar 的身份键是 (timestamp, duration_ms)，
# run_summaries 是 (timestamp, trace_id)，若多条记录时间戳、duration 全相同会被
# 合并去重折叠成一条——那是测试构造不当，而非被测缓存的问题。
_SEED_COUNTER = {"value": 0}


def _seed_audit_run(service: StockAnalyzerService, *, trace_id: str) -> None:
    """在 service 内存中造一条 run summary + 一条 audit 事件并落盘。

    - run summary 会进 ``run_summaries`` sidecar，支撑 ``sla_report``；
    - audit 事件会进 ``audit_events`` sidecar，支撑 ``audit_events``；
    - run summary 同时会写 ``latency_history_ms`` sidecar，``sla_report`` 的
      ``recent_runs`` 实际统计的就是它。
    这些都是本次改动缓存的 sidecar，读侧一旦漏检文件变化就会读到过期数据。

    每次调用用递增的秒数与 duration，保证 run/latency 记录不会因身份键相同而被
    ``_merge_runtime_state_history`` 去重折叠。
    """
    _SEED_COUNTER["value"] += 1
    seq = _SEED_COUNTER["value"]
    timestamp = f"2026-03-01T20:25:{seq % 60:02d}"
    report = {
        "trace_id": trace_id,
        "timestamp": timestamp,
        "risk": {"action": "monitor", "drawdown_pct": 0.0},
        "signals": [{"symbol": "600000"}],
        "actionable_signals": [{"symbol": "600000"}],
    }
    service._record_run_summary(  # noqa: SLF001
        report=report,
        current_equity=1.0,
        actionable_count=1,
        duration_ms=10 + seq,
    )
    service._record_audit_event(  # noqa: SLF001
        event_type="pipeline_run",
        trace_id=trace_id,
        payload={"strategy": "trend", "symbols": ["600000"]},
    )


def _audit_trace_ids(service: StockAnalyzerService, *, limit: int = 200) -> set[str]:
    """经公开只读接口读取 audit 事件的 trace_id 集合。

    该接口内部会先调用 ``_refresh_runtime_state_from_disk_if_changed``，因此这是
    模拟「另一个进程发起一次读请求」最贴近真实运行时的入口。
    """
    events = _as_mapping(service.audit_events(limit=limit))
    items = events["events"]
    assert isinstance(items, list)
    result: set[str] = set()
    for item in items:
        if isinstance(item, Mapping):
            result.add(str(item.get("trace_id", "")))
    return result


def test_cross_process_new_history_is_visible_after_reload(tmp_path: Path) -> None:
    """场景 1：B 写入的新历史记录，A 重载后必须能读到。

    这是本次改动最关键的回归点：如果 A 的读缓存漏检 sidecar 文件变化，A 就会
    继续返回旧内容，B 写的记录对 A 不可见，本测试即失败。
    """
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    # A 先写一条并落盘，同时把结果读进 A 自己的读缓存。
    _seed_audit_run(service_a, trace_id="trace-A-initial")
    assert "trace-A-initial" in _audit_trace_ids(service_a)

    # B（另一个进程）写入新记录并落盘 —— 主文件 mtime 与 sidecar 均前进。
    _seed_audit_run(service_b, trace_id="trace-B-new")

    # A 经公开只读接口触发重载：必须能看到 B 写的记录，且旧记录仍在。
    a_view = _audit_trace_ids(service_a)
    assert "trace-B-new" in a_view, "读缓存漏检文件变化会导致 B 的记录对 A 不可见"
    assert "trace-A-initial" in a_view

    sla = _as_mapping(service_a.sla_report(recent_runs=50))
    assert _as_int(sla["recent_runs"]) >= 2


def test_cross_process_repeated_reload_never_loses_data(tmp_path: Path) -> None:
    """场景 2：A 反复重载，其中若干次文件未变、若干次 B 又写了新记录，
    每次 A 看到的历史都必须是当时磁盘上的全集。
    """
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    _seed_audit_run(service_a, trace_id="trace-round-0")
    expected: set[str] = {"trace-round-0"}
    assert expected.issubset(_audit_trace_ids(service_a))

    for round_index in range(1, 6):
        if round_index % 2 == 1:
            # 奇数轮：B 写新记录，A 必须看到增量。
            trace_id = f"trace-round-{round_index}"
            _seed_audit_run(service_b, trace_id=trace_id)
            expected.add(trace_id)
        # 偶数轮：磁盘未变，A 仍应看到已有全集（命中缓存也不能丢数据）。
        observed = _audit_trace_ids(service_a)
        assert expected.issubset(observed), (
            f"第 {round_index} 轮重载丢失记录，期望包含 {expected}，实际 {observed}"
        )


def test_skip_rewrite_preserves_persistence_semantics_for_fresh_process(
    tmp_path: Path,
) -> None:
    """场景 3：A 连续落盘两次（第二次内容未变，触发写侧跳写），
    随后全新实例 C 从磁盘加载，C 看到的历史必须与 A 内存一致。
    """
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)

    _seed_audit_run(service_a, trace_id="trace-skip-write")
    # 第二次落盘时 sidecar 内容与第一次完全一致 —— 命中「内容未变跳过重写」。
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001

    a_view = _audit_trace_ids(service_a)
    assert "trace-skip-write" in a_view

    # 全新进程 C：只从磁盘加载，不共享 A 的任何内存/缓存。
    service_c = _new_service(config)
    c_view = _audit_trace_ids(service_c)
    assert c_view == a_view, "跳写后全新实例从磁盘加载的历史必须与 A 内存一致"
    assert "trace-skip-write" in c_view


def test_history_limit_truncation_keeps_latest_after_reload(tmp_path: Path) -> None:
    """场景 4：写入超过 limit 条记录，落盘并重载后必须只保留最新的 limit 条。

    用 reconcile_history 验证截断：它的 sidecar limit 直接来自
    ``config.reconcile.history_limit``，把它调小即可在少量记录下触发截断，避免
    造几千条 run summary。
    """
    config = _load_test_config(tmp_path)
    limit = 5
    config.reconcile.history_limit = limit
    service_a = _new_service(config)

    total = limit + 4  # 故意超出 limit，制造需要被截断的记录
    records = [
        {
            "timestamp": f"2026-03-01T20:{index:02d}:00",
            "status": "ok",
            "trace_id": f"reconcile-{index:03d}",
        }
        for index in range(total)
    ]
    _patch_attr(service_a, "_reconcile_history", list(records))
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001

    # 全新实例 C 从磁盘加载，只应保留最新的 limit 条（尾部 limit 条）。
    service_c = _new_service(config)
    reloaded = list(service_c._reconcile_history)  # noqa: SLF001
    assert len(reloaded) == limit, f"截断后应只剩 {limit} 条，实际 {len(reloaded)} 条"
    reloaded_traces = [str(item.get("trace_id", "")) for item in reloaded]
    expected_traces = [f"reconcile-{index:03d}" for index in range(total - limit, total)]
    assert reloaded_traces == expected_traces, "保留的必须是最新的 limit 条且顺序正确"
