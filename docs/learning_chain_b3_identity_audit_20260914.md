# 学习链整改 B3：身份链与在服工件只读审计（2026-09-14）

> 材料性质：**只读审计记录**（不写 registry、不改 DB、不部署）。
> 采集时刻：NAS 本地时间 2026-09-14 07:39–08:05（+08:00），构建 `62a85d1`。
> 采集方式：`docker exec -i stock-analyzer-api python -` 读 DuckDB `read_only=True` + JSON 文件；
> 主机侧只读命令。未使用任何会写审计/登记记录的服务层接口。

---

## §0 前置：9/12–9/13 漏跑窗口判定（L4）

### §0.1 事实

| 项 | 观测值 | 来源 |
| --- | --- | --- |
| NAS 当前时间 | 2026-09-14 07:39 +08（uptime 9:33，开机 9/13 22:06:50） | `date -Is; uptime` |
| 构建 | `62a85d1`（本地 A/B 分支未部署） | `.build_commit` |
| 容器 | 5 个 Up，`RestartCount=0`，StartedAt=2026-09-14T01:00:36+08 | `docker ps` / `docker inspect` |
| `week5_night_scan` | 最后一次 9/11（周五）21:45 → success 22:09（detail `week5_automation:empty`）；9/12、9/13 无尝试 | `scheduler_heavy_state.json` |
| `evolution_offhours` | 9/12（周六）21:45 → success 22:24；**9/13（周日）21:45 → failed**，`last_failure=supervisor_restarted_before_child_recovery`（9/14 01:01 补记） | 同上 |
| 周日扫描进度 | `status=failed`，`phase=final_pipeline`，**43/120**，elapsed 20.2 min，`error_summary=ConnectionError: Error -3 connecting to redis:6379. Temporary failure in name resolution.` | `/app/artifacts/runtime/week5_scan_progress.json` |
| 训练库 | 只读连接成功，8 张表（`model_registry` 18 行）；**未被重启写坏** | `duckdb.connect(read_only=True)` |
| readiness | `target_trade_date=2026-09-11`，daily/delta/index 全 `ok`（5537/5537） | `nightly_data_ready.json` |
| 主机 cron | 数据链全部 `* * 1-5`（周末不跑，设计内）；`financial_update.sh` 为 `0 21 * * 0` | `crontab -l` |

### §0.2 判定：**不补跑**

按"补跑合格判据"逐条核对，第 (e)(f) 条不通过：

| 判据 | 结果 | 依据 |
| --- | --- | --- |
| (a) 非自愈（下一个自然窗口远） | 通过 | 下一窗口=9/19 周六 |
| (b) 根因是瞬时基础设施而非未修缺陷 | 通过 | 主机 22:06 关机 → 容器内 DNS 解析失败 → 扫描中断；非代码缺陷 |
| (c) 前置就绪 | 通过 | readiness 就位（9/11 为最近交易日）、无残留锁（`scheduler_job_locks` 空）、容器健康 |
| (d) 重跑不破坏状态 | 通过 | `Week5ScanProgress` 文档注释明确"进度文件只是可观测性，不得影响扫描语义"；重跑覆盖写 |
| **(e) 不与自然周期冲突** | **不通过** | 9/14 是交易日：09:25 起有 5 个 radar 槽位，21:45 是生产 `week5_night_scan`（heavy 组 `max_concurrency=1`） |
| **(f) 产出有增量价值** | **不通过** | 周六/周日同属 `weekend_full_deep` 档（`weekday>=5` 同分支、同 `universe_max_symbols`）；两天输入基准同为最近交易日 9/11 → 周日重跑复现的是**已在 9/12 发布**的那次扫描（`week5_scan_latest.timestamp=2026-09-12T21:45:26`），且 14 小时后会被周一新数据的自然窗口取代 |

结论：该次失败只影响一次"输入基准与已发布结果重复"的探索性全量扫描，无信息增量，且补跑与交易日周期冲突 → **不补跑**；改为盯 9/14 21:45 自然窗口。

### §0.3 窗口内无其它漏跑

`week5_weekend_learning`（9/12 12:32）为 `skipped/detail=backoff` 且 `last_success` 同刻 → 已成功；
`daily_news_sync` / `theme_daily_sync` / `week4_acceptance` 均 9/11 成功；
`factor_ic_decay_report`（8/31）、`week5_automation_market_radar_1`（9/11 09:25）为窗口外旧失败。

---

## §1 在服工件身份（`/app/artifacts/model_v1.json`）

| 字段 | 值 |
| --- | --- |
| 文件 sha256 | `71f64a21c131fe594d871bf4eaa87e75d8667fb722556de0f2f85a8607cb2559` |
| size / mtime | 32200 B / 2026-08-16 19:06:43 |
| version | `v2` |
| label 契约 | `label_policy_id=label_policy_v1_e2afc1135a3f`，hash `e2afc1135a3f…`（**v1 soup：TP/SL 路径标签**） |
| feature schema | `285626b6…`；197 列 |
| manifest | `dataset_manifest_v1_50ec7236be71` |
| 训练指标 | `auc=0.331355`、`accuracy=0.661077`、`brier=0.218951`、`positive_rate=0.238313`、**`mean_prob_spread=-0.158217`**、`embargo_days=0`、`calibration_samples=3734`、`test_samples=3936` |
| 校准器 | `lgbm_calibrator` 356 段、`xgb_calibrator` 269 段；**`y_hat` 前 353/261 段恒为 0.0**；`x_right` 大量重复值（如 0.006856 连续 50 段） |
| metadata | `calibration_method=isotonic`、`embargo_days=0`、`label_conflict_policy=bar_shape_heuristic`、`meta_blend_weights={lgbm:0.517, xgb:0.483}` |

**质量资格判定：不合格（ineligible）**，三条独立理由：
1. `auc=0.331 < 0.5`（劣于随机）且 `mean_prob_spread=-0.158`（负分离）；
2. 标签契约是 v1 soup，**与整改后生产链的 `return_rank` v3 契约不同基**（不可与 v3 结果同表比较）；
3. isotonic 阶梯实测近似塌成常数（353/356、261/269 段输出 0.0），输出语义不可用。

→ 按 B3 规则"倒序登记 ≠ 授予质量批准"，**该工件不授予任何质量批准，也不得作为 champion 登记**。

## §2 registry 对账

- `model_registry` 共 18 行：**17 行 legacy 全部 `lifecycle_state=revoked`**（2026-09-05T05:00:21 统一撤销），`blocked_reason` 含 `quarantine:empty_content_hash` + `quarantine:legacy_alias_pointer` / `artifact_uri_points_to_manifest_json` / `orig:artifact_overwritten`；第 18 行为 9/13 challenger `model_v2_e0e264cb3547ca9ec95d`（`trained`，`artifact_content_hash=e0e264cb…` 非空）。
- 指向 `/app/artifacts/model_v1.json` 的记录 2 条（`model_v1_a5b4842e85a3`、`model_v1_prod_bootstrap_existing`），**两条 `artifact_content_hash` 均为空串**。
- 按在服文件真实 sha256 反查：**命中 0 条** → 在服工件的身份**未被任何 registry 记录绑定**（文件已脱钩）。
- 归档 `model_archive/model_v2_1f42dcd5e4aa0f05b355/model.json`（8/24，sha256 `e5541b4a…`，指标与在服工件完全相同：auc 0.331355 / positive_rate 0.238313）**同样 0 条 registry 记录** → 孤儿归档。

### §2.1 热载门禁影响核实（重要）

`_validated_predictor_reload` 的撤销环按 **content-hash** 匹配（`compute_artifact_identity_hash` vs `record.artifact_content_hash`），
**不按 `artifact_uri`**。因 17 条 legacy 记录 hash 为空、在服文件真实 hash 无记录命中：

- 在服模型落到"无匹配 + 无 active champion → 引导窗口放行（留审计事件）"分支；
- **不会被 B2 新增的 `blocked/revoked` 环误拒**（该环只对 hash 命中的撤销记录生效）。

即：部署 B2 后重启容器不会因撤销记录拒载在服模型。此结论仅在"不写入带真实 hash 的 revoked 记录"前提下成立。

### §2.2 决策：**本轮不写任何 registry 记录**

理由（按 B3 规则与"登记≠批准"原则）：
- 以真实 hash 登记在服工件为 `revoked`，会让热载撤销环**开始命中**该 hash → 下次重启拒载在服模型（自伤）；
- 登记为 `trained`/`approved` 等于对 AUC 0.331 的旧基工件背书 → 违反质量资格判定；
- 正确顺序是"身份留痕 → 质量判定 → 仅在 D 批受控晋升中由**新 champion**（带真实 hash、v3 契约）替换旧别名"。

因此身份链缺口以**本文档留痕**（文件 sha256 + 契约 + 指标），待 D 批替换别名时一并闭环。

## §3 A3 部署就绪性核实（本次顺带完成）

NAS 上 `label_policy_registry` 已有 v3 行 `label_policy_v3_b0b3724553b5`（`label_return_rank` / schema 3 / horizon 10 / `rank_quantile` / `next_tradable_open` / maturity `label_mature_time_v1`），
但表结构**无** `top_quantile/bottom_quantile/drop_middle/min_cross_section` 列（A3 尚未部署）。

- 本地 A3 分支用默认参数复算：`build_return_rank_policy_record(horizon_days=10)`
  → `label_policy_v3_b0b3724553b5` / `b0b3724553b53de40e6ba8c125feaedfa6523aa94e54ed3073f4579435e48732`
  → **与 NAS 行一致（MATCH: True）**，参数 `(0.3, 0.3, True, 30)`。
- NAS 运行期有效配置：`labels.basis=return_rank`，`return_rank_top_quantile=0.3 / bottom_quantile=0.3 / drop_middle=True / min_cross_section=30`（无 `.env` 覆盖，仅 156 行设 `BASIS`）。

结论：A3 的受控迁移路径可命中（`hash_matches_return_rank_params` 为真），**不会因缺参拒训**；无需改配置。
（A2 的边界修正不回溯修复既有工件——见 §1 校准器实测。）

## §4 附带发现（未修，待拍板）

1. **`financial_update.sh` 周日作业连续失败**：9/13 21:00、9/6 21:00 均 `rc=1`，
   `FileNotFoundError: /app/artifacts/universe_all.txt`（`scripts/backfill_financial_snapshots.py:259`）。
   属慢性缺陷（非窗口内偶发），**补跑无意义**，需修正输入来源或脚本口径。
2. **训练开关由 compose overlay 钉死**：`.env:70 SA__TRAINING__ENABLED=true` 但容器内为 `false` ——
   `docker-compose.vendor-overlay.yml` / `docker-compose.advisory.yml` 硬编码 `"false"`（后加载覆盖）。
   即 `.env` 该行**无效**，改它不会打开训练；重训须走受控手工路径或改 overlay。
3. **9/13 challenger 校准塌缩实测**：`xgb_calibrator` **仅 1 段**（常数）、`lgbm_calibrator` 5 段；
   测试集 `positive_rate=0.993729`、`mean_prob_spread=0.000272`（2392 行几乎全部判正），
   `dataset_naive_compounded_nav=1e+18` 触发既有 `nav_compounding_explosion` 硬门（故 NO-GO 登记为 challenger）。
   这些是 B1 的 raw/cal 分离与 B2 输出健康门要拿到的直接证据形态。
4. **manifest 行数上限生效**：`dataset_manifest_v2_8b250aa25009` 的 `sample_selection_rule` 含 `snapshots=40000`，
   `included_snapshot_count=included_outcome_count=40000`（`SA__TRAINING__BOOTSTRAP_DATASET_MAX_ROWS=40000`），
   且 `manifest_quality_flags_json=[]`（无 A1 的隔离/purge 留痕）。→ C4 行数上限与 A1 回归口径直接相关。
