"""在服工件的身份对账：把「进程实际加载的文件」与「注册表登记的 champion」对起来。

**为什么需要它**（2026-09-16 实证）：`registry` 有 `artifact_content_hash` 列、`bundle.py`
有 `compute_artifact_identity_hash`，但**加载路径从不参与**——`AnalyzerPipeline` 是按
`config.training.artifact_path` 直接读文件的。后果：

- 生产实际在跑哪个工件，只能靠文件 mtime 旁证（我排查 raw A/B 自检失败时就吃了这个亏，
  只能猜"是不是工件被换过"）；
- 注册表里唯一指向该文件的行是 `revoked` + `blocked_reason=quarantine:empty_content_hash`，
  而在服文件的哈希谁都没算过——**"登记 ≠ 发布 ≠ 热载"没有可验证的连接**。

本模块只做**判定**，不做门禁：返回状态与两侧哈希，由调用方决定报警还是拒绝。
把判定与执行分开，是为了让"先观测、再决定要不要强制"这条路径可走。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# 状态取值（稳定契约，供接口与测试引用）
IDENTITY_MATCH = "match"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_NO_CHAMPION = "no_champion"
IDENTITY_CHAMPION_HASH_MISSING = "champion_hash_missing"
IDENTITY_LOADED_HASH_MISSING = "loaded_hash_missing"
IDENTITY_REGISTRY_UNAVAILABLE = "registry_unavailable"
# 注册表被**本进程持有的写锁**挡住 —— 属"这次读不到"，不是"身份对不上"。
# 2026-09-16 盘中实测：巡检 10:50 报 registry_unavailable 被当 defect，其实只是 api 容器
# 正在写这个库。探测器假警报一次就会被当噪音，必须与真错误分开。
IDENTITY_REGISTRY_BUSY = "registry_busy"
# 在服工件与某条**已登记**记录的内容哈希一致，但那条不是 champion（或压根没有
# champion）：身份**可验证**了，只是"批准"缺位。二者必须分开——2026-09-16 定案。
IDENTITY_MATCH_REGISTERED = "match_registered"

IDENTITY_STATUSES = (
    IDENTITY_MATCH,
    IDENTITY_MISMATCH,
    IDENTITY_NO_CHAMPION,
    IDENTITY_CHAMPION_HASH_MISSING,
    IDENTITY_LOADED_HASH_MISSING,
    IDENTITY_REGISTRY_UNAVAILABLE,
    IDENTITY_REGISTRY_BUSY,
    IDENTITY_MATCH_REGISTERED,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def _first_hash_match(
    records: Sequence[Mapping[str, Any]], loaded_hash: str
) -> Mapping[str, Any] | None:
    """按内容哈希在登记记录里找第一条匹配（大小写不敏感；空哈希不参与匹配）。"""
    target = _text(loaded_hash).lower()
    if not target:
        return None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if _text(record.get("artifact_content_hash")).lower() == target:
            return record
    return None


def content_hash_matches_stamp(*, claimed: object, actual: object) -> bool | None:
    """发布时**盖章**的哈希 vs 加载时按**实际文件**算出的哈希。

    ``None`` 表示无法判定（任一侧为空）——**不得**把"无法判定"读成 True：没有哈希
    不等于哈希一致，这正是 2026-09-05 那条 champion 被 ``quarantine:empty_content_hash``
    拦下的原因。返回 False 才是硬信号：磁盘上的工件与发布时不是同一个。
    """
    claimed_text = _text(claimed).lower()
    actual_text = _text(actual).lower()
    if not claimed_text or not actual_text:
        return None
    return claimed_text == actual_text


def describe_artifact_identity(
    *,
    loaded_uri: object,
    loaded_hash: object,
    champion: Mapping[str, Any] | None = None,
    registered: Sequence[Mapping[str, Any]] = (),
    registry_error: str = "",
    registry_busy: bool = False,
) -> dict[str, object]:
    """判定在服工件与注册表 champion 的身份关系。

    判定顺序（先排除"无法判定"，再判等）：
    1. ``registry_busy`` → ``registry_busy``（本进程的写锁挡着，属"这次读不到"）；
    2. ``registry_error`` 非空 → ``registry_unavailable``（读不到注册表 ≠ 身份不一致）；
    3. 在服工件没算出哈希 → ``loaded_hash_missing``（无法比对，绝不能当成 match）；
    4. ``champion`` 为 None → ``no_champion``；
    5. champion 的 ``artifact_content_hash`` 为空 → ``champion_hash_missing``
       （这正是 2026-09-05 quarantine 把唯一那条 champion 记录拦下的原因）；
    6. 两侧哈希相等 → ``match``，否则 ``mismatch``。

    注意哈希比较**大小写不敏感、去空白**：registry 侧历史上写入过带大写/空白的值，
    格式差异不该被读成"身份不符"（那会制造假警报），但也绝不把空串当相等。
    """
    loaded_hash_text = _text(loaded_hash)
    loaded_uri_text = _text(loaded_uri)
    payload: dict[str, object] = {
        "status": IDENTITY_REGISTRY_UNAVAILABLE,
        "loaded_uri": loaded_uri_text,
        "loaded_content_hash": loaded_hash_text,
        "champion_model_id": "",
        "champion_content_hash": "",
        "detail": "",
    }
    if registry_busy:
        payload["status"] = IDENTITY_REGISTRY_BUSY
        payload["detail"] = "注册表被本进程的写锁占用，这次读不到（属暂时读不到，不是身份不符）" + (
            f": {registry_error}" if registry_error else ""
        )
        return payload
    if registry_error:
        payload["detail"] = f"注册表不可读: {registry_error}"
        return payload
    if not loaded_hash_text:
        payload["status"] = IDENTITY_LOADED_HASH_MISSING
        payload["detail"] = "在服工件未计算内容哈希，身份无法验证"
        return payload
    if champion is None:
        # 身份"可验证"与"已批准"分开：没有 champion 时，仍可能与某条 challenger/trained
        # 记录同哈希——那说明"这就是登记过的那份内容"，只是没人批准它。
        same = _first_hash_match(registered, loaded_hash_text)
        if same is not None:
            payload["status"] = IDENTITY_MATCH_REGISTERED
            payload["champion_model_id"] = _text(same.get("model_id"))
            payload["detail"] = (
                f"在服工件 == 登记记录 {_text(same.get('model_id'))}"
                f"（{_text(same.get('lifecycle_state')) or 'unknown'}），但注册表无 champion"
            )
            return payload
        payload["status"] = IDENTITY_NO_CHAMPION
        payload["detail"] = "注册表没有 champion，在服工件无登记身份"
        return payload

    champion_id = _text(champion.get("model_id"))
    champion_hash = _text(champion.get("artifact_content_hash"))
    payload["champion_model_id"] = champion_id
    payload["champion_content_hash"] = champion_hash
    if not champion_hash:
        payload["status"] = IDENTITY_CHAMPION_HASH_MISSING
        payload["detail"] = f"champion {champion_id or '(unknown)'} 没有内容哈希，身份无法验证"
        return payload
    if champion_hash.lower() == loaded_hash_text.lower():
        payload["status"] = IDENTITY_MATCH
        payload["detail"] = f"在服工件 == champion {champion_id}"
    else:
        payload["status"] = IDENTITY_MISMATCH
        payload["detail"] = (
            f"在服工件与 champion {champion_id} 的内容哈希不同（"
            f"loaded={loaded_hash_text[:12]}… champion={champion_hash[:12]}…）"
        )
    return payload


# ---------------------------------------------------------------------------
# S01（Alpha V2 M1）：真实模型身份链
#
# 上面的判定函数回答"两侧哈希关系如何"；本节回答"身份事实从哪来、谁能覆盖谁"。
#
# 唯一真相源规则（S01 验收项）：**Pipeline 实际加载谁，报告就必须报告谁**。
#   事实（facts）= 磁盘/加载路径上的 artifact：内容哈希、created_at、feature schema、
#                 label policy、dataset manifest；
#   补充（supplement）= registry 登记信息与 bootstrap 运行时状态：只能标注"这份内容在
#                 注册表里叫什么/被谁批准过"，**不得覆盖事实**。
#
# 2026-09-17 之前的实际行为违反这条：``week5_historical_runner._resolve_model_info``
# 与 ``asof_backtest_service`` 在找不到 champion 时把 **bootstrap 的
# ``last_bootstrap_at`` 当成 ``model_trained_at`` 报出去**——于是报告写 2026-09-15
# 训练、实际加载的却是 2026-08-16 的工件（蓝图 §2.9）。S01 把"事实"与"补充"分开记录，
# 并对"已证实不符/无法比对"的身份 fail-closed 到研究侧。
# ---------------------------------------------------------------------------

# 事实字段（稳定契约）：取值必须来自 artifact 本身或加载进程的直接观测。
MODEL_IDENTITY_FACT_KEYS = (
    "artifact_uri",
    "artifact_exists",
    "artifact_content_hash",
    "artifact_created_at",
    "feature_schema_id",
    "feature_schema_hash",
    "label_policy_id",
    "label_policy_hash",
    "dataset_manifest_id",
)

# 研究侧 fail-closed 的状态：只含"已证实不符"与"无法比对"两类**硬信号**。
# 刻意不含 no_champion / registry_unavailable / registry_busy：
#   - no_champion：身份可验证（match_registered）但没有批准记录，属治理缺口，
#     当前生产就是这个状态（蓝图 §2.5），一刀切会把整条研究链锁死；
#   - registry 读不到 / 写锁占用：属"这次判不了"，不是"身份不符"（假警报会让
#     探测器被当噪音，见本模块开头 2026-09-16 记录）。
IDENTITY_RESEARCH_FAIL_CLOSED_STATUSES = (
    IDENTITY_MISMATCH,
    IDENTITY_LOADED_HASH_MISSING,
    IDENTITY_CHAMPION_HASH_MISSING,
)

# 身份"可验证"的状态（事实与登记内容对得上）。
IDENTITY_VERIFIED_STATUSES = (IDENTITY_MATCH, IDENTITY_MATCH_REGISTERED)


def research_fail_closed(status: object) -> bool:
    """该身份状态是否必须让 V2 研究/回测拒绝出结果。"""
    return _text(status) in IDENTITY_RESEARCH_FAIL_CLOSED_STATUSES


def identity_verified(status: object) -> bool:
    """该身份状态是否已"可验证"（而非仅"可读"）。"""
    return _text(status) in IDENTITY_VERIFIED_STATUSES


def load_artifact_facts(artifact_path: str | Path) -> dict[str, object]:
    """从磁盘工件读"事实"身份（只读 JSON 头字段，不解模型后端）。

    供**没有已加载 predictor** 的调用方使用：例如 asof 回测在跑扫描前先固定一次
    身份，避免把运行期 bootstrap 时间当模型身份报出去。

    任何一步失败只写进 ``load_error`` / ``artifact_exists``，不抛异常：身份必须
    永远可报告——"读不到"本身就是要被记录的事实。
    """
    from stock_analyzer.models.artifact import ModelArtifact  # noqa: WPS433 - 避免环导入
    from stock_analyzer.models.bundle import compute_artifact_identity_hash  # noqa: WPS433

    resolved = Path(artifact_path).expanduser()
    facts: dict[str, object] = {
        "artifact_uri": str(resolved),
        "artifact_exists": False,
        "artifact_content_hash": "",
        "artifact_created_at": "",
        "feature_schema_id": "",
        "feature_schema_hash": "",
        "label_policy_id": "",
        "label_policy_hash": "",
        "dataset_manifest_id": "",
        "load_error": "",
    }
    if not resolved.exists():
        facts["load_error"] = "artifact_missing"
        return facts
    facts["artifact_exists"] = True
    try:
        artifact = ModelArtifact.load(resolved)
    except Exception as exc:  # noqa: BLE001 - 身份读取不得抛
        facts["load_error"] = f"artifact_load_failed:{type(exc).__name__}"
    else:
        facts.update(
            {
                "artifact_created_at": _text(artifact.created_at),
                "feature_schema_id": _text(artifact.feature_schema_id),
                "feature_schema_hash": _text(artifact.feature_schema_hash),
                "label_policy_id": _text(artifact.label_policy_id),
                "label_policy_hash": _text(artifact.label_policy_hash),
                "dataset_manifest_id": _text(artifact.dataset_manifest_id),
            }
        )
    try:
        facts["artifact_content_hash"] = _text(compute_artifact_identity_hash(resolved))
    except Exception as exc:  # noqa: BLE001 - 同上门禁
        facts["load_error"] = facts["load_error"] or f"hash_failed:{type(exc).__name__}"
    return facts


def registry_identity(registry: object | None) -> dict[str, object]:
    """只读收集 registry 侧补充身份（champion / 登记清单 / 错误）。

    与 ``service.artifact_identity_report`` 原本的内联逻辑同源；抽出来是为了让历史/
    研究路径与生产健康端点用**同一套**判定输入，避免两处口径漂移。

    ``registry_busy`` 一律为 False：本函数**观测到写锁占用**的能力有限，
    不从异常文本里猜（"db locked" 这类文本既可能是本进程持锁，也可能是别的进程
    持锁或其它 IO 错误）。生产健康端点的既有契约是把读失败报成
    ``registry_unavailable``，这里保持同口径；确实知道自己持写锁的调用方
    （如巡检的直连兜底路径）自行构造 snapshot 传 ``registry_snapshot``。
    """
    payload: dict[str, object] = {
        "champion": None,
        "registered": [],
        "registry_error": "",
        "registry_busy": False,
    }
    if registry is None:
        return payload
    champion: dict[str, object] | None = None
    try:
        record = registry.active_champion(suppress_read_errors=True)  # type: ignore[attr-defined]
        if record is not None:
            champion = {
                "model_id": getattr(record, "model_id", ""),
                "artifact_uri": getattr(record, "artifact_uri", ""),
                "artifact_content_hash": getattr(record, "artifact_content_hash", ""),
                "lifecycle_state": str(getattr(record, "lifecycle_state", "")),
            }
    except Exception as exc:  # noqa: BLE001 - 读不到注册表要如实上报
        payload["registry_error"] = f"{type(exc).__name__}: {exc}"
    registered: list[dict[str, object]] = []
    try:
        registered = [
            {
                "model_id": getattr(item, "model_id", ""),
                "artifact_content_hash": getattr(item, "artifact_content_hash", ""),
                "lifecycle_state": str(getattr(item, "lifecycle_state", "")),
            }
            for item in registry.list_records(  # type: ignore[attr-defined]
                limit=200, suppress_read_errors=True
            )
        ]
    except Exception:  # noqa: BLE001 - 登记清单读不到只降级为"这半判不了"
        registered = []
    payload["champion"] = champion
    payload["registered"] = registered
    return payload


def build_model_identity_report(
    facts: Mapping[str, Any],
    *,
    registry: object | None = None,
    claimed_content_hash: object = "",
    registry_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """合并"工件事实 + registry 补充"，输出可审计的身份报告。

    Args:
        facts: 事实字段（见 :data:`MODEL_IDENTITY_FACT_KEYS`），至少含
            ``artifact_uri`` 与 ``artifact_content_hash``。
        registry: 注册表对象（鸭子类型：``active_champion`` / ``list_records``）。
            None 表示调用方不持有注册表，此时判定为 ``registry_unavailable``。
        claimed_content_hash: 发布时**盖章**的哈希（bundle 自描述），与加载期实算
            哈希比对得到 ``content_hash_verified``（三态 True/False/None）。
        registry_snapshot: 已收集好的 registry 身份，避免重复读库。

    Returns:
        报告 dict：事实字段原样透传 + ``status``（六态判定）+ registry 补充字段
        （**一律带 ``registry_`` / ``champion_`` / ``claimed_`` 前缀**，结构上区分
        "补充"与"事实"，防止下游把登记值当真相读）+ ``research_fail_closed`` /
        ``identity_verified``。
    """
    if registry_snapshot is not None:
        snapshot = dict(registry_snapshot)
    elif registry is not None:
        snapshot = registry_identity(registry)
    else:
        # 未附加注册表**不是**错误（`registry_unavailable` 会掩盖真正的工件问题，
        # 例如工件缺失导致的 loaded_hash_missing）。这里按"没有登记信息"处理：
        # champion=None / registered=[] / 无错误，由 ``registry_attached=False``
        # 让调用方知道"状态里没有 registry 那一半"。
        snapshot = {
            "champion": None,
            "registered": [],
            "registry_error": "",
            "registry_busy": False,
        }
    champion = snapshot.get("champion")
    registered = snapshot.get("registered")
    status_payload = describe_artifact_identity(
        loaded_uri=facts.get("artifact_uri", ""),
        loaded_hash=facts.get("artifact_content_hash", ""),
        champion=champion if isinstance(champion, Mapping) else None,
        registered=registered if isinstance(registered, list) else [],
        registry_error=_text(snapshot.get("registry_error", "")),
        registry_busy=bool(snapshot.get("registry_busy", False)),
    )
    report: dict[str, object] = {key: facts.get(key, "") for key in MODEL_IDENTITY_FACT_KEYS}
    for extra_key in (
        "predictor_loaded",
        "artifact_path_requested",
        "score_source",
        "output_semantics",
        "inference_allowed",
        "inference_blocked_reason",
        "load_error",
    ):
        if extra_key in facts:
            report[extra_key] = facts[extra_key]
    report.update(
        {
            "status": status_payload.get("status", ""),
            "detail": status_payload.get("detail", ""),
            "claimed_content_hash": _text(claimed_content_hash),
            "content_hash_verified": content_hash_matches_stamp(
                claimed=claimed_content_hash,
                actual=facts.get("artifact_content_hash", ""),
            ),
        }
    )
    report["registry_model_id"] = _text(status_payload.get("champion_model_id", ""))
    report["registry_content_hash"] = _text(status_payload.get("champion_content_hash", ""))
    report["registry_error"] = _text(snapshot.get("registry_error", ""))
    report["registry_busy"] = bool(snapshot.get("registry_busy", False))
    report["registry_attached"] = registry is not None or registry_snapshot is not None
    report["research_fail_closed"] = research_fail_closed(report["status"])
    report["identity_verified"] = identity_verified(report["status"])
    return report
