"""Alpha V2 Validation Freeze Manifest（M3）。

**目的**：真实 OOS（Shadow 验证）开始后，不能一边看成绩一边改模型/口径——
否则"未来数据"又被拿来参与了选择，clean OOS 之名失效。上游五份方案文档
**没有**为这个阶段定义任何字段（2026-09-18 文档扫描确认：全文无
``freeze`` / ``epoch`` 字样），因此本文件的字段定义以 M3 阶段提示词 §3
为权威来源。

一份 freeze manifest 回答："这个 validation epoch 里，代码/配置/模型/特征
schema/标签口径/选择契约/基准定义/样本门，分别是什么，哈希是多少"。

三条纪律：

1. **缺失字段必须显式**。模型工件还没冻结时写 ``pending_freeze`` 之类的
   显式状态，而不是省略键——验收方要区分"没做"与"做了但为空"；
2. **哈希可复算**。``freeze_manifest_hash`` 对除自身外的全字段做
   canonical JSON + sha256，任何事后改字都会被
   :func:`verify_freeze_integrity` 抓住；
3. **冻结对象与冻结时间分离**：``validation_start_date`` 是真实 OOS 第一个
   交易日的占位；清单可在 shadow 启动**之前**生成（``validation_start_date``
   为 ``None``），启动时回填（见 :func:`seal_freeze_manifest`——回填属于
   写入新文件，整体哈希随之更新并落审计，不是静默改历史）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.research.benchmarks import (
    DEFAULT_LAYERS,
    PRIMARY_LAYER,
    BenchmarkSpec,
)
from stock_analyzer.alpha_v2.research.outcomes import LABEL_V2_SCHEMA, OutcomeSpec
from stock_analyzer.config_identity import stable_payload_hash

FREEZE_MANIFEST_SCHEMA = "alpha_v2_validation_freeze.v1"
FREEZE_MANIFEST_FILENAME = "validation_freeze_manifest.json"
VALIDATION_DIRNAME = "validation"

# 主 horizon=5D、确认 horizon=3D 是 M3 提示词 §5 的业务口径（蓝图 §1.2 同向）。
PRIMARY_BUSINESS_HORIZON = 5
CONFIRMATION_HORIZON = 3

# 基准集合在 epoch 内冻结（M3 §12）；层名与 S12 的 BenchmarkSpec 对齐。
FROZEN_BENCHMARK_LAYERS: tuple[str, ...] = tuple(DEFAULT_LAYERS)

# 样本门（蓝图 §7.4 / M3 §14，逐值一一对应，语义不重命名）。
SAMPLE_GATES: dict[str, int] = {
    "failure_alert": 20,
    "direction_review": 60,
    "advisory_discussion": 120,
    "auto_governance": 250,
}

# 没有合法 OOS 校准时的方向分标记（M3 §6：不得制造伪概率）。
CALIBRATION_NONE = "none"

PENDING_MODEL_STATUS = "pending_freeze"
FROZEN_MODEL_STATUS = "frozen"

# 冻结清单必须全部给出的键（assert 会逐项核对）。
REQUIRED_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "schema",
    "validation_epoch_id",
    "code_commit",
    "git_branch",
    "config_hash",
    "config_hash_scope",
    "model",
    "feature_schema_id",
    "feature_schema_hash",
    "label_policy_id",
    "label_policy_hash",
    "selection_contract_id",
    "quality_target",
    "light_target",
    "deep_target",
    "final_cap",
    "execution_price_mode",
    "feature_price_mode",
    "primary_business_horizon",
    "confirmation_horizon",
    "horizons",
    "benchmarks",
    "sample_gates",
    "validation_start_date",
    "created_at",
    "policy_freeze",
    "validation_mode",
    # R3：确定性时钟授权（生产 freeze CLI 恒 false；只有测试夹具会置 true）
    "deterministic_clock",
    "deterministic_clock_source",
)

REQUIRED_MODEL_FIELDS: tuple[str, ...] = (
    "model_id",
    "artifact_hash",
    "artifact_created_at",
    "artifact_path",
    "status",
    "calibration",
    "provenance",
    # R4.1：训练身份是模型身份块的一部分（存在性强制；取值是否可证由生产门禁判定）
    "model_training_code_commit",
)


class FreezeIncompleteError(ValueError):
    """冻结清单缺关键字段时的显式失败。"""


def label_policy_payload(spec: OutcomeSpec | None = None) -> dict[str, object]:
    """标签口径自述（id + 参数全文）；哈希以此为输入。"""
    resolved = spec or OutcomeSpec()
    payload = resolved.to_payload()
    payload["policy_id"] = str(payload.get("schema", LABEL_V2_SCHEMA))
    return payload


def feature_schema_payload(
    feature_columns: Sequence[str], *, group_ids: Sequence[str]
) -> dict[str, object]:
    """特征 schema 自述：列集合（排序）+ 组集合；列顺序不是身份，列集合是。"""
    return {
        "schema_id": "alpha_v2_base_features_v1",
        "feature_columns": sorted(str(column) for column in feature_columns),
        "group_ids": sorted(str(group) for group in group_ids),
    }


def feature_schema_hash_of(feature_columns: Sequence[str]) -> str:
    """特征集合的唯一哈希。冻结清单与冻结模型工件**必须用同一算法/同一输入**——
    否则 epoch.identity.feature_schema_hash 与运行时来自模型工件的哈希永远不可比
    （本轮修复 B4 的另一半）。"""
    return stable_payload_hash({"feature_columns": sorted(str(c) for c in feature_columns)})


def benchmark_freeze_payload(spec: BenchmarkSpec | None = None) -> dict[str, object]:
    """冻结的基准定义（M3 §12）：层集合 + 口径全文哈希，epoch 内不得调整。"""
    resolved = spec or BenchmarkSpec()
    payload = resolved.to_payload()
    return {
        "layers": list(FROZEN_BENCHMARK_LAYERS),
        "primary_layer": PRIMARY_LAYER,
        "definition": payload,
        "definition_hash": stable_payload_hash(payload),
        "freeze_rule": "benchmark 定义在 validation epoch 内冻结；不得按结果换基准",
    }


def build_validation_freeze(
    *,
    validation_epoch_id: str,
    code_commit: str,
    git_branch: str,
    config_hash: str,
    config_hash_scope: str,
    model: Mapping[str, object] | None = None,
    feature_columns: Sequence[str] = (),
    feature_group_ids: Sequence[str] = (),
    selection_contract: Mapping[str, object] | None = None,
    execution_price_mode: str,
    feature_price_mode: str,
    outcome_spec: OutcomeSpec | None = None,
    benchmark_spec: BenchmarkSpec | None = None,
    validation_start_date: str | None = None,
    created_at: str,
    legacy_invariants: Mapping[str, object] | None = None,
    validation_mode: str = "production",
    feature_schema_source: str = "cli_provided",
    deterministic_clock: bool = False,
    deterministic_clock_source: str = "",
) -> dict[str, object]:
    """构造冻结清单（纯计算，不落盘；缺失的模型信息以显式状态表达）。

    ``model`` 应为冻结模型工件清单里的身份段（model_id/artifact_hash/...），
    缺省或与工件对不上时写 ``status=pending_freeze``——shadow 侧把它当作
    "模型身份未锚定"，仍是合法但会反映在 readiness 报告里的状态。
    """
    label_policy = label_policy_payload(outcome_spec)
    label_policy_hash = stable_payload_hash(label_policy)
    schema_payload = feature_schema_payload(feature_columns, group_ids=feature_group_ids)

    contract = dict(selection_contract or {})
    contract_id = str(
        contract.get("selection_contract_id", contract.get("contract_id", "night_alpha_v2_v1"))
    )
    model_block = _normalize_model_block(model)
    # 模型未冻结时整个清单仍然成立，但绝不伪造一个 hash 上去。
    if model_block["status"] != FROZEN_MODEL_STATUS:
        model_block["artifact_hash"] = str(model_block["artifact_hash"] or "pending_freeze")

    manifest: dict[str, object] = {
        "schema": FREEZE_MANIFEST_SCHEMA,
        "validation_epoch_id": str(validation_epoch_id),
        "code_commit": str(code_commit),
        "git_branch": str(git_branch),
        "config_hash": str(config_hash),
        "config_hash_scope": str(config_hash_scope),
        "model": model_block,
        "feature_schema_id": str(schema_payload["schema_id"]),
        # 与 frozen model manifest 同一个哈希算法（修复 B4 的"两份 schema hash 不可比"）
        "feature_schema_hash": feature_schema_hash_of(feature_columns),
        "feature_schema": schema_payload,
        "label_policy_id": str(label_policy["policy_id"]),
        "label_policy_hash": label_policy_hash,
        "label_policy": label_policy,
        "selection_contract_id": contract_id,
        "selection_contract": contract,
        "quality_target": _contract_int(contract, "quality_target", 300),
        "light_target": _contract_int(contract, "light_target", 100),
        "deep_target": _contract_int(contract, "deep_target", 50),
        "final_cap": _contract_int(contract, "final_cap", 5),
        "execution_price_mode": str(execution_price_mode),
        "feature_price_mode": str(feature_price_mode),
        "primary_business_horizon": PRIMARY_BUSINESS_HORIZON,
        "confirmation_horizon": CONFIRMATION_HORIZON,
        "horizons": [3, 5, 10, 15],
        "benchmarks": benchmark_freeze_payload(benchmark_spec),
        "sample_gates": dict(SAMPLE_GATES),
        "validation_start_date": validation_start_date,
        "validation_mode": str(validation_mode),
        "feature_schema_source": str(feature_schema_source),
        # R3：确定性时钟只对 rehearsal/test 开放；生产 freeze CLI 不提供开启入口。
        "deterministic_clock": bool(deterministic_clock),
        "deterministic_clock_source": str(deterministic_clock_source),
        "created_at": str(created_at),
        "policy_freeze": {
            # M3 §13：OOS 窗口内这些一律不得调整（蓝图 §18 的禁止清单原样引用）。
            "no_threshold_changes": [
                "final_signal_min_threshold",
                "cross_review_thresholds",
                "model_weights",
                "feature_set",
                "label_policy",
                "selection_contract",
            ],
            "rule": "修改任一冻结对象 => 关闭当前 epoch，重新验收，开启新 epoch",
            "legacy_invariants": dict(legacy_invariants or {}),
        },
    }
    manifest["freeze_manifest_hash"] = freeze_manifest_hash(manifest)
    return manifest


def _normalize_model_block(model: Mapping[str, object] | None) -> dict[str, object]:
    block = dict(model or {})
    status = str(block.get("status", "")).strip()
    has_hash = bool(str(block.get("artifact_hash", "") or "").strip())
    if not status:
        status = FROZEN_MODEL_STATUS if has_hash else PENDING_MODEL_STATUS
    return {
        "model_id": str(block.get("model_id", "") or ("" if status != FROZEN_MODEL_STATUS else "")),
        "artifact_hash": str(block.get("artifact_hash", "") or ""),
        "artifact_created_at": str(block.get("artifact_created_at", "") or ""),
        "artifact_path": str(block.get("artifact_path", "") or ""),
        "heads": list(block.get("heads", []) or []),
        "calibration": dict(block.get("calibration", {}) or {}),
        "status": status,
        "provenance": dict(block.get("provenance", {}) or {}),
        # R4.1：规范化必须**保留**训练身份，否则 CLI 传进来的值会在写入前被静默丢掉
        "model_training_code_commit": str(block.get("model_training_code_commit", "") or ""),
    }


def _contract_int(contract: Mapping[str, object], key: str, default: int) -> int:
    value = contract.get(key, default)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return int(default)


def freeze_manifest_hash(payload: Mapping[str, object]) -> str:
    """除 ``freeze_manifest_hash`` 字段本身外的 canonical JSON sha256。"""
    body = {key: value for key, value in payload.items() if key != "freeze_manifest_hash"}
    serialized = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def freeze_manifest_path(root: str | Path) -> Path:
    return Path(root) / VALIDATION_DIRNAME / FREEZE_MANIFEST_FILENAME


def write_validation_freeze(
    payload: Mapping[str, object], *, root: str | Path
) -> Path:
    """原子落盘；写前强制完整性检查——不完整的冻结清单不允许落盘。"""
    assert_freeze_complete(payload)
    recorded = str(payload.get("freeze_manifest_hash", "") or "")
    computed = freeze_manifest_hash(payload)
    if recorded and recorded != computed:
        raise FreezeIncompleteError(
            f"freeze_manifest_hash 与内容不一致（{recorded[:12]}… != {computed[:12]}…）；"
            "请用 freeze_manifest_hash() 重算后重写，不要手改清单"
        )
    body = dict(payload)
    body["freeze_manifest_hash"] = computed
    return write_json_atomic(freeze_manifest_path(root), body)


def load_validation_freeze(root: str | Path) -> dict[str, object] | None:
    path = freeze_manifest_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def verify_freeze_integrity(payload: Mapping[str, object]) -> bool:
    """哈希完整性核验；缺 hash 或被篡改返回 False。"""
    recorded = str(payload.get("freeze_manifest_hash", "") or "")
    if not recorded:
        return False
    return recorded == freeze_manifest_hash(payload)


def assert_freeze_complete(payload: Mapping[str, object]) -> None:
    """缺键即抛错（M3 顶层字段必须全部显式存在；取值可写 not_available）。"""
    missing = [field for field in REQUIRED_TOP_LEVEL_FIELDS if field not in payload]
    if missing:
        raise FreezeIncompleteError(f"freeze manifest 缺字段: {missing}")
    if payload.get("schema") != FREEZE_MANIFEST_SCHEMA:
        raise FreezeIncompleteError(
            f"schema 必须是 {FREEZE_MANIFEST_SCHEMA}，收到 {payload.get('schema')!r}"
        )
    if str(payload.get("code_commit", "")).strip() == "":
        raise FreezeCompleteKeyError("code_commit")
    for field in ("config_hash", "feature_schema_hash", "label_policy_hash"):
        if not str(payload.get(field, "")).strip():
            raise FreezeCompleteKeyError(field)
    model = payload.get("model")
    if not isinstance(model, Mapping):
        raise FreezeIncompleteError("model 必须是映射")
    missing_model = [field for field in REQUIRED_MODEL_FIELDS if field not in model]
    if missing_model:
        raise FreezeIncompleteError(f"model 块缺字段: {missing_model}")


class FreezeCompleteKeyError(FreezeIncompleteError):
    """单个关键键为空的细分错误（便于测试断言具体字段）。"""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"freeze manifest 的关键字段不得为空: {field}")


def verify_freeze_against_runtime(
    manifest: Mapping[str, object],
    *,
    code_commit: str | None = None,
    config_hash: str | None = None,
    model_artifact_hash: str | None = None,
    model_training_code_commit: str | None = None,
    feature_schema_hash: str | None = None,
    label_policy_hash: str | None = None,
) -> list[str]:
    """把运行侧身份与冻结清单逐项对账；返回违例清单（空 = 一致）。

    这是 read-only 校验（不抛错），供 readiness 报告与每日 shadow 自检使用：
    任何一项不一致都意味着"当前运行已不在被冻结的口径上"。
    ``model_training_code_commit`` 为 R4.1 新增项：传 ``None`` 时跳过（不传给老调用方
    加一个"必须知道模型训练身份"的新前提）。
    """
    violations: list[str] = []
    if not verify_freeze_integrity(manifest):
        violations.append("freeze_manifest_hash_mismatch_or_missing")
    checks = (
        ("code_commit", code_commit, manifest.get("code_commit")),
        ("config_hash", config_hash, manifest.get("config_hash")),
        ("feature_schema_hash", feature_schema_hash, manifest.get("feature_schema_hash")),
        ("label_policy_hash", label_policy_hash, manifest.get("label_policy_hash")),
    )
    model = manifest.get("model")
    if isinstance(model, Mapping):
        checks += (
            ("model_artifact_hash", model_artifact_hash, model.get("artifact_hash")),
            (
                "model_training_code_commit",
                model_training_code_commit,
                model.get("model_training_code_commit"),
            ),
        )
    for name, actual, expected in checks:
        if actual is None:
            continue
        if str(actual) != str(expected):
            violations.append(f"{name}_mismatch:{str(expected)[:16]}!={str(actual)[:16]}")
    return violations


def seal_freeze_manifest(
    existing: Mapping[str, object], *, validation_start_date: str, sealed_at: str
) -> dict[str, object]:
    """首个 shadow 交易日回填 ``validation_start_date``（防呆：只能回填一次）。

    回填后哈希随之更新——这是有意的：旧哈希与新哈希都留在 git/审计轨迹里，
    任何"回填过"的事实都可见。
    """
    if existing.get("validation_start_date"):
        raise ValueError(
            "validation_start_date 已回填（"
            f"{existing.get('validation_start_date')}），epoch 的验证起点不可改写"
        )
    payload = dict(existing)
    payload["validation_start_date"] = str(validation_start_date)
    payload["created_at"] = str(payload.get("created_at") or sealed_at)
    payload["sealed_at"] = str(sealed_at)
    payload["freeze_manifest_hash"] = freeze_manifest_hash(payload)
    return payload


__all__ = [
    "CALIBRATION_NONE",
    "CONFIRMATION_HORIZON",
    "FREEZE_MANIFEST_FILENAME",
    "FREEZE_MANIFEST_SCHEMA",
    "FROZEN_BENCHMARK_LAYERS",
    "FROZEN_MODEL_STATUS",
    "FreezeCompleteKeyError",
    "FreezeIncompleteError",
    "PENDING_MODEL_STATUS",
    "PRIMARY_BUSINESS_HORIZON",
    "REQUIRED_MODEL_FIELDS",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "SAMPLE_GATES",
    "VALIDATION_DIRNAME",
    "assert_freeze_complete",
    "benchmark_freeze_payload",
    "build_validation_freeze",
    "feature_schema_hash_of",
    "feature_schema_payload",
    "freeze_manifest_hash",
    "freeze_manifest_path",
    "label_policy_payload",
    "load_validation_freeze",
    "seal_freeze_manifest",
    "verify_freeze_against_runtime",
    "verify_freeze_integrity",
    "write_validation_freeze",
]
