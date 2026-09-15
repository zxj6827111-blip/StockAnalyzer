# 集成分支上线前核对（2026-09-14，部署 NAS 前）

> 部署目标：`feat/learning-chain-integration-0914`（= A2/A1/A3/B1/B2 + C1 + C2 + B4，
> 本地全量质量门 `exit 0` / `blocking_failures=[]` / 覆盖率 79.35%）。
> 现状基线：NAS `.build_commit = 62a85d1`（容器 Up，RestartCount=0，docker enabled+active）。
> 目的：把"这次部署会改变什么"逐条量出来，而不是靠"应该没事"。

## 1. A2 校准器边界修正：对**在服模型**的影响已实测

在服 `/app/artifacts/model_v1.json` 的两个校准器（lgbm 356 段 / xgb 269 段）实测：

| 测法 | lgbm | xgb |
| --- | --- | --- |
| 0–1 均匀网格 10001 点，改前(`side=right`) vs 改后(`side=left`) 差异点 | **0** | **0** |
| 阶梯**去重端点**处差异点 | 3 / 10 | 6 / 9 |
| 端点处最大绝对差 | 0.540464 | 0.315314 |
| 差异方向 | 旧实现**过冲一步**（如 score=0.153782：旧 0.730175 → 新 0.189711） | 同（score=0.322946：旧 0.726797 → 新 0.411483） |

结论：A2 只在分数**恰好等于阶梯端点**时改变输出，且改变方向是把"过冲一步"纠正为契约规定的左开右闭区间。
原始模型分数是连续浮点，命中 10 个特定端点之一的频率可忽略（1 万点网格零命中）；
即便命中，也是纠正而非引入误差。**故这次部署不会让在服模型的日常输出漂移**。

## 2. B2 输出健康门 / 热载门 vs 在服工件

- 在服工件落盘指标**不含** B1 的输出语义字段（`scored_samples_*` / `unique_values_*`）→
  `evaluate_output_health` 判 `evaluable=False` → 走 legacy 分支**留审计放行**
  （`predictor_reload_output_health_legacy_skipped`），与测试
  `test_reload_allows_legacy_artifact_without_output_semantics` 一致。
- registry 17 条 legacy 记录的 `artifact_content_hash` **全为空串**，而热载撤销环按
  **content-hash** 匹配 → 在服文件不命中任何 revoked 记录 → 走"无匹配 + 无 active
  champion → 引导窗口放行"。**不会因撤销记录被拒载**（B3 审计 §2.1）。
- bootstrap 晋级门：在服工件指标同样无输出语义字段 → 输出健康部分不产生 blocking。

## 3. C2 输出语义：生产路径为**加法式诊断**

- `cross_review.passed` 在追加语义 reason **之前**已算完，语义只追加到 `result.reasons`；
- 唯一消费 `reasons` 的策略逻辑是**精确字符串集合匹配**
  （`strategy/soup.py:83` 的 `"degraded_consensus_lgbm_saturated" not in reasons`），
  新增的 `output_semantics:<sem>` 不命中任何既有分支；
- 概率健康快照新增 `output_semantics` 字典键：该 dict 本身是 `dict[str, object]` 且已含
  布尔键，消费方只有 `.get("promotion_allowed", True)` 与整体序列化 → 无 schema 破坏。

## 4. 只影响训练路径的改动（生产当前不训练）

A1（决策日粒度 purge）、B4（选池引用级读取）都只在
`_try_train_models_from_learning_protocol` 里生效；而 NAS 的
`SA__TRAINING__ENABLED` 被 `docker-compose.vendor-overlay.yml` / `advisory.yml`
**硬编码为 false**（后加载覆盖 `.env:70` 的 `true`，已实测容器内为 false）。
故这两项对今晚 21:45 的 `week5_night_scan` 无行为影响。

## 5. C1 的改动面

只动 `backtest/walk_forward_xsec.py` 与 `learning/scoring_eval.py`（离线 walk-forward
harness 与其 bootstrap），不在任何生产运行路径上。

## 6. 部署前置与回滚

- 前置：docker `enabled`+`active`；容器 5 个 Up、`RestartCount=0`；当前 `.build_commit=62a85d1`（回滚点）。
- 时间窗：避开 19:45 updater / 21:30–23:00 数据同步 / 21:45 night_scan；本次在交易日白天执行。
- 回滚：`git checkout 62a85d19b119cf700fa635d77909ccc4ac5e9386` 后重建（compose 文件集不变，
  无 DB schema 迁移、无 registry 写入、无 `.env` 变更）。
- 部署方式：遵守 NAS 约定——先 `source scripts/nas_compose_files.sh`，用
  `"${NAS_COMPOSE_ARGS[@]}"`，**不手工拼 `-f` 列表、不 `docker cp` 进生产容器**。

## 7. 遗留风险（不阻塞本次部署，但需记录）

1. `model_archive/model_v2_1f42dcd5e4aa0f05b355`（8/24 归档，指标与在服相同）在 registry
   中**无身份记录**（hash 命中 0 条）——身份链缺口，待 D 批由带真实 hash 的新工件替换别名时闭环。
2. 部署后**未验证**项：真实一次 night_scan 的端到端信号、B4 的 `memory.peak` 4 GiB 实测、
   C1 harness 在 NAS 数据上的复算（需 PIT 面板窗口）。
