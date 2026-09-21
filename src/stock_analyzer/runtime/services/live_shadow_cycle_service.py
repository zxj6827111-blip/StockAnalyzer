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
    emit_funnel_snapshot,
    extract_funnel_from_scan_report,
    funnel_snapshot_hash,
    funnel_snapshot_path,
    link_funnel_to_report,
    load_funnel_snapshot,
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
        return Path(__file__).resolve().parents[3]

    def funnel_root(self) -> Path:
        alpha_cfg = self._alpha_config()
        relative = str(getattr(alpha_cfg, "production_funnel_root", "")).strip()
        return self.repo_root() / relative

    def artifact_root(self) -> Path:
        alpha_cfg = self._alpha_config()
        return Path(str(getattr(alpha_cfg, "artifact_root", "artifacts/alpha_v2")))

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
            payload = extract_funnel_from_scan_report(
                source_report=report,
                trace_id=trace_id,
                scan_status="night_scan_completed",
                created_at=trade_date.isoformat(),
            )
            day = trade_date.date().isoformat()
            payload["signal_date"] = day
            payload["trade_date"] = day
            payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
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

        - **依赖驱动**：必须"当天晚报已发布（funnel 已链接）"才起捕获；未就绪时
          快速返回 waiting（不计失败），窗口内每个槽位重试；
        - **无 active epoch = safe skip**：Alpha V2 尚未启动阶段不破坏生产调度；
        - active epoch + 交易日 + 到窗口末尾仍未就绪 = 明确 failure/audit，并落
          missing 台账（该日永不计 clean OOS，绝不静默成功）；
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

        readiness = self._service._week5_automation_service.probe_nightly_readiness()
        funnel_ready, funnel_reason = self._funnel_ready(trade_date=trade_date)
        if not bool(state["captured"]) and not (
            bool(readiness.get("allowed", False)) and funnel_ready
        ):
            reason = funnel_reason or str(readiness.get("reason", "") or "readiness_blocked")
            if current.time() >= self._deadline():
                self._record_missing_day(
                    root=root,
                    epoch_id=epoch.epoch_id,
                    trade_date=trade_date,
                    reason=f"production_funnel_unavailable:{reason}",
                )
                self._audit(
                    event_type="alpha_v2_cycle_blocked_day",
                    level="error",
                    payload={
                        "trade_date": trade_date.isoformat(),
                        "reason": reason,
                        "readiness": dict(readiness),
                    },
                )
                return {
                    **self._result(
                        True, f"alpha_v2_blocked_recorded_missing:{reason}", trade_date
                    ),
                    "missing_recorded": True,
                }
            return self._result(True, f"alpha_v2_waiting:{reason}", trade_date)

        steps: list[tuple[str, list[str], int]] = []
        if not bool(state["captured"]):
            steps.extend(
                [
                    (
                        "data_health",
                        [
                            "--as-of",
                            trade_date.isoformat(),
                            "--market-db",
                            "artifacts/warehouse/market.duckdb",
                            "--out",
                            "artifacts/runtime/data_health.json",
                        ],
                        300,
                    ),
                    (
                        "capture",
                        [
                            "--epoch-id",
                            epoch.epoch_id,
                            "--signal-date",
                            trade_date.isoformat(),
                            "--market-db",
                            "artifacts/warehouse/market.duckdb",
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
                    [
                        "--epoch-id",
                        epoch.epoch_id,
                        "--evaluation-date",
                        trade_date.isoformat(),
                        "--market-db",
                        "artifacts/warehouse/market.duckdb",
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
        if not str(payload.get("night_scan_report_id", "") or "").strip():
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
