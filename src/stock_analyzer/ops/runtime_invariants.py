"""运行时不变式巡检：一条命令回答"这个系统现在是不是正常在跑"。

## 为什么需要它

2026-09-16 复盘发现：现有门禁全是**代码级**（ruff/mypy/pytest），preflight/smoke 只管
"能不能启动、API 通不通"。**没有任何一条在问"分钟表今天推进了吗""注册表有 champion 吗"
"那个挂载还在吗"**。而这个系统的故障形态恰恰是**静默**的——不崩、不报错、日志还打印
"一切正常"，只是什么也没做：

- 分钟汇总同步空转 18 天（每天打印 `no missing dates`）；
- `week5_first_board_*` 状态 `success` 却 20 天没跑过；
- 夜扫连续 6 次 `blocked_data_gate`；
- 校准器塌成常数（13 个交易日分数无区分度）。

静默故障不产生告警，只能靠人偶然盯到输出起疑——所以"修不完"的真正原因是**没有探测器**。
本模块就是那层探测器：把不变式写成可执行断言，一条命令给出红/绿。

## 设计要点

- **不依赖节假日日历**：核心不变式是"分钟表的最新日期 >= 日线表的最新日期"（自指），
  再加一条"日线表 >= 昨日（周一~周五）"的绝对底线。日线链本身每晚都在推进，拿它当
  基准比引入交易日历更稳，也不会在节假日误报。
- **分级而不是一刀切**：``defect``（真坏了）/ ``pending_decision``（已知、等治理决定，
  比如"registry 无 champion"）/ ``info``（陈旧条目这类不会算错数但会误导排查的东西）。
  红/绿只看 ``defect`` —— 把"等决定"染成永久红灯，等于把红灯训练成背景噪音。
- **判定与取数分离**：每个 ``check_*`` 只吃显式原语（日期、字典、path->bool），
  所以能离线用假数据把所有分支测干净；真正的 IO 只集中在 ``main()``。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SEVERITY_DEFECT = "defect"
SEVERITY_PENDING = "pending_decision"
SEVERITY_INFO = "info"

CST = timezone(timedelta(hours=8))

# 关键挂载/路径：缺任何一个，对应链路都会静默降级（2026-09-16 的 qq_minute_raw 事故）
REQUIRED_PATHS = (
    "/app/artifacts",
    "/data/intraday_summary",
    "/data/qq_minute_raw",
    "/data/vendor_history",
)

# 更新窗：19:45 updater 会先失效 readiness、约 20:35 前重新发布。此窗口内缺 readiness
# 属正常，不当 defect（否则每天 19:45~20:35 都会假报警）。
READINESS_UPDATE_WINDOW = ((19, 0), (21, 0))

_STUCK_RUNNING_MINUTES = 60
_STALE_DUE_DAYS = 3
# 连败的新鲜度分界：末次尝试在此天数内才算「正在失败」，否则属「等下次窗口验证」。
# 月度任务（如 factor_ic_decay_report）的旧失败不该常亮红灯，否则红灯成背景噪音。
_RECENT_FAILURE_DAYS = 3


@dataclass
class InvariantResult:
    name: str
    ok: bool
    severity: str
    detail: str
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def _previous_weekday(today: date) -> date:
    """今日之前最近的一个周一~周五（节假日无妨：节假日时数据本来就停在更早的交易日）。"""
    cursor = today - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def check_freshness(
    *,
    daily_max: date | None,
    minute_max: Mapping[str, date | None],
    today: date,
) -> list[InvariantResult]:
    """数据新鲜度：日线要有绝对底线，分钟要跟得上日线。

    ``minute_max`` 形如 ``{"intraday_summary_1m": date, "intraday_summary_5m": date}``。
    """
    results: list[InvariantResult] = []
    floor = _previous_weekday(today)
    if daily_max is None:
        results.append(
            InvariantResult(
                name="daily_bars_freshness",
                ok=False,
                severity=SEVERITY_DEFECT,
                detail="daily_bars 表里没有任何日期",
                evidence={"table": "daily_bars"},
            )
        )
    else:
        ok = daily_max >= floor
        results.append(
            InvariantResult(
                name="daily_bars_freshness",
                ok=ok,
                severity=SEVERITY_DEFECT,
                detail=(
                    f"daily_bars 最新 {daily_max}，要求 >= {floor}（昨日）"
                    if ok
                    else f"daily_bars 停在 {daily_max}，落后于 {floor}——日线链没在推进"
                ),
                evidence={"daily_max": daily_max.isoformat(), "floor": floor.isoformat()},
            )
        )
    for table, value in minute_max.items():
        if daily_max is None:
            continue
        if value is None:
            results.append(
                InvariantResult(
                    name=f"{table}_freshness",
                    ok=False,
                    severity=SEVERITY_DEFECT,
                    detail=f"{table} 表里没有任何日期",
                    evidence={"table": table},
                )
            )
            continue
        # 自指不变式：分钟侧必须至少和日线侧一样新。18 天空转正是这条破了
        # （分钟停 8/28、日线一直在推进）。用日线做基准无需交易日历。
        ok = value >= daily_max
        results.append(
            InvariantResult(
                name=f"{table}_freshness",
                ok=ok,
                severity=SEVERITY_DEFECT,
                detail=(
                    f"{table} 最新 {value} >= 日线 {daily_max}"
                    if ok
                    else f"{table} 停在 {value}，落后日线 {daily_max}——同步链路在空转"
                ),
                evidence={
                    "table": table,
                    "minute_max": value.isoformat(),
                    "daily_max": daily_max.isoformat(),
                    "lag_days": (daily_max - value).days,
                },
            )
        )
    return results


def check_mounts(
    *,
    required: Sequence[str] = REQUIRED_PATHS,
    exists: Callable[[str], bool],
) -> InvariantResult:
    """关键路径是否存在——缺了就是"看不见源"型静默故障的前置条件。"""
    missing = [path for path in required if not exists(path)]
    return InvariantResult(
        name="required_paths",
        ok=not missing,
        severity=SEVERITY_DEFECT,
        detail=(
            f"关键路径齐备（{len(required)} 个）"
            if not missing
            else f"缺少关键路径 {missing}——对应链路会静默降级"
        ),
        evidence={"missing": missing, "required": list(required)},
    )


def check_artifact_identity(identity: Mapping[str, Any] | None) -> InvariantResult:
    """在服工件是否有可验证的登记身份（models/identity.py 的六态判定）。

    ``match`` 才算 ok；``no_champion`` 归 ``pending_decision``——那是"治理没批准"，
    不是"系统坏了"，不该把红灯一直点亮（否则红灯会被当背景噪音）。
    """
    if identity is None:
        return InvariantResult(
            name="artifact_identity",
            ok=False,
            severity=SEVERITY_DEFECT,
            detail="拿不到工件身份报告（artifact_identity_report 不可用）",
        )
    status = str(identity.get("status", ""))
    loaded = str(identity.get("loaded_content_hash", ""))
    if status == "match":
        return InvariantResult(
            name="artifact_identity",
            ok=True,
            severity=SEVERITY_DEFECT,
            detail=f"在服工件与 champion 一致（{str(identity.get('champion_model_id'))}）",
            evidence={"status": status, "loaded_content_hash": loaded},
        )
    # "等治理决定"与"这次读不到"都归 pending，不该染红：前者等批准，后者只是本进程
    # 自己占着写锁（2026-09-16 盘中实测：巡检把 lock 冲突报成 defect = 假警报）。
    severity = (
        SEVERITY_PENDING
        if status in {"no_champion", "champion_hash_missing", "registry_busy", "match_registered"}
        else SEVERITY_DEFECT
    )
    return InvariantResult(
        name="artifact_identity",
        ok=False,
        severity=severity,
        detail=str(identity.get("detail", "")) or f"身份状态 {status}",
        evidence={"status": status, "loaded_content_hash": loaded},
    )


def check_overextension_inputs(candidates: Sequence[Mapping[str, Any]] | None) -> InvariantResult:
    """过热闸的输入是否退化成 fallback（**闸门退化 = 静默否决整条买入路径**）。

    2026-09-16 实据：12 轮夜扫 600 条候选，`overextension.level` **全部**是
    ``reject``，且 ``atr_distance / bias_ma5`` **恒为 33.333**。喂进去的行既没有
    ``ma5`` 也没有 ``atr14``（生产走 ``_latest_bar_dict(bars)``，bars 只有 OHLCV；
    期望输入是特征快照的 ``ma5/atr14`` 列），于是两处 fallback 同时生效：
    ``bias_ma5 = close - 1``、``atr_distance = (close - 1) / 0.03``，任何股价
    > 1.15 元的票都会越过 ``bias_reject_min=0.15`` 与 ``atr_distance_reject=3.0``
    → **无条件 reject**。

    **判据不能只看那个比值**：给真实输入时
    ``atr_distance / bias_ma5 = ma5 / atr14``，低波动票完全可能正好是 33.33。
    所以主判据取**物理上不成立的乖离**——``bias_ma5 = |close/ma5 - 1|`` 超过
    ``0.5`` 意味着收盘价高于 MA5 的 1.5 倍，真实行情不会在大半个池子里同时出现；
    而 fallback 会让几乎所有 1.5 元以上的票都落到这里。比值只作旁证。

    危害在于它是**静默**的：闸门"看起来在工作"（有 level、有 reasons、有 metrics），
    只是每次都说"过热"。没有这条探测，它只会表现为"夜扫长期 0 信号"，
    而被误读成"阈值太高"或"分数没 alpha"。
    """
    if not candidates:
        return InvariantResult(
            name="overextension_inputs",
            ok=True,
            severity=SEVERITY_INFO,
            detail="无候选可判（不构成信号）",
        )
    fallback_ratio = 1.0 / 0.03  # DEFAULT_ATR14_FALLBACK
    implausible_bias = 0
    ratio_like = 0
    rejected = 0
    total = 0
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        decision = item.get("overextension")
        metrics = decision.get("metrics") if isinstance(decision, Mapping) else None
        if not isinstance(metrics, Mapping):
            continue
        bias = metrics.get("bias_ma5")
        atr = metrics.get("atr_distance")
        if not isinstance(bias, (int, float)):
            continue
        total += 1
        if isinstance(decision, Mapping) and str(decision.get("level") or "") == "reject":
            rejected += 1
        if abs(float(bias)) > 0.5:
            implausible_bias += 1
        if isinstance(atr, (int, float)) and bias:
            if abs(float(atr) / float(bias) - fallback_ratio) < 0.01:
                ratio_like += 1
    if total == 0:
        return InvariantResult(
            name="overextension_inputs",
            ok=True,
            severity=SEVERITY_INFO,
            detail="候选里没有过热闸指标可判（未评估过）",
        )
    share = implausible_bias / total
    evidence = {
        "implausible_bias": implausible_bias,
        "ratio_like_fallback": ratio_like,
        "total": total,
        "reject_share": round(rejected / total, 4),
    }
    detail = (
        f"|bias_ma5|>0.5 的候选 {implausible_bias}/{total}"
        f"（fallback 特征 atr/bias=={fallback_ratio:.3f} 的 {ratio_like}/{total}），"
        f"reject 占比 {rejected / total:.1%}——疑似门槛取到 fallback（ma5=1.0/atr14=0.03），"
        "闸门退化为无条件否决"
    )
    return InvariantResult(
        name="overextension_inputs",
        ok=share < 0.5,
        severity=SEVERITY_DEFECT,
        detail="过热闸输入正常" if share < 0.5 else detail,
        evidence=evidence,
    )


def check_scheduler(
    *,
    jobs: Mapping[str, Mapping[str, object]],
    now: datetime,
    stuck_running_minutes: int = _STUCK_RUNNING_MINUTES,
    stale_due_days: int = _STALE_DUE_DAYS,
    recent_failure_days: int = _RECENT_FAILURE_DAYS,
) -> list[InvariantResult]:
    """调度健康：卡住的任务 / **正在**失败的 / 陈旧条目。

    刻意**不**按 cadence 判 next_due（月度任务会被误判成失败）：``next_due`` 只用来
    识别"陈旧条目"（``info``，会误导排查但不产生错数）。

    连败按**新鲜度**分两类（2026-09-16 首跑后补）：``factor_ic_decay_report`` 的 9 次
    失败全在 8/31（月度任务、修复已在其后落地），若与"昨天还在失败"的
    ``week5_automation_auction`` 同等报红，红灯就会被当背景噪音——**红灯必须意味着
    "正在坏"**：

    - 末次尝试在 ``recent_failure_days`` 内且 cf>0 → ``defect``（正在失败）；
    - 末次尝试更早 → ``pending_decision``（等下次窗口验证，可能已修）。
    """
    results: list[InvariantResult] = []
    stuck: list[str] = []
    failing: list[str] = []
    awaiting: list[str] = []
    stale: list[str] = []
    for name, entry in sorted(jobs.items()):
        running_since = str(entry.get("running_since") or "").strip()
        if running_since:
            started = _parse_dt(running_since)
            if started is not None and (now - started) > timedelta(minutes=stuck_running_minutes):
                stuck.append(f"{name}({running_since[:19]})")
        failures = entry.get("consecutive_failures")
        if isinstance(failures, int) and failures > 0:
            reason = str(entry.get("last_failure") or "")[:60]
            last_attempt = _parse_dt(
                str(entry.get("last_attempt_at") or entry.get("last_attempt") or "")
            )
            if last_attempt is not None and (now - last_attempt) <= timedelta(
                days=recent_failure_days
            ):
                failing.append(f"{name}(cf={failures}, {reason})")
            else:
                stamp = last_attempt.date().isoformat() if last_attempt else "unknown"
                awaiting.append(f"{name}(cf={failures}, 末次尝试 {stamp}, {reason})")
        due = _parse_dt(str(entry.get("next_due_at") or ""))
        if due is not None and (now - due) > timedelta(days=stale_due_days):
            stale.append(f"{name}(next_due={str(entry.get('next_due_at'))[:19]})")
    stuck_detail = (
        "无卡死任务"
        if not stuck
        else f"{len(stuck)} 个任务 running_since 超过 {stuck_running_minutes} 分钟未清：{stuck}"
    )
    failing_detail = (
        "无正在失败的任务" if not failing else f"{len(failing)} 个任务正在失败：{failing}"
    )
    awaiting_detail = (
        "无待验证的旧失败"
        if not awaiting
        else (
            f"{len(awaiting)} 个任务的历史失败早于 {recent_failure_days} 天，"
            f"等下次窗口验证：{awaiting}"
        )
    )
    stale_detail = (
        "无陈旧条目"
        if not stale
        else f"{len(stale)} 条 next_due 已过期 >{stale_due_days} 天（多为设计内退出）：{stale}"
    )
    results.append(
        InvariantResult(
            name="scheduler_stuck_jobs",
            ok=not stuck,
            severity=SEVERITY_DEFECT,
            detail=stuck_detail,
            evidence={"stuck": stuck},
        )
    )
    results.append(
        InvariantResult(
            name="scheduler_failing_jobs",
            ok=not failing,
            severity=SEVERITY_DEFECT,
            detail=failing_detail,
            evidence={"failing": failing},
        )
    )
    results.append(
        InvariantResult(
            name="scheduler_failures_awaiting_run",
            ok=not awaiting,
            severity=SEVERITY_PENDING,
            detail=awaiting_detail,
            evidence={"awaiting": awaiting},
        )
    )
    results.append(
        InvariantResult(
            name="scheduler_stale_entries",
            ok=not stale,
            severity=SEVERITY_INFO,
            detail=stale_detail,
            evidence={"stale": stale},
        )
    )
    return results


def check_readiness(
    *,
    payload: Mapping[str, Any] | None,
    expected_trade_date: date | None,
    now: datetime,
) -> InvariantResult:
    """夜扫 readiness：更新窗内缺席不算缺陷。"""
    if payload is None:
        in_window = _within(now, READINESS_UPDATE_WINDOW)
        return InvariantResult(
            name="nightly_readiness",
            ok=in_window,
            severity=SEVERITY_INFO if in_window else SEVERITY_DEFECT,
            detail=(
                "更新窗内 readiness 暂缺（19:45 失效、约 20:35 重新发布），属正常"
                if in_window
                else "nightly_data_ready.json 缺失且在更新窗之外——今晚的夜扫会被拦"
            ),
            evidence={"in_update_window": in_window},
        )
    target = _parse_date(str(payload.get("target_trade_date") or ""))
    slots_ok = all(
        isinstance(payload.get(slot), Mapping) and bool(payload[slot].get("ok"))  # type: ignore[union-attr]
        for slot in ("daily", "index", "delta")
    )
    date_ok = expected_trade_date is None or target == expected_trade_date
    ok = slots_ok and date_ok
    return InvariantResult(
        name="nightly_readiness",
        ok=ok,
        severity=SEVERITY_DEFECT,
        detail=(
            f"readiness 就绪（target={target}）"
            if ok
            else f"readiness 异常：target={target}（期望 {expected_trade_date}）slots_ok={slots_ok}"
        ),
        evidence={
            "target_trade_date": target.isoformat() if target else "",
            "expected": expected_trade_date.isoformat() if expected_trade_date else "",
            "slots_ok": slots_ok,
        },
    )


def check_night_scan_artifact(
    *,
    latest_status: str,
    latest_detail: str,
    latest_timestamp: datetime | None,
    now: datetime,
) -> InvariantResult:
    """最近一次夜扫是否成功、是否够新（按自然日：隔夜没跑就算缺陷）。"""
    if latest_timestamp is None:
        return InvariantResult(
            name="night_scan_artifact",
            ok=False,
            severity=SEVERITY_DEFECT,
            detail="找不到任何夜扫结果文件",
        )
    age_days = (now - latest_timestamp).days
    ok = latest_status == "success" and age_days <= 1
    scan_detail = (
        f"最近夜扫 {latest_timestamp.date()} success"
        if ok
        else (
            f"最近夜扫 {latest_timestamp.date()} status={latest_status} "
            f"detail={latest_detail}（{age_days} 天前）"
        )
    )
    return InvariantResult(
        name="night_scan_artifact",
        ok=ok,
        severity=SEVERITY_DEFECT,
        detail=scan_detail,
        evidence={
            "status": latest_status,
            "detail": latest_detail,
            "age_days": age_days,
        },
    )


def _parse_dt(value: str) -> datetime | None:
    """解析调度器/产物时间戳。

    ``runtime_state.json`` 与 job 结果里的时间戳是**无时区的 CST 本地时间**
    （例：``updated_at='2026-09-15T20:05:57'`` 对应当时 CST 20:05）。把它们当 UTC
    解释会让"卡死 3 小时"变成"还有 5 小时才到期"——判定直接失效，所以默认补 CST。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=CST)


def _parse_date(value: str) -> date | None:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _within(now: datetime, window: tuple[tuple[int, int], tuple[int, int]]) -> bool:
    moment = now.hour * 60 + now.minute
    (start_h, start_m), (end_h, end_m) = window
    return start_h * 60 + start_m <= moment <= end_h * 60 + end_m


def summarize(results: Sequence[InvariantResult]) -> dict[str, object]:
    """汇总：红/绿只看 ``defect``；``pending_decision`` 与 ``info`` 如实列出。"""
    defects = [r for r in results if not r.ok and r.severity == SEVERITY_DEFECT]
    pendings = [r for r in results if not r.ok and r.severity == SEVERITY_PENDING]
    infos = [r for r in results if not r.ok and r.severity == SEVERITY_INFO]
    return {
        "ok": not defects,
        "checked": len(results),
        "defects": [r.to_dict() for r in defects],
        "pending_decisions": [r.to_dict() for r in pendings],
        "notes": [r.to_dict() for r in infos],
        "results": [r.to_dict() for r in results],
    }


# --------------------------------------------------------------------------- #
# IO 层（只有这里碰磁盘/DB；判定逻辑全在上面，便于离线测试）
# --------------------------------------------------------------------------- #


def collect_and_evaluate(
    *,
    artifacts_root: Path,
    state_path: Path,
    market_db: Path,
    protocol_db: Path,
    now: datetime,
    exists: Callable[[str], bool] | None = None,
    health_urls: Sequence[str] = (),
) -> dict[str, object]:
    """读真实来源并跑全部检查。DB 一律只读打开。"""

    path_exists = exists if exists is not None else (lambda path: Path(path).exists())

    daily_max = _table_max_date(market_db, "daily_bars")
    minute_max = {
        table: _table_max_date(market_db, table)
        for table in ("intraday_summary_1m", "intraday_summary_5m")
    }
    state = _read_json(state_path)
    scheduler = (state.get("scheduler_state") or {}) if isinstance(state, dict) else {}
    jobs = scheduler.get("jobs") or {}
    readiness = _read_json(artifacts_root / "runtime" / "nightly_data_ready.json")
    scan_status, scan_detail, scan_ts = _latest_job_result(
        artifacts_root / "runtime" / "scheduler_job_results", "week5_night_scan"
    )
    identity = _artifact_identity(protocol_db, health_urls)
    overext_candidates = _night_scan_candidates(
        artifacts_root / "runtime" / "scheduler_job_results", "week5_night_scan"
    )

    results: list[InvariantResult] = []
    results.extend(check_freshness(daily_max=daily_max, minute_max=minute_max, today=now.date()))
    results.append(check_mounts(exists=path_exists))
    results.append(check_artifact_identity(identity))
    results.append(check_overextension_inputs(overext_candidates))
    results.extend(check_scheduler(jobs=jobs, now=now))
    results.append(
        check_readiness(
            payload=readiness if isinstance(readiness, dict) else None,
            expected_trade_date=daily_max,
            now=now,
        )
    )
    results.append(
        check_night_scan_artifact(
            latest_status=scan_status,
            latest_detail=scan_detail,
            latest_timestamp=scan_ts,
            now=now,
        )
    )
    summary = summarize(results)
    summary["generated_at"] = now.isoformat()
    summary["daily_max"] = daily_max.isoformat() if daily_max else ""
    return summary


def _table_max_date(db_path: Path, table: str) -> date | None:
    import duckdb  # noqa: WPS433

    if not Path(db_path).exists():
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception:  # noqa: BLE001 - 读不到库由上层按 None 处理
        return None
    try:
        row = con.execute(f"SELECT max(date) FROM {table}").fetchone()
    except Exception:  # noqa: BLE001 - 表不存在同样按 None
        return None
    finally:
        con.close()
    return _parse_date(str(row[0])) if row and row[0] is not None else None


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_job_result(results_root: Path, job: str) -> tuple[str, str, datetime | None]:
    files = sorted(
        Path(results_root).glob(f"*/{job}.*.json"),
        key=lambda item: item.stat().st_mtime,
    )
    if not files:
        return "", "", None
    payload = _read_json(files[-1])
    stamp = str(payload.get("timestamp") or "")
    return (
        str(payload.get("status") or ""),
        str(payload.get("detail") or ""),
        _parse_dt(stamp),
    )


def _night_scan_candidates(results_root: Path, job: str) -> list[Mapping[str, Any]]:
    """最近一份夜扫产物里的候选列表（含过热门指标）；读不到就返回空。"""
    files = sorted(
        Path(results_root).glob(f"*/{job}.*.json"),
        key=lambda item: item.stat().st_mtime,
    )
    if not files:
        return []
    payload = _read_json(files[-1])
    results = payload.get("results")
    entry = results[0] if isinstance(results, list) and results else {}
    entry_payload = entry.get("payload") if isinstance(entry, Mapping) else None
    report = entry_payload.get("report") if isinstance(entry_payload, Mapping) else None
    source = report.get("source_report") if isinstance(report, Mapping) else None
    pool = source.get("signal_pool") if isinstance(source, Mapping) else None
    candidates = pool.get("candidates") if isinstance(pool, Mapping) else None
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, Mapping)]


def _identity_from_service(
    health_urls: Sequence[str], *, timeout: float = 15.0
) -> dict[str, object] | None:
    """向服务本身要身份报告（首个可用地址为准）。

    为什么不直连学习库：服务进程在运行期持着该库的写锁，**只有它能读注册表**。
    2026-09-16 在 NAS 实测：任何其它进程（同容器的 api 容器内、scheduler-critical
    容器内都一样）用 ``read_only=True`` 直连必然报 ``Conflicting lock is held``；
    于是身份项在服务运行期只能恒答 ``registry_busy`` —— 而服务运行期恰恰是唯一
    需要它的时刻。故选"先问服务，直连仅作服务不可达时的兜底"（那时锁已释放）。

    返回 ``None`` 表示问不到（服务不可达 / 响应里没有身份块），调用方据此降级。
    """
    import urllib.request  # noqa: WPS433

    for url in health_urls:
        if not url:
            continue
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 服务不可达属预期降级路径，不是异常
            continue
        identity = payload.get("model_identity") if isinstance(payload, Mapping) else None
        if not isinstance(identity, Mapping) or not identity.get("status"):
            continue
        served = dict(identity)
        # 标注来源：同一状态可能来自"问服务"或"直连兜底"，排查时必须能分辨。
        detail = str(served.get("detail") or "")
        provenance = f"（来源：服务 API {url}）"
        served["detail"] = f"{detail}{provenance}" if detail else provenance
        return served
    return None


def _read_identity_from_db(protocol_db: Path) -> dict[str, object] | None:
    """兜底路径：直连学习库读注册表（**只在服务不可达时才有意义**，见上）。"""
    import duckdb  # noqa: WPS433

    from stock_analyzer.models.bundle import compute_artifact_identity_hash  # noqa: WPS433
    from stock_analyzer.models.identity import describe_artifact_identity  # noqa: WPS433

    artifact_path = Path("/app/artifacts/model_v1.json")
    loaded_hash = ""
    if artifact_path.exists():
        try:
            loaded_hash = compute_artifact_identity_hash(artifact_path)
        except Exception:  # noqa: BLE001
            loaded_hash = ""
    champion: dict[str, object] | None = None
    registered: list[dict[str, object]] = []
    error = ""
    busy = False
    try:
        con = duckdb.connect(str(protocol_db), read_only=True)
        try:
            rows = con.execute(
                "SELECT model_id, artifact_content_hash, lifecycle_state, updated_at "
                "FROM model_registry ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()
        finally:
            con.close()
        for row in rows:
            entry = {
                "model_id": row[0],
                "artifact_content_hash": row[1],
                "lifecycle_state": row[2],
            }
            registered.append(entry)
            if champion is None and str(row[2]) == "champion":
                champion = entry
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        busy = "lock" in error.lower()
    return describe_artifact_identity(
        loaded_uri=str(artifact_path),
        loaded_hash=loaded_hash,
        champion=champion,
        registered=registered,
        registry_error=error,
        registry_busy=busy,
    )


def _artifact_identity(
    protocol_db: Path, health_urls: Sequence[str] = ()
) -> dict[str, object] | None:
    """身份报告：优先问服务，问不到再直连兜底（两条路径同用 models/identity.py 判定）。"""
    served = _identity_from_service(health_urls)
    if served is not None:
        return served
    return _read_identity_from_db(protocol_db)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="运行时不变式巡检（一条命令给红/绿）")
    parser.add_argument("--artifacts-root", default="/app/artifacts")
    parser.add_argument("--state-path", default="/app/artifacts/runtime/runtime_state.json")
    parser.add_argument("--market-db", default="/app/artifacts/warehouse/market.duckdb")
    parser.add_argument("--protocol-db", default="/app/artifacts/training/learning_protocol.duckdb")
    parser.add_argument(
        "--health-url",
        default="",
        help=(
            "服务健康端点（身份项优先走它，因为学习库写锁在服务进程手里）。"
            "留空则用 SA_INVARIANTS_HEALTH_URL，再退到内建候选 127.0.0.1:8000 / api:8000。"
        ),
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    configured = args.health_url or os.environ.get("SA_INVARIANTS_HEALTH_URL", "")
    # 内建候选覆盖「在 api 容器里跑」与「在姊妹容器里跑」两种用法；问不到就各自降级。
    health_urls = ([configured] if configured else []) + [
        "http://127.0.0.1:8000/health/deep",
        "http://api:8000/health/deep",
    ]

    now = datetime.now(CST)
    summary = collect_and_evaluate(
        artifacts_root=Path(args.artifacts_root),
        state_path=Path(args.state_path),
        market_db=Path(args.market_db),
        protocol_db=Path(args.protocol_db),
        now=now,
        exists=os.path.exists,
        health_urls=health_urls,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        marks = {"defect": "FAIL", "pending_decision": "PEND", "info": "NOTE"}
        stamp = now.isoformat(timespec="seconds")
        print(f"[runtime-invariants] {stamp}  checked={summary['checked']}")
        for item in summary["results"]:  # type: ignore[union-attr]
            mark = "OK  " if item["ok"] else marks[str(item["severity"])]
            print(f"  {mark} {item['name']:26s} {item['detail']}")
        verdict = "OK" if summary["ok"] else f"DEFECTS={len(summary['defects'])}"
        print(f"[verdict] {verdict}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
