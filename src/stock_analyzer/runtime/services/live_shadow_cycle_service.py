"""M4-L：Alpha V2 生产影子循环的运行时胶水（runtime 侧唯一的 V2 flag 消费者）。

为什么独立成模块
----------------

``tests/test_alpha_v2_baseline.py`` 的架构守卫要求生产入口
（``main.py`` / ``pipeline.py`` / ``runtime/service.py``）**结构上零提及** V2：
"V2 关闭 = 零行为变化"要是结构性事实，而不是被测试出来的巧合。M4-L 又必须在
正式调度器上挂一个 flag-gated 的每日循环，两者调和方式只有一种：

- 入口侧只留一次**中性委托**（``self._live_shadow_cycle.registration()``），
  字面上不出现 ``alpha_v2``；
- 全部 flag 读取、工件读写、子进程编排集中在本模块；
- 本模块显式登记进 ``_ALPHA_V2_FLAG_CONSUMERS`` 白名单——**新增消费者 =
  可审查动作**，评审时一眼能看到"runtime 里到底谁在碰 V2"。

职责
----

1. ``registration()``：alpha_v2.enabled=true（且 shadow_only、未 enforce）时
   返回调度注册规格（窗口/间隔/回调），否则返回 None（不注册，行为与旧版一致）；
2. ``emit_funnel_from_scan_report()``：夜扫跑完 snapshot_funnel 后落生产漏斗工件；
3. ``link_published_report()``：晚报发布后把 report_id + 报告 sha256 链进漏斗；
4. ``run_daily_cycle()``：每日循环 —— data_health → capture → mature → KPI，
   依赖驱动、跨零点前必须完成、无 epoch 安全跳过、幂等、失败留痕。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from stock_analyzer.alpha_v2.validation.production_funnel import (
    FunnelNotFoundError,
    _published_report_id,
    build_source_evidence,
    emit_funnel_snapshot,
    extract_funnel_from_source_evidence,
    file_sha256,
    funnel_snapshot_path,
    link_funnel_to_report,
    load_funnel_snapshot,
    write_source_evidence,
)

JOB_NAME = "alpha_v2_shadow_cycle"
_CYCLE_STEPS: tuple[tuple[str, str, int], ...] = (
    ("data_health", "alpha_v2_data_health_snapshot.py", 300),
    ("capture", "alpha_v2_shadow_capture.py", 3600),
    ("mature", "alpha_v2_shadow_mature.py", 5400),
    ("report", "alpha_v2_validation_report.py", 600),
)


class LiveShadowCycleService:
    """把所有"runtime 碰 V2"的动作收在这一个类里（见模块 docstring）。"""

    def __init__(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------------
    # 配置读取（本模块是 runtime 侧唯一读 V2 flag 的地方）
    # ------------------------------------------------------------------

    def _alpha_config(self) -> Any:
        # 直接属性读取（而不是 getattr + 字符串）：让"本模块消费 V2 flag"在
        # 文本上可被架构守卫看见，从而必须显式登记白名单——评审可见性优先于
        # 一丁点防御性写法。
        #
        # 但**部分构造的测试替身**（StockAnalyzerService.__new__ 注入最小属性）
        # 可能没有 alpha_v2 块：此时按"未启用"处理（no-op），绝不在夜扫主链路上
        # 抛 AttributeError。
        try:
            return self._service._config.alpha_v2
        except AttributeError:
            return None

    def enabled(self) -> bool:
        alpha_cfg = self._alpha_config()
        if alpha_cfg is None or not bool(getattr(alpha_cfg, "enabled", False)):
            return False
        # 防御式：enabled 且 shadow_only 才允许跑影子循环（config 模型已强校验，
        # 这里再核一次，避免直接改对象绕过校验器的路径）。
        if not bool(getattr(alpha_cfg, "shadow_only", True)):
            return False
        return not bool(getattr(alpha_cfg, "enforce_final_selection", False))

    def repo_root(self) -> Path:
        """仓库根：向上找同时含 ``scripts/`` 与 ``src/`` 的目录。

        不能写死 ``parents[N]``——本模块在 ``runtime/services/`` 下（比
        ``runtime/service.py`` 深一层），写死层级会把 scripts 路径指到 ``src/scripts``
        （外部复核 R1 实测：data_health 子进程直接 FileNotFoundError）。
        """
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "scripts").is_dir() and (parent / "src").is_dir():
                return parent
        return here.parents[4]

    def funnel_root(self) -> Path:
        alpha_cfg = self._alpha_config()
        relative = str(getattr(alpha_cfg, "production_funnel_root", "")).strip()
        return self.repo_root() / relative

    def artifact_root(self) -> Path:
        alpha_cfg = self._alpha_config()
        return Path(str(getattr(alpha_cfg, "artifact_root", "artifacts/alpha_v2")))

    def market_db_path(self) -> str:
        """行情库路径：取 ``config.market_warehouse.db_path``（生产 NAS 是 delta 库，
        不是仓库默认的 artifacts/warehouse/market.duckdb——写死会让整个循环读错库）。

        这是 **feature 侧**（qfq）行情库：capture 产特征、跑模型，用的就是它。
        """
        config = self._service._config
        return str(getattr(config.market_warehouse, "db_path", "artifacts/warehouse/market.duckdb"))

    def feature_market_db_path(self) -> str:
        """feature 侧行情库：``alpha_v2.feature_market_db`` 为空时回退 market_warehouse。"""
        configured = str(
            getattr(self._alpha_config(), "feature_market_db", "") or ""
        ).strip()
        return configured or self.market_db_path()

    def execution_market_db_path(self) -> str:
        """execution 侧行情库（**必须 raw**）。

        留空表示未配置：mature 会 fail closed（exit 4）而不是拿 qfq 当成交价
        ——P0 双价格序列契约里，这条路径宁可不产出 outcome，也不产出错口径的 outcome。
        """
        alpha_cfg = self._alpha_config()
        return str(getattr(alpha_cfg, "execution_market_db", "") or "").strip()

    def data_health_out(self) -> Path:
        """data_health 落点：``<alpha_root>/runtime/data_health.json``。

        放 alpha 根下（而不是 artifacts/runtime）有两个理由：① capture 的工件查找
        候选里就有 ``<root>/runtime/data_health.json``（root=alpha 根），写这里必然
        被读到；② 影子证据全部留在 shadow 树内，不和生产目录混。
        """
        return self.artifact_root() / "runtime" / "data_health.json"

    # ------------------------------------------------------------------
    # 1) 调度注册
    # ------------------------------------------------------------------

    def registration(self) -> dict[str, object] | None:
        """返回 ``register_interval`` 所需规格；未启用则 None（不注册）。"""
        if not self.enabled():
            return None
        alpha_cfg = self._alpha_config()
        return {
            "name": JOB_NAME,
            "start": str(alpha_cfg.live_cycle_start_time),
            "latest": str(alpha_cfg.live_cycle_latest_time),
            "interval": max(1, int(alpha_cfg.live_cycle_interval_minutes)),
            "callback": self.run_daily_cycle,
        }

    # ------------------------------------------------------------------
    # 2) 夜扫 → 生产漏斗工件
    # ------------------------------------------------------------------

    def emit_funnel_from_scan_report(
        self,
        *,
        report: dict[str, object],
        trade_date: datetime,
        trace_id: str,
    ) -> None:
        """夜扫拿到终态结果后落盘当日生产漏斗（失败只记审计，绝不影响选股）。"""
        if not self.enabled():
            return
        funnel_block = report.get("funnel")
        prefilter = report.get("prefilter")
        if not isinstance(funnel_block, dict) or not isinstance(prefilter, dict):
            return
        if str(funnel_block.get("policy", "")).strip() != "snapshot_funnel":
            return
        if not bool(funnel_block.get("deep_stage_ran", False)):
            return
        try:
            day = trade_date.date().isoformat()
            # R1（BLOCKER 7）：先把成员**原文**落成不可变 source evidence，
            # 再从它抽取 funnel——source_night_scan_artifact_sha256 因此真的指向
            # "成员从哪来"，而不是指向只存 counts 的正式晚报。
            evidence = build_source_evidence(
                source_report=report,
                trade_date=day,
                trace_id=trace_id,
                created_at=trade_date.isoformat(),
            )
            evidence_path = write_source_evidence(
                funnel_root=self.funnel_root(), payload=evidence
            )
            payload = extract_funnel_from_source_evidence(
                evidence,
                source_artifact_path=str(evidence_path),
                # 用**文件字节哈希**而不是字典规范化哈希：读侧重算文件哈希对账。
                source_artifact_sha256=file_sha256(evidence_path),
                signal_date=day,
                trade_date=day,
            )
            path = emit_funnel_snapshot(funnel_root=self.funnel_root(), payload=payload)
            self._audit(
                event_type="alpha_v2_production_funnel_emitted",
                level="info",
                trace_id=trace_id,
                payload={
                    "trade_date": day,
                    "path": str(path),
                    "quality_count": payload["quality_count"],
                    "light_count": payload["light_count"],
                    "deep_count": payload["deep_count"],
                    "selector_mode": payload["selector_mode"],
                    "funnel_snapshot_hash": payload["funnel_snapshot_hash"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - 影子证据链事故不得炸掉选股
            self._audit(
                event_type="alpha_v2_production_funnel_emit_failed",
                level="error" if exc.__class__.__name__ == "FunnelTamperError" else "warn",
                trace_id=trace_id,
                payload={"error": f"{exc.__class__.__name__}: {exc}"},
            )

    # ------------------------------------------------------------------
    # 3) 晚报发布 → 链接
    # ------------------------------------------------------------------

    def link_published_report(
        self,
        *,
        trade_date: str,
        report_id: str,
        report_service: Any,
    ) -> None:
        """正式晚报发布后把 report_id + 报告文件 sha256 链进 funnel（不可逆）。"""
        if not self.enabled() or not report_id:
            return
        try:
            report_path = report_service.report_path(trade_date, report_id)
            path = link_funnel_to_report(
                funnel_root=self.funnel_root(),
                trade_date=trade_date,
                report_id=report_id,
                report_path=report_path,
            )
            self._audit(
                event_type="alpha_v2_production_funnel_linked",
                level="info",
                payload={
                    "trade_date": trade_date,
                    "report_id": report_id,
                    "funnel_path": str(path),
                },
            )
        except FunnelNotFoundError:
            # 夜扫被门拦 / 非 snapshot_funnel 的当天本来就没有 funnel：正常空路径。
            self._audit(
                event_type="alpha_v2_production_funnel_link_skipped",
                level="info",
                payload={
                    "trade_date": trade_date,
                    "report_id": report_id,
                    "reason": "funnel_artifact_absent",
                },
            )
        except Exception as exc:  # noqa: BLE001 - 证据链事故不得打断晚报发布
            self._audit(
                event_type="alpha_v2_production_funnel_link_failed",
                level="error",
                payload={
                    "trade_date": trade_date,
                    "report_id": report_id,
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
            )

    # ------------------------------------------------------------------
    # 4) 每日循环（scheduler job）
    # ------------------------------------------------------------------

    def run_daily_cycle(self) -> dict[str, object]:
        """Alpha V2 每日正式循环：data_health → capture → mature → KPI 报告。

        纪律（M4-L §14-§19）：

        - **依赖驱动**：三个前置全部满足才起捕获——
          (a) nightly data ready、(b) 当天 funnel 已链接正式晚报、
          (c) **当天 data_health 生成并通过 gate（status=ok 且 as_of 同日）**；
          未就绪时快速返回 waiting（不计失败），窗口内每个槽位重试。
          R1 起 data_health 由本循环自己派生（``--derive-inputs``）并**读取验证**——
          绝不在 degraded 上写 immutable 快照（那样即使数据随后变齐，当天也永远
          不可能成为 clean OOS）；
        - **无 active epoch = safe skip**：Alpha V2 尚未启动阶段不破坏生产调度；
        - active epoch + 交易日 + 到窗口末尾仍未就绪 = 明确 failure/audit，并落
          missing 台账（该日永不计 clean OOS，绝不静默成功）；此时**仍然推进
          历史日的 mature 与 KPI**（missing day 只影响它自己，不阻断既有权重日
          的 3/5/10/15D 成熟）；
        - **幂等**：当天已有快照 + KPI 报告 → already_completed，重复调度不重复写。
        """
        current = self._job_now()
        trade_date = current.date()
        if not self.enabled():
            return self._result(True, "alpha_v2_disabled", trade_date)

        from stock_analyzer.alpha_v2.validation.epoch import active_epoch

        root = self.artifact_root()
        try:
            epoch = active_epoch(root)
        except Exception as exc:  # noqa: BLE001 - 注册表损坏也不得炸调度
            self._audit(
                event_type="alpha_v2_cycle_epoch_registry_error",
                level="error",
                payload={"error": f"{exc.__class__.__name__}: {exc}"},
            )
            return self._result(False, "alpha_v2_epoch_registry_error", trade_date)
        if epoch is None:
            return self._result(True, "alpha_v2_no_active_epoch", trade_date)

        state = self._cycle_state(root=root, epoch_id=epoch.epoch_id, trade_date=trade_date)
        if bool(state["captured"]) and bool(state["reported"]):
            return {
                **self._result(True, "alpha_v2_already_completed", trade_date),
                "validation_epoch_id": epoch.epoch_id,
            }

        # P1 R1（BLOCKER）：**active epoch 下必须要求双 delta readiness**。
        # epoch 的 label / 成交价 / 净收益 / 超额 / MAE-MFE 全部取自 execution/raw 库，
        # 而 readiness 的默认档为了 Legacy/Week5 向后兼容允许 v2（只有 feature delta）。
        # 沿用默认档就等于"在没有执行侧证据的晚上照记 clean day"，而 clean OOS 天数正是
        # epoch 的验收凭据——所以这里显式升到严格档；v2 一律不 ready，原因码
        # nightly_dual_delta_not_ready，走既有的 waiting → deadline missing 纪律。
        readiness = self._service._week5_automation_service.probe_nightly_readiness(
            require_dual_delta=True
        )
        funnel_ready, funnel_reason = self._funnel_ready(trade_date=trade_date)
        health_ready = True
        health_reason = ""
        health_summary: dict[str, object] = {}
        if not bool(state["captured"]):
            # R1（BLOCKER 2）：先生成当天 data_health，再**读取验证**——
            # CLI 的 returncode==0 不代表健康（degraded 也是 0）。
            health_ready, health_reason, health_summary = self._ensure_data_health(
                trade_date=trade_date
            )
        # P0 Final R1（BLOCKER 2）：**捕获前**复核 feature 价格口径是否仍等于冻结值。
        # 没有这道前置，口径漂移只会在 capture 里以 exit 11 出现，调度器便一路
        # alpha_v2_step_failed:capture:11 —— 到 23:55 也不会记 missing day。
        feature_mode_ready = True
        feature_mode_reason = ""
        feature_mode_summary: dict[str, object] = {}
        if not bool(state["captured"]):
            feature_mode_ready, feature_mode_reason, feature_mode_summary = (
                self._ensure_feature_price_series(root=root, trade_date=trade_date)
            )
        prerequisites_ok = bool(
            bool(readiness.get("allowed", False))
            and funnel_ready
            and health_ready
            and feature_mode_ready
        )
        if not bool(state["captured"]) and not prerequisites_ok:
            if not health_ready:
                reason = health_reason or "data_health_not_healthy"
            elif not feature_mode_ready:
                reason = feature_mode_reason or "feature_price_mode_not_ready"
            else:
                reason = funnel_reason or str(readiness.get("reason", "") or "readiness_blocked")
            if current.time() >= self._deadline():
                # 到窗口末尾仍未就绪：不写当日快照，落 missing 台账 + 审计；
                # 但**继续推进历史日的成熟与 KPI**（§9：missing 只约束它自己）。
                tail = self._run_history_tail(
                    root=root, epoch_id=epoch.epoch_id, trade_date=trade_date
                )
                self._record_missing_day(
                    root=root,
                    epoch_id=epoch.epoch_id,
                    trade_date=trade_date,
                    reason=f"production_prerequisites_unavailable:{reason}",
                )
                self._audit(
                    event_type="alpha_v2_cycle_blocked_day",
                    level="error",
                    payload={
                        "trade_date": trade_date.isoformat(),
                        "reason": reason,
                        "readiness": dict(readiness),
                        "funnel_ready": funnel_ready,
                        "data_health": health_summary,
                        "feature_price_series": feature_mode_summary,
                        "history_tail": tail,
                    },
                )
                return {
                    **self._result(
                        True, f"alpha_v2_blocked_recorded_missing:{reason}", trade_date
                    ),
                    "missing_recorded": True,
                    "history_tail": tail,
                }
            return {
                **self._result(True, f"alpha_v2_waiting:{reason}", trade_date),
                # 等待态也带出证据（排障不必再去翻工件）
                "data_health": health_summary,
                "feature_price_series": feature_mode_summary,
            }

        steps: list[tuple[str, list[str], int]] = []
        if not bool(state["captured"]):
            steps.extend(
                [
                    (
                        "capture",
                        [
                            "--epoch-id",
                            epoch.epoch_id,
                            "--signal-date",
                            trade_date.isoformat(),
                            # P0 Final R1：capture 读的是 feature 侧权威配置
                            # （alpha_v2.feature_market_db 为空时回退 market_warehouse），
                            # 不再直接用 db_path——两者可以是不同的库。
                            "--market-db",
                            self.feature_market_db_path(),
                            "--out",
                            str(root),
                            "--cohort-source",
                            "production_funnel",
                            "--quality-pool-source",
                            "production_selection_engine",
                        ],
                        3600,
                    ),
                ]
            )
        steps.extend(
            [
                (
                    "mature",
                    # P0 双价格序列：mature 的两个角色**分开**给——execution 必须 raw，
                    # feature 只供风格维度。一个 --market-db 走到底正是本 P0 的成因。
                    [
                        "--epoch-id",
                        epoch.epoch_id,
                        "--evaluation-date",
                        trade_date.isoformat(),
                        "--execution-market-db",
                        self.execution_market_db_path(),
                        "--feature-market-db",
                        self.feature_market_db_path(),
                        "--out",
                        str(root),
                    ],
                    5400,
                ),
                (
                    "report",
                    ["--epoch-id", epoch.epoch_id, "--out", str(root)],
                    600,
                ),
            ]
        )
        results: list[dict[str, object]] = []
        for step_name, argv, timeout_sec in steps:
            script = {name: script for name, script, _ in _CYCLE_STEPS}[step_name]
            try:
                returncode, tail = self._run_cli(script, argv, timeout_sec=timeout_sec)
            except Exception as exc:  # noqa: BLE001 - 子进程启动/超时都要有明确结论
                returncode, tail = -1, f"{exc.__class__.__name__}: {exc}"
            results.append(
                {"step": step_name, "returncode": returncode, "tail": tail[-800:]}
            )
            if returncode != 0:
                self._audit(
                    event_type="alpha_v2_cycle_step_failed",
                    level="error",
                    payload={
                        "trade_date": trade_date.isoformat(),
                        "step": step_name,
                        "returncode": returncode,
                        "tail": tail[-1500:],
                    },
                )
                return {
                    **self._result(
                        False, f"alpha_v2_step_failed:{step_name}:{returncode}", trade_date
                    ),
                    "steps": results,
                }
        self._audit(
            event_type="alpha_v2_cycle_completed",
            level="info",
            payload={
                "trade_date": trade_date.isoformat(),
                "validation_epoch_id": epoch.epoch_id,
                "steps": [item["step"] for item in results],
                "capture_ran": not bool(state["captured"]),
            },
        )
        return {
            **self._result(True, "alpha_v2_cycle_completed", trade_date),
            "validation_epoch_id": epoch.epoch_id,
            "steps": results,
        }


    # ------------------------------------------------------------------
    # data_health：生成 + 读取验证（R1 BLOCKER 1/2）
    # ------------------------------------------------------------------

    def _ensure_data_health(
        self, *, trade_date: date
    ) -> tuple[bool, str, dict[str, object]]:
        """生成当天 data_health 工件并**读取验证**它真的可用于 clean OOS。

        顺序（外部复核 §1.2 要求）：

        ```text
        派生并写 data_health  ->  读回工件  ->  capture gate（status=ok 且 as_of 同日）
        ```

        只有 gate 通过才返回 True。CLI ``returncode==0`` 不作为证据：degraded 也返回 0。
        """
        from stock_analyzer.alpha_v2.validation.data_health_capture import (
            capture_data_health_block,
            data_health_gate_ok,
            load_data_health_artifact,
        )

        returncode, tail = -1, ""
        try:
            returncode, tail = self._run_cli(
                "alpha_v2_data_health_snapshot.py",
                [
                    "--as-of",
                    trade_date.isoformat(),
                    "--market-db",
                    self.market_db_path(),
                    "--out",
                    str(self.data_health_out()),
                    "--alpha-v2-root",
                    str(self.artifact_root()),
                    "--derive-inputs",
                ],
                timeout_sec=300,
            )
        except Exception as exc:  # noqa: BLE001 - 超时/启动失败都算"未就绪"
            tail = f"{exc.__class__.__name__}: {exc}"
        summary: dict[str, object] = {
            "cli_returncode": returncode,
            "cli_tail": tail[-500:],
        }
        payload, source = load_data_health_artifact(
            path=self.data_health_out(), root=self.artifact_root()
        )
        block = capture_data_health_block(
            signal_date=trade_date, payload=payload, source=source
        )
        summary["block"] = {
            "status": block.get("status"),
            "source_status": block.get("source_status"),
            "as_of": block.get("as_of"),
            "source": block.get("source"),
        }
        if payload:
            summary["broken_checks"] = list(payload.get("broken_checks", []) or [])
            summary["degraded_checks"] = list(payload.get("degraded_checks", []) or [])
            summary["missing_artifacts"] = list(payload.get("missing_artifacts", []) or [])
        gate_ok, reason = data_health_gate_ok(block, trade_date)
        if not gate_ok:
            return False, f"data_health_not_healthy:{reason or 'unknown'}", summary
        return True, "", summary

    def _run_history_tail(
        self, *, root: Path, epoch_id: str, trade_date: date
    ) -> list[dict[str, object]]:
        """当天无法捕获时，仍推进历史日的 mature 与 KPI（§9）。

        missing day 只约束它自己：既有权重日的 3/5/10/15D 成熟不能被当天失败拖停。
        失败只记审计（tail 结果原样返回供审批示）。
        """
        results: list[dict[str, object]] = []
        for step_name in ("mature", "report"):
            script = {name: script for name, script, _ in _CYCLE_STEPS}[step_name]
            argv = (
                [
                    "--epoch-id",
                    epoch_id,
                    "--evaluation-date",
                    trade_date.isoformat(),
                    "--execution-market-db",
                    self.execution_market_db_path(),
                    "--feature-market-db",
                    self.feature_market_db_path(),
                    "--out",
                    str(root),
                ]
                if step_name == "mature"
                else ["--epoch-id", epoch_id, "--out", str(root)]
            )
            timeout_sec = {name: timeout for name, _, timeout in _CYCLE_STEPS}[step_name]
            try:
                returncode, tail = self._run_cli(script, argv, timeout_sec=timeout_sec)
            except Exception as exc:  # noqa: BLE001
                returncode, tail = -1, f"{exc.__class__.__name__}: {exc}"
            results.append(
                {"step": step_name, "returncode": returncode, "tail": tail[-400:]}
            )
            if returncode != 0:
                self._audit(
                    event_type="alpha_v2_history_tail_step_failed",
                    level="warn",
                    payload={
                        "trade_date": trade_date.isoformat(),
                        "step": step_name,
                        "returncode": returncode,
                    },
                )
        return results

    def _ensure_feature_price_series(
        self, *, root: Path, trade_date: date
    ) -> tuple[bool, str, dict[str, object]]:
        """捕获前的 feature 价格口径前置门（P0 Final R1 / BLOCKER 2）。

        期望值只能来自**冻结身份**：``freeze.model.provenance.feature_data_identity.
        price_series_mode``（受 ``freeze_manifest_hash`` 锚定）——不读当前 config 的
        ``vendor_zip_price_series_mode`` 猜，那样"训练 qfq、线上被改成 raw"会静默通过。

        判据复用唯一实现：``live_data_health_inputs.derive_feature_price_series_input``
        → ``preflight.probe_price_series_mode`` + ``require_declared_feature_series``。

        返回 ``(ok, reason, evidence)``：未就绪时调用方按 waiting / missing 处理——
        绝不让 capture 以 exit 11 反复失败到窗口结束。
        """
        from stock_analyzer.alpha_v2.dual_price_series import (
            feature_mode_of_freeze_manifest,
            is_live_strict_mode,
        )
        from stock_analyzer.alpha_v2.validation.freeze import load_validation_freeze
        from stock_analyzer.alpha_v2.validation.live_data_health_inputs import (
            derive_feature_price_series_input,
        )

        try:
            freeze = load_validation_freeze(root) or {}
        except Exception as exc:  # noqa: BLE001 - 清单损坏按"未就绪"，不炸调度
            return False, f"freeze_manifest_unreadable:{exc.__class__.__name__}", {}
        validation_mode = str(freeze.get("validation_mode", "production"))
        strict = bool(is_live_strict_mode(validation_mode))
        expected_mode = feature_mode_of_freeze_manifest(freeze)
        market_db = self.feature_market_db_path()
        evidence: dict[str, object] = {
            "validation_mode": validation_mode,
            "enforced": strict,
            "expected_mode": expected_mode,
            "feature_market_db": market_db,
        }
        if not market_db:
            evidence["status"] = "feature_market_db_not_configured"
            evidence["reason"] = "未配置 feature 行情库（alpha_v2.feature_market_db）"
            return False, "feature_market_db_not_configured", evidence
        if not expected_mode:
            evidence["status"] = "expected_feature_mode_missing"
            evidence["reason"] = (
                "冻结清单的模型块未声明 feature 数据身份"
                "（model.provenance.feature_data_identity.price_series_mode）"
            )
            if strict:
                return False, "frozen_feature_mode_missing", evidence
            # 非严格模式（rehearsal）：没有可比的冻结口径就不做这层探测——
            # 该 epoch 的行恒 clean_oos_eligible=false，证据已如实标未声明/未强制。
            evidence["contract_ok"] = False
            return True, "", evidence
        probe = derive_feature_price_series_input(
            market_db=market_db,
            expected_mode=expected_mode,
            as_of=trade_date,
        )
        evidence.update(probe)
        evidence["enforced"] = strict
        if bool(probe.get("contract_ok", False)):
            return True, "", evidence
        status = str(probe.get("status", "") or "unprovable")
        evidence["reason"] = str(probe.get("reason", "") or status)
        if not strict:
            # rehearsal：口径漂移照样记证据/审计，但不阻断排演（永不进 clean）。
            return True, "", evidence
        reason = (
            "feature_price_mode_mismatch" if status == "mismatch" else f"feature_price_{status}"
        )
        return False, reason, evidence

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _job_now(self) -> datetime:
        now = getattr(self._service, "_job_now", None)
        return now() if callable(now) else datetime.now()

    def _deadline(self):
        alpha_cfg = self._alpha_config()
        raw = str(getattr(alpha_cfg, "live_cycle_latest_time", "23:55")).strip()
        hour, minute = raw.split(":")[:2]
        from datetime import time as dt_time

        return dt_time(int(hour), int(minute))

    def _audit(
        self, *, event_type: str, level: str, payload: dict[str, object], trace_id: str = ""
    ) -> None:
        recorder = getattr(self._service, "_record_audit_event", None)
        if not callable(recorder):
            return
        try:
            recorder(
                event_type=event_type, level=level, trace_id=trace_id, payload=payload
            )
        except Exception:  # noqa: BLE001 - 审计失败绝不反噬主流程
            pass

    def _result(
        self, success: bool, detail: str, trade_date: date
    ) -> dict[str, object]:
        return {
            "_scheduler_success": bool(success),
            "_scheduler_detail": detail,
            "_scheduler_ran": True,
            "trade_date": trade_date.isoformat(),
        }

    def _run_cli(
        self, script_name: str, argv: list[str], *, timeout_sec: int
    ) -> tuple[int, str]:
        """重活隔离在子进程（捕获/成熟要加载全市场面板，GiB 级内存随退出释放）。"""
        repo_root = self.repo_root()
        command = [sys.executable, str(repo_root / "scripts" / script_name), *argv]
        completed = subprocess.run(  # noqa: S603 - 固定脚本 + 列表参数，无 shell
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout_sec)),
            check=False,
        )
        tail = "\n".join(
            (completed.stdout or "").strip().splitlines()[-5:]
            + (completed.stderr or "").strip().splitlines()[-15:]
        )
        return int(completed.returncode), tail

    def _cycle_state(
        self, *, root: Path, epoch_id: str, trade_date: date
    ) -> dict[str, object]:
        from stock_analyzer.alpha_v2.validation.epoch import epoch_subdirs
        from stock_analyzer.alpha_v2.validation.shadow_capture import shadow_path

        shadow_exists = shadow_path(root, epoch_id, trade_date).exists()
        report_exists = (
            epoch_subdirs(root, epoch_id)["reports"]
            / f"validation_kpi_{epoch_id}_{trade_date.strftime('%Y%m%d')}.json"
        ).exists()
        return {"captured": shadow_exists, "reported": report_exists}

    def _funnel_ready(self, *, trade_date: date) -> tuple[bool, str]:
        """廉价前置检查：当天 funnel 存在且已链接正式晚报（完整校验在 capture 内）。"""
        path = funnel_snapshot_path(self.funnel_root(), trade_date)
        if not path.exists():
            return False, "production_funnel_absent"
        try:
            payload = load_funnel_snapshot(path)
        except Exception as exc:  # noqa: BLE001 - 细粒度原因交给 capture 报
            return False, f"production_funnel_unreadable:{exc.__class__.__name__}"
        if not _published_report_id(payload):
            return False, "production_funnel_not_linked_to_report"
        return True, ""

    def _record_missing_day(
        self, *, root: Path, epoch_id: str, trade_date: date, reason: str
    ) -> None:
        """把"这一天没有合法生产捕获"写进 missing 台账（幂等；失败只记审计）。"""
        try:
            from stock_analyzer.alpha_v2.validation.epoch import get_epoch
            from stock_analyzer.alpha_v2.validation.shadow_capture import (
                read_shadow_rows,
                record_missing_prediction_day,
            )

            epoch = get_epoch(root, epoch_id)
            if epoch is None:
                return
            if read_shadow_rows(root, epoch_id, trade_date):
                return
            record_missing_prediction_day(
                root=root, epoch=epoch, signal_date=trade_date, reason=reason
            )
        except Exception as exc:  # noqa: BLE001
            self._audit(
                event_type="alpha_v2_missing_day_record_failed",
                level="error",
                payload={
                    "trade_date": trade_date.isoformat(),
                    "reason": reason,
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
            )


__all__ = ["JOB_NAME", "LiveShadowCycleService"]
