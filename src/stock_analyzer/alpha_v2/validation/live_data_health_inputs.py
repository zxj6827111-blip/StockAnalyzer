"""M4-L R1：date_health 的**权威输入构造**（全部只读、只从当天真实数据/工件计算）。

为什么需要它（外部复核 B1）：``alpha_v2_data_health_snapshot.py`` 原本要求调用方
逐个传文件路径（universe/board/feature/model/breadth），而生产调度循环一个都没传 →
S08 七项全部 degraded → ``status != healthy`` → 每一天都写 ``clean_oos_eligible=false``
（且 immutable 落盘后无法改口）⇒ Live Clean OOS 永远累计不了。本模块把这些输入
按"当天真实数据 + 权威实现"算出来，供 CLI 的 ``--derive-inputs`` 使用：

S08 输入的权威来源（逐项只读、只算"当天可证明"的东西）：

```text
latest_trade_date   market.duckdb max(date)
universe_snapshot   resolve_asof_universe（S03 唯一实现）
                    （候选名单 = 库内全集；入选完全由 <= as_of 的 bar 事实决定）
valid_symbol_count  expected_active 中在 as_of 当日有 bar 的只数
                    （分子分母同集合——旧实现用全库当日符号数，会把非 expected 算进分子）
board_coverage      同一 expected_active 集合按 board 分组：
                    板块内当日有 bar 只数 / 板块内 expected 只数
feature_snapshot    week5 features_light/current.json（FeatureSnapshotManifest）
                    snapshot_is_current + coverage + max_trade_date 与 as_of 对齐
model_identity      active epoch 的 frozen shadow model 工件（不是 legacy champion）
                    逐文件重算 artifact_hash + 比对 epoch 冻结 identity / schema
breadth_artifact    compute_market_breadth_from_warehouse（唯一广度实现）
                    现算落影子证据路径；as_of / date_max 必须对齐
```

**广度为什么落影子路径**：生产 ``artifacts/runtime/market_breadth.json`` 目前缺失，
live 广度门因此在 fail-open；直接补写生产路径会把"低广度禁买"从静默失效变成生效——
那是**选股语义变更**，M4-L 明令禁止（observer 角色）。所以这里用同一个 builder
（同数据、同代码、同 as_of）把证据写到 ``alpha_v2`` 影子路径，既满足 S08 的
"缺失不得当健康"，又不动生产门。（生产 breadth 接线属另一个授权项。）

所有函数只读；任何一步失败都返回"缺失"（None / False），绝不猜、绝不构造假 payload。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from stock_analyzer.alpha_v2.dual_price_series import (
    FEATURE_PRICE_MODE_ALLOWED,
    FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA,
)
from stock_analyzer.alpha_v2.research.panel import normalize_board
from stock_analyzer.data.asof_universe import (
    DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS,
    SymbolPitStats,
    history_window_days,
    resolve_asof_universe,
)

BREADTH_EVIDENCE_SCHEMA = "alpha_v2_breadth_evidence.v1"
DEFAULT_BREADTH_EVIDENCE_RELATIVE = "artifacts/alpha_v2/runtime/market_breadth_evidence.json"
FEATURE_SNAPSHOT_MANIFEST_FILENAME = "current.json"


class LiveInputError(RuntimeError):
    """输入构造失败（调用方按"缺失"处理，不得降级成健康）。"""


@dataclass(slots=True)
class DerivedDataHealthInputs:
    """一次派生得到的 S08 输入快照 + 逐项证据（供审计与日志）。"""

    latest_trade_date: str | None = None
    valid_symbol_count: int | None = None
    universe_snapshot: dict[str, object] | None = None
    board_coverage: dict[str, float] | None = None
    feature_snapshot: dict[str, object] | None = None
    model_identity: dict[str, object] | None = None
    breadth_artifact_present: bool = False
    evidence: dict[str, object] = field(default_factory=dict)

    def as_inputs(self) -> dict[str, object]:
        return {
            "latest_trade_date": self.latest_trade_date,
            "valid_symbol_count": self.valid_symbol_count,
            "universe_snapshot": self.universe_snapshot,
            "board_coverage": self.board_coverage,
            "feature_snapshot": self.feature_snapshot,
            "model_identity": self.model_identity,
            "breadth_artifact_present": self.breadth_artifact_present,
        }


# ---------------------------------------------------------------------------
# 行情库事实（universe / valid / board）
# ---------------------------------------------------------------------------


def _connect_read_only(market_db: str | Path):
    import duckdb

    return duckdb.connect(str(market_db), read_only=True)


def derive_universe_facts(
    *,
    market_db: str | Path,
    as_of: date,
    min_history_days: int,
    expected_active_lookback_days: int = DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS,
) -> tuple[dict[str, object] | None, int | None, dict[str, float] | None, dict[str, object]]:
    """构造 ``(universe_snapshot_payload, valid_symbol_count, board_coverage, evidence)``。

    实现要点（与 ``build_pit_stats`` 逐项对拍，见 tests）：

    - 候选名单 = 库内全部 symbol（**不是** Quality300——那是筛选结果，不是 PIT 分母）；
    - ``bars_in_window`` / ``bars_in_lookback`` / ``first_bar_date`` / ``last_bar_date``
      用与 ``build_pit_stats`` 相同的自然日 cutoff 在 SQL 侧聚合，避免把 50 万行
      拉进 pandas；
    - 判定本身仍走 ``resolve_asof_universe``（S03 唯一实现），本函数不重新定义
      eligible / expected_active 语义；
    - ``valid_symbol_count`` = expected_active ∩（as_of 当日有 bar），分子分母同集合。
    """
    probe_window_days = history_window_days(
        min_history_days=int(min_history_days),
        lookback_days=int(expected_active_lookback_days),
    )
    history_cutoff = as_of - pd.Timedelta(days=int(probe_window_days)).to_pytimedelta()
    lookback_cutoff = as_of - pd.Timedelta(days=int(expected_active_lookback_days)).to_pytimedelta()
    evidence: dict[str, object] = {
        "probe_window_days": int(probe_window_days),
        "history_cutoff": history_cutoff.isoformat(),
        "lookback_cutoff": lookback_cutoff.isoformat(),
        "min_history_days": int(min_history_days),
        "expected_active_lookback_days": int(expected_active_lookback_days),
    }
    try:
        connection = _connect_read_only(market_db)
    except Exception as exc:  # noqa: BLE001 - 读不到就是缺失
        evidence["error"] = f"market_db_unreadable:{exc.__class__.__name__}:{exc}"
        return None, None, None, evidence
    try:
        symbols = [
            str(row[0]).strip()
            for row in connection.execute("SELECT DISTINCT symbol FROM daily_bars").fetchall()
            if str(row[0]).strip()
        ]
        rows = connection.execute(
            """
            SELECT symbol,
                   sum(CASE WHEN date >= CAST(? AS DATE) THEN 1 ELSE 0 END) AS bars_in_window,
                   sum(CASE WHEN date >= CAST(? AS DATE) THEN 1 ELSE 0 END) AS bars_in_lookback,
                   min(CASE WHEN date >= CAST(? AS DATE) THEN date END) AS first_in_window,
                   max(date) AS last_bar
            FROM daily_bars
            WHERE date <= CAST(? AS DATE)
            GROUP BY 1
            """,
            [
                history_cutoff.isoformat(),
                lookback_cutoff.isoformat(),
                history_cutoff.isoformat(),
                as_of.isoformat(),
            ],
        ).fetchall()
        present_rows = connection.execute(
            "SELECT symbol FROM daily_bars WHERE date = CAST(? AS DATE)", [as_of.isoformat()]
        ).fetchall()
        board_rows = connection.execute(
            """
            SELECT symbol, board FROM (
              SELECT symbol, board,
                     row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
              FROM daily_bars
              WHERE date <= CAST(? AS DATE)
            ) WHERE rn = 1
            """,
            [as_of.isoformat()],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"universe_probe_failed:{exc.__class__.__name__}:{exc}"
        return None, None, None, evidence
    finally:
        connection.close()

    if not symbols or not rows:
        evidence["error"] = "universe_probe_empty"
        return None, None, None, evidence

    stats: dict[str, SymbolPitStats] = {}
    for symbol, bars_in_window, bars_in_lookback, first_in_window, last_bar in rows:
        normalized = str(symbol).strip()
        if not normalized:
            continue
        stats[normalized] = SymbolPitStats(
            symbol=normalized,
            bars_in_window=int(bars_in_window or 0),
            bars_in_lookback=int(bars_in_lookback or 0),
            first_bar_date=first_in_window if isinstance(first_in_window, date) else None,
            last_bar_date=last_bar if isinstance(last_bar, date) else None,
        )
    snapshot = resolve_asof_universe(
        as_of=as_of,
        index_symbols=symbols,
        stats=stats,
        min_history_days=int(min_history_days),
        expected_active_lookback_days=int(expected_active_lookback_days),
    )
    expected_active = set(snapshot.expected_active_symbols)
    present_symbols = {str(row[0]).strip() for row in present_rows if str(row[0]).strip()}
    valid_symbol_count = len(expected_active & present_symbols)

    board_by_symbol: dict[str, str] = {}
    for symbol, board in board_rows:
        normalized = str(symbol).strip()
        if not normalized:
            continue
        board_by_symbol[normalized] = normalize_board(board, symbol=normalized)
    expected_by_board: dict[str, int] = {}
    present_by_board: dict[str, int] = {}
    for symbol in expected_active:
        board_name = board_by_symbol.get(symbol) or normalize_board("", symbol=symbol)
        expected_by_board[board_name] = expected_by_board.get(board_name, 0) + 1
        if symbol in present_symbols:
            present_by_board[board_name] = present_by_board.get(board_name, 0) + 1
    board_coverage = {
        board_name: round(present_by_board.get(board_name, 0) / count, 6)
        for board_name, count in sorted(expected_by_board.items())
        if count > 0
    }
    evidence.update(
        {
            "index_symbol_count": len(symbols),
            "eligible_count": snapshot.eligible_count,
            "expected_active_count": snapshot.expected_active_count,
            "valid_expected_active": valid_symbol_count,
            "board_count": len(board_coverage),
            "universe_snapshot_id": snapshot.universe_snapshot_id,
        }
    )
    payload = dict(snapshot.to_payload())
    payload["expected_active_count"] = snapshot.expected_active_count
    return payload, valid_symbol_count, board_coverage, evidence


def latest_trade_date(*, market_db: str | Path) -> str | None:
    try:
        connection = _connect_read_only(market_db)
    except Exception:  # noqa: BLE001
        return None
    try:
        latest = connection.execute("SELECT max(date) FROM daily_bars").fetchone()[0]
    except Exception:  # noqa: BLE001
        return None
    finally:
        connection.close()
    if latest is None:
        return None
    return latest.isoformat() if hasattr(latest, "isoformat") else str(latest)


# ---------------------------------------------------------------------------
# Feature snapshot / model identity / breadth
# ---------------------------------------------------------------------------


def derive_feature_snapshot_input(
    *, config: object, as_of: date
) -> tuple[dict[str, object] | None, dict[str, object]]:
    """读 week5 feature snapshot 清单并验证"当前 + 覆盖 + 交易日对齐"。

    只读 ``current.json``（不加载 parquet 帧）：清单本身就是覆盖率的权威陈述，
    帧内容由 capture 侧自行加载；这里只回答 S08 的问题"特征快照可用吗"。
    """
    from stock_analyzer.feature.snapshot import (
        FeatureSnapshotManifest,
        resolve_snapshot_root,
        snapshot_is_current,
    )

    evidence: dict[str, object] = {}
    try:
        root = resolve_snapshot_root(config)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"feature_snapshot_root_unresolvable:{exc.__class__.__name__}"
        return None, evidence
    manifest_path = Path(root) / FEATURE_SNAPSHOT_MANIFEST_FILENAME
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        evidence["error"] = f"feature_snapshot_manifest_unreadable:{exc.__class__.__name__}"
        evidence["manifest_path"] = str(manifest_path)
        return None, evidence
    manifest = FeatureSnapshotManifest.from_payload(payload)
    if manifest is None:
        evidence["error"] = "feature_snapshot_manifest_invalid"
        return None, evidence
    current = bool(snapshot_is_current(manifest, config))  # type: ignore[arg-type]
    aligned = str(manifest.max_trade_date or manifest.trade_date) == as_of.isoformat()
    evidence.update(
        {
            "manifest_path": str(manifest_path),
            "trade_date": manifest.trade_date,
            "max_trade_date": manifest.max_trade_date,
            "coverage_ratio": manifest.coverage_ratio,
            "symbol_count": manifest.symbol_count,
            "failed_symbols": manifest.failed_symbols,
            "current": current,
            "aligned_to_as_of": aligned,
        }
    )
    if not aligned:
        # 快照覆盖的不是当天 → 不是"当天的特征证据"，如实报缺失。
        evidence["error"] = "feature_snapshot_date_not_aligned"
        return None, evidence
    return (
        {
            "current": current,
            "coverage_ratio": float(manifest.coverage_ratio),
            "trade_date": manifest.trade_date,
            "max_trade_date": manifest.max_trade_date,
            "symbol_count": int(manifest.symbol_count),
            "scope": manifest.scope,
            "universe_hash": manifest.universe_hash,
        },
        evidence,
    )


def derive_alpha_v2_model_identity(
    *, root: str | Path, epoch_id: str = ""
) -> tuple[dict[str, object] | None, dict[str, object]]:
    """active epoch 的 frozen shadow model 身份（**不是** legacy champion）。

    权威源是**磁盘冻结清单**（``validation_freeze_manifest.json``，受
    ``freeze_manifest_hash`` 锚定），不是内存里的 epoch identity——后者只是它的
    派生副本，且不同开 epoch 路径（CLI / 夹具）填充的键并不一致。

    验证链：冻结清单 model 块 ↔ epoch identity（在两者都有的键上必须一致）
    ↔ 磁盘工件 ``model_manifest.json`` ↔ 逐文件哈希重算（``load_frozen_model``）。
    全过才 ``identity_verified=true``；任一不一致 → 带 ``research_fail_closed`` 返回
    （S08 判 broken，该日不进数据健康）。
    """
    from stock_analyzer.alpha_v2.validation.epoch import active_epoch, get_epoch
    from stock_analyzer.alpha_v2.validation.freeze import load_validation_freeze
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        FrozenModelError,
        load_frozen_model,
    )

    evidence: dict[str, object] = {"artifact_root": str(root), "epoch_id": epoch_id}
    alpha_root = Path(root)
    try:
        epoch = get_epoch(alpha_root, epoch_id) if epoch_id else active_epoch(alpha_root)
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"epoch_registry_error:{exc.__class__.__name__}:{exc}"
        return None, evidence
    if epoch is None:
        evidence["error"] = "no_active_epoch"
        return None, evidence
    freeze = load_validation_freeze(alpha_root)
    if not isinstance(freeze, Mapping):
        evidence["error"] = "freeze_manifest_missing"
        return None, evidence
    freeze_model = dict(freeze.get("model") or {})
    epoch_identity = dict(epoch.identity)
    model_id = str(freeze_model.get("model_id", "") or epoch_identity.get("model_id", "") or "")
    expected_hash = str(
        freeze_model.get("artifact_hash", "") or epoch_identity.get("model_artifact_hash", "") or ""
    )
    expected_schema = str(
        freeze_model.get("feature_schema_hash", "")
        or freeze.get("feature_schema_hash", "")
        or epoch_identity.get("feature_schema_hash", "")
        or ""
    )
    training_commit = str(
        freeze_model.get("model_training_code_commit", "")
        or epoch_identity.get("model_training_code_commit", "")
        or ""
    )
    evidence.update(
        {
            "epoch_id": epoch.epoch_id,
            "model_id": model_id,
            "freeze_manifest_hash": str(freeze.get("freeze_manifest_hash", "")),
            "epoch_model_artifact_hash": str(epoch_identity.get("model_artifact_hash", "")),
            "freeze_model_artifact_hash": str(freeze_model.get("artifact_hash", "")),
            "model_training_code_commit": training_commit,
        }
    )
    payload: dict[str, object] = {
        "status": "unknown",
        "model_id": model_id,
        "artifact_hash": expected_hash,
        "feature_schema_hash": expected_schema,
        "model_training_code_commit": training_commit,
        "identity_verified": False,
        "scope": "alpha_v2_frozen_shadow_model",
    }
    if not model_id or not expected_hash:
        evidence["error"] = "freeze_model_identity_incomplete"
        payload["research_fail_closed"] = True
        return payload, evidence
    if not training_commit:
        evidence["error"] = "training_code_commit_missing"
        payload["research_fail_closed"] = True
        return payload, evidence
    model_dir = alpha_root / "model" / model_id
    evidence["model_dir"] = str(model_dir)
    if not model_dir.exists():
        evidence["error"] = "model_artifact_missing"
        payload["research_fail_closed"] = True
        return payload, evidence
    # 生产 epoch（validation_mode=production）额外要求工件已封存训练 provenance
    # （R1.1）：S08 的 model_identity 是"今天能不能用这份模型"的证据，若工件的
    # 训练输入身份可事后改写，这个"verified"就没有意义。test/rehearsal 保持宽松
    # （排演夹具允许只登记窗口的工件）。
    require_sealed = str(freeze.get("validation_mode", "")) == "production"
    evidence["require_sealed_provenance"] = require_sealed
    try:
        model = load_frozen_model(
            model_dir,
            expected_artifact_hash=expected_hash,
            require_sealed_provenance=require_sealed,
        )
    except FrozenModelError as exc:
        evidence["error"] = f"model_artifact_invalid:{exc}"
        payload["research_fail_closed"] = True
        payload["status"] = "mismatch"
        return payload, evidence
    manifest = dict(model.manifest)
    actual_schema = str(manifest.get("feature_schema_hash", "") or "")
    actual_commit = str(manifest.get("code_commit", "") or "")
    verified = (
        str(manifest.get("artifact_hash", "")) == expected_hash
        and bool(expected_schema)
        and actual_schema == expected_schema
        and str(manifest.get("model_id", "")) == model_id
        and actual_commit == training_commit
    )
    payload.update(
        {
            "status": "verified" if verified else "mismatch",
            "artifact_hash": str(manifest.get("artifact_hash", "")),
            "feature_schema_hash": actual_schema,
            "model_created_at": str(manifest.get("created_at", "")),
            "identity_verified": verified,
        }
    )
    evidence.update(
        {
            "artifact_feature_schema_hash": actual_schema,
            "artifact_training_code_commit": actual_commit,
            "artifact_created_at": str(manifest.get("created_at", "")),
        }
    )
    if not verified:
        payload["research_fail_closed"] = True
        evidence["error"] = "model_identity_mismatch"
    return payload, evidence


def derive_breadth_evidence(
    *,
    market_db: str | Path,
    as_of: date,
    config: object,
    evidence_path: str | Path,
    now: datetime | None = None,
) -> tuple[bool, dict[str, object]]:
    """现算广度（唯一 builder）并把证据写到影子路径；返回 (present, evidence)。

    ``present`` 只在"文件存在 + as_of 对齐 + freshness.date_max == as_of"时为真——
    缺失/错日一律 False（S08 判 degraded），不给假健康。
    """
    from stock_analyzer.data.market_warehouse import MarketWarehouse
    from stock_analyzer.ops.market_breadth import compute_market_breadth_from_warehouse

    detail: dict[str, object] = {"evidence_path": str(evidence_path)}
    try:
        warehouse = MarketWarehouse(
            db_path=market_db,
            package_root=Path(market_db).parent,
            package_writes_enabled=False,
            read_only=True,
        )
        snapshot = compute_market_breadth_from_warehouse(
            warehouse=warehouse,
            limit_rule=getattr(config, "limit_rule", None),
            now=now or datetime.now().astimezone(),
        )
    except Exception as exc:  # noqa: BLE001 - 广度算不出来 = 缺失
        detail["error"] = f"breadth_compute_failed:{exc.__class__.__name__}:{exc}"
        return False, detail
    if not snapshot:
        detail["error"] = "breadth_compute_unavailable"
        return False, detail
    freshness = snapshot.get("freshness")
    date_max = ""
    if isinstance(freshness, Mapping):
        date_max = str(freshness.get("date_max", "") or "")
    payload = {
        "schema": BREADTH_EVIDENCE_SCHEMA,
        "as_of": as_of.isoformat(),
        "computed_at": (now or datetime.now().astimezone()).isoformat(),
        "source": "compute_market_breadth_from_warehouse",
        "market_db": str(market_db),
        "snapshot": snapshot,
    }
    target = Path(evidence_path)
    detail.update({"date_max": date_max, "score": snapshot.get("score")})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        temp.replace(target)
    except OSError as exc:
        detail["error"] = f"breadth_evidence_write_failed:{exc.__class__.__name__}:{exc}"
        return False, detail
    if date_max != as_of.isoformat():
        detail["error"] = "breadth_evidence_date_not_aligned"
        return False, detail
    return True, detail


# ---------------------------------------------------------------------------
# Live feature 价格口径（P0 Final R1：捕获前的 prerequisite 门）
# ---------------------------------------------------------------------------

# 轻量认证窗口：与 preflight 的 probe 同一套参数（40 交易日 × ≤300 只），
# 不为检查口径装载全市场完整窗口。
FEATURE_PRICE_PROBE_DAYS = 40
FEATURE_PRICE_PROBE_SYMBOLS = 300
FEATURE_PRICE_PROBE_WARMUP_DAYS = 60

STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
STATUS_EXPECTED_MODE_MISSING = "expected_feature_mode_missing"
STATUS_UNPROVABLE = "unprovable"
STATUS_UNAVAILABLE = "unavailable"


def derive_feature_price_series_input(
    *,
    market_db: str | Path,
    expected_mode: str,
    as_of: date,
    probe_days: int = FEATURE_PRICE_PROBE_DAYS,
    probe_symbols: int = FEATURE_PRICE_PROBE_SYMBOLS,
    probe_warmup_days: int = FEATURE_PRICE_PROBE_WARMUP_DAYS,
) -> dict[str, object]:
    """当天 feature 行情库的价格口径证据（**判据只有一套**）。

    复用 ``preflight.probe_price_series_mode``（轻量 certify）与
    ``dual_price_series.require_declared_feature_series``（唯一契约判据）——本函数
    只负责把"抛异常"翻译成"可审计的三态证据"，不另立第二套比较逻辑：

    ```text
    ok                     expected 可证且与当天实测一致
    expected_feature_mode_missing  冻结模型没声明 feature 口径（生产 fail closed）
    mismatch               实测口径 != 冻结口径（train=qfq / live=raw 这类漂移）
    unprovable             当天库读得出，但口径不可证（unknown / 探针无样本）
    unavailable            库不可读 / 探针失败（数据未到位、库被锁等）
    ```

    只读；任何失败都不构造假 payload。
    """
    from stock_analyzer.alpha_v2.dual_price_series import (
        ROLE_FEATURE,
        PriceSeriesContractError,
        require_declared_feature_series,
    )
    from stock_analyzer.alpha_v2.research.panel import PriceModeCertification
    from stock_analyzer.alpha_v2.validation.preflight import probe_price_series_mode

    resolved_expected = str(expected_mode or "").strip().lower()
    evidence: dict[str, object] = {
        "schema": FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA,
        "source_db": str(market_db),
        "expected_mode": resolved_expected,
        "observed_mode": "",
        "contract_ok": False,
        "status": STATUS_UNPROVABLE,
        "reason": "",
        "as_of": as_of.isoformat(),
    }
    if not resolved_expected:
        evidence["status"] = STATUS_EXPECTED_MODE_MISSING
        evidence["reason"] = (
            "冻结模型未声明 feature 数据身份（provenance.feature_data_identity."
            "price_series_mode 缺失）——无法证明当天特征与训练同口径"
        )
        return evidence
    try:
        probe = probe_price_series_mode(
            Path(str(market_db)),
            as_of=as_of,
            days=int(probe_days),
            symbols=int(probe_symbols),
            warmup_days=int(probe_warmup_days),
        )
    except Exception as exc:  # noqa: BLE001 - 库不可读/探针失败都按"未就绪"处理
        evidence["status"] = STATUS_UNAVAILABLE
        evidence["reason"] = f"feature_price_probe_failed:{exc.__class__.__name__}:{exc}"
        return evidence
    observed = str(probe.get("price_series_mode", "") or "").strip().lower()
    certification = PriceModeCertification(
        mode=observed,
        source=str(probe.get("certification_source", "") or ""),
        certified=bool(probe.get("price_series_certified", False)),
        evidence=dict(probe.get("certification_evidence") or {}),
    )
    evidence.update(
        {
            "observed_mode": observed,
            "certification_source": certification.source,
            "certification_evidence": dict(certification.evidence),
            "probe_panel": dict(probe.get("panel") or {}),
        }
    )
    try:
        require_declared_feature_series(
            certification,
            context=f"live_feature_price_series(as_of={as_of.isoformat()})",
            expected_mode=resolved_expected,
            db=str(market_db),
        )
    except PriceSeriesContractError as exc:
        evidence["status"] = (
            STATUS_MISMATCH
            if observed in FEATURE_PRICE_MODE_ALLOWED
            else STATUS_UNPROVABLE
        )
        evidence["reason"] = str(exc)
        evidence["role"] = ROLE_FEATURE
        return evidence
    evidence["contract_ok"] = True
    evidence["status"] = STATUS_OK
    evidence["reason"] = ""
    return evidence


# ---------------------------------------------------------------------------
# 一步到位：派生全部 S08 输入
# ---------------------------------------------------------------------------

def derive_all_data_health_inputs(
    *,
    config: object,
    market_db: str | Path,
    as_of: date,
    alpha_v2_root: str | Path,
    breadth_evidence_path: str | Path,
    epoch_id: str = "",
    min_history_days: int | None = None,
    expected_active_lookback_days: int = DEFAULT_EXPECTED_ACTIVE_LOOKBACK_DAYS,
    max_freshness_days: int = 3,
    min_expected_active_coverage: float = 0.95,
    now: datetime | None = None,
) -> DerivedDataHealthInputs:
    """派生 S08 七项输入（缺哪项就如实缺哪项，绝不编造）。"""
    if min_history_days is None:
        min_history_days = int(
            getattr(getattr(config, "week5", None), "universe_quality_min_history_days", 60)
        )
    evidence: dict[str, object] = {}
    result = DerivedDataHealthInputs(evidence=evidence)

    result.latest_trade_date = latest_trade_date(market_db=market_db)
    evidence["latest_trade_date"] = result.latest_trade_date

    snapshot, valid_count, board_coverage, universe_evidence = derive_universe_facts(
        market_db=market_db,
        as_of=as_of,
        min_history_days=int(min_history_days),
        expected_active_lookback_days=int(expected_active_lookback_days),
    )
    result.universe_snapshot = snapshot
    result.valid_symbol_count = valid_count
    result.board_coverage = board_coverage
    evidence["universe"] = universe_evidence

    feature_input, feature_evidence = derive_feature_snapshot_input(config=config, as_of=as_of)
    result.feature_snapshot = feature_input
    evidence["feature_snapshot"] = feature_evidence

    model_input, model_evidence = derive_alpha_v2_model_identity(
        root=alpha_v2_root, epoch_id=epoch_id
    )
    result.model_identity = model_input
    evidence["model_identity"] = model_evidence

    breadth_present, breadth_evidence = derive_breadth_evidence(
        market_db=market_db,
        as_of=as_of,
        config=config,
        evidence_path=breadth_evidence_path,
        now=now,
    )
    result.breadth_artifact_present = breadth_present
    evidence["breadth"] = breadth_evidence

    evidence["thresholds"] = {
        "max_freshness_days": int(max_freshness_days),
        "min_expected_active_coverage": float(min_expected_active_coverage),
    }
    return result


__all__ = [
    "BREADTH_EVIDENCE_SCHEMA",
    "DEFAULT_BREADTH_EVIDENCE_RELATIVE",
    "DerivedDataHealthInputs",
    "FEATURE_PRICE_SERIES_EVIDENCE_SCHEMA",
    "LiveInputError",
    "derive_all_data_health_inputs",
    "derive_alpha_v2_model_identity",
    "derive_breadth_evidence",
    "derive_feature_price_series_input",
    "derive_feature_snapshot_input",
    "derive_universe_facts",
    "latest_trade_date",
]
