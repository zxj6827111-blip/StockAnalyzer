# Week5 方向一' 实施任务：label 改收益排序语义 + NaN 特征接入 + 重训 18-fold 验证

> 本任务是已拍板方案的落地实施。上游分析结论已定案（见「必读文件」），
> 不要重新论证方向选择，直接实施。实施完成后由另一个会话复核。

## 背景（30 秒版）

Week5 Phase 2 walk-forward 18/18 fold 全市场 PIT 口径下模型分数 IC=-0.024（CI 全负）→ NO-GO。
归因扫描（352 评估日 × 208 特征）+ 双口径诊断已定案根因：

1. **标签/评估口径错位（核心根因）**：训练标签是 TP/SL 路径标签（T+1 开盘入场、
   10 日内先摸 +8% → label=1，先跌 5% → label=0，冲突 soft 0.5）。该标签奖励
   "彩票性"——高波动/高动量票容易先摸 +8%（模型预测它的 AUC=0.586），但同批
   票 10 日期末总收益反而低（对 fwd_return 的 IC 全负）。63 个显著负 IC 特征里
   62 个对 label 的 AUC 显著 >0.5，IC-AUC 相关系数 -0.518。模型学的是 label
   教的事，但 label 教的不是选股系统要的。
2. **98/208 个特征全 NaN**：holder_count、北向、融资、分钟收益、moneyflow、
   30+ 个 bg_* 财务背景字段。这些是 9/5 data_gate 修复回填进 market.duckdb 的
   字段，但 PIT 快照生成链没消费（数据集 9/6 凌晨生成，早于回填生效）。训练时
   这些列是常量/零填充，模型从未见过真实值。

## 必读文件（开工前通读，顺序即依赖顺序）

1. `docs/week5_backtest_remediation_plan_20260831.md` —— 总整改方案（Phase 0 硬门、
   时间不变量、验收框架）
2. `docs/week5_phase0_audit_20260831.md` —— Phase 0 审计（B1-B10 修复记录；
   §10.1 时间戳/锚点字段、conflict_flag 持久化等约束必须保留）
3. `docs/week5_feature_attribution_20260907.md` —— 归因报告（**§6 双口径诊断 = 本任务依据**，
   含方向一'完整论证与验收标准）
4. `src/stock_analyzer/labels/soup.py` —— 现行标签实现（T+1 open 锚点在此，勿动）
5. `src/stock_analyzer/backtest/pit_dataset.py` —— PIT 数据集生成器（NaN 接入改造点）
6. `src/stock_analyzer/backtest/walk_forward_xsec.py` —— 18-fold harness（复用其
   PitDatasetStore/plan_folds/run_fold/checkpoint 机制）
7. `scripts/week5_feature_attribution.py` / `scripts/week5_dual_metric_diagnosis.py` ——
   归因工具（重训后复验用同一批工具口径）

## 实施内容（三个子任务，一次重训到位）

### 任务 1：新增收益排序 label（新 policy，旧 policy 共存）

在 label_policy_registry（版本化、immutable、hash 绑定——见 Phase 0 审计 §2.1）
新增一个 label policy，例如 `cross_sectional_rank`：

- 语义：以 fwd_return 为基础，按**同一交易日横截面**计算分位——
  top 30% 记 1、bottom 30% 记 0、中间 30% 记 0.5（soft；或剔除中间段，
  实现时二选一并写明理由）
- 时间语义：保留全部 Phase 0 不变量——decision_time、T+1 open 入场锚点、
  label_mature_trade_date（horizon=10）、embargo（horizon + settlement_lag）。
  **只换 label 公式，不换任何时间锚点。**
- 关键实现约束：fwd_return 的横截面分位必须在 PIT 数据集生成时计算
  （逐日横截面，无前视——分位只依赖当日截面内的 fwd_return，而 fwd_return
  本身就是未来窗口的，训练时 embargo 已保证隔离）
- 旧 soup TP/SL policy 不删除不修改（registry 版本化共存，历史快照不受影响）
- 配置落点：`LabelsConfig` 新增 basis/policy 字段默认值保持旧行为不变，
  新行为通过显式配置开启（避免影响生产链路）；default.yaml 同步注释

### 任务 2：98 个 NaN 特征接入 PIT 快照链

- 定位 `pit_dataset.py` 生成器读取特征的路径，确认 98 个全 NaN 字段
  （清单在 `backfill_tmp/feature_attribution_20260907.json` 里 ic_mean 为 NaN
  的条目）是"生成时列存在但源数据没回填进来"还是"生成器根本没读这些列"——
  先查清是哪一种，再改
- 数据在 market.duckdb 已回填（9/5 data_gate 修复，`prewarm` 覆盖率验证过 8 字段
  全 1.0）——需要的是让 PIT 快照生成链消费它们
- PIT 纪律：这些是 as-of 数据（holder_count 按披露日、北向/融资按交易日），
  接入时按已有的 PIT 对齐模式（参考 `warehouse_enrichment.py` 的
  `enrich_daily_financial_pit` 既有实现），**严禁把未来披露映射到过去日期**
- bg_is_st 等背景字段如仍无历史数据（只在回填日之后有值），如实保留 NaN，
  不要假填充

### 任务 3：重建数据集 + 重训 + 18-fold 验证（NAS 执行）

- 重建 PIT 数据集（`pit_dataset_ext` 换新目录，旧的保留勿覆盖——磁盘空间
  允许的话；`pit_generate.log` 显示全量生成约 513s）
- 18-fold walk-forward 重训重验（复用 harness 的 checkpoint 机制；
  新数据集新 out-dir）
- **验收硬门（勿降标准）**：模型分数 IC 的 date-block bootstrap 95% CI
  下界 > 0（不只是均值转正）。若未达标，如实报告数字并停在第 6 节的
  排查清单，不要继续调参硬凑
- 对照基线：同一 harness 配置下，旧 soup label 的 IC=-0.024 CI=[-0.043,-0.005]
  必须出现在新报告里作为对照行

## 工程纪律（Phase 2 已踩的坑，勿重蹈）

- **内存**：单评估日单次 SQL 拉全特征；日期谓词必须 ISO 字符串直比
  （CAST 退化全表扫描曾顶爆 4GiB）；训练下采样在 SQL 内做（fetch 后 pandas
  副本曾是 OOM 根因）；每 fold 新建 trainer（LGBM/XGBoost C 层分配器跨 fold
  泄漏 ~1.6GB）；任何"逐日累积不释放"的缓冲都是泄漏源（归因 runner 首版
  因此被杀）
- **执行窗口**：避开 NAS 21:30-23:00 cron（sync/updater/night_scan）；
  scheduler-critical 容器 3GiB 限额（2026-09-07 已从 2G 上调，OOM 定案见
  docs 归因报告同日 commit）
- **容器与代码同步**：NAS 容器是旧镜像时新脚本要 `docker cp` 进容器
  （scripts 在镜像层 COPY，宿主 git pull 不更新容器内文件）；`docker exec -d`
  必须 `</dev/null`；正式部署走 `nas_deploy_update.sh`（含镜像重建）
- **测试**：新 label policy 必须有对抗测试（时间不变量、横截面分位无前视、
  中间段处理边界）；特征接入要有覆盖率测试（用 9/5 归因 JSON 里的 NaN 清单
  做回归断言）；全量测试套件跑通 + ruff + mypy 基线对比无新增
- **分支**：`feat/week5-label-remediation-0907`，完成后 PR 合 main（用户惯例：
  先评审后合并，提交信息中文、含根因/验证数据）

## 验收清单（复核会话将按此检查）

- [ ] label_policy_registry 新 policy 注册 + hash 绑定 + 对抗测试（时间不变量 0 违规）
- [ ] 98 个 NaN 特征在**新数据集**里非 NaN 覆盖率显著 >0（逐特征报告，允许
      bg_is_st 类历史无数据的字段保留 NaN 并说明）
- [ ] 新数据集 pit_meta.json（rows/symbols/trade_dates 与旧版同量级差异可解释）
- [ ] 18-fold 报告：新 label IC + CI、对照行（旧 label -0.024）、lookahead
      violations=0
- [ ] 硬门判定行：IC CI 下界 > 0 → PASS / 否则 FAIL（如实）
- [ ] 生产链路零影响（旧 policy 默认值不变、night_scan/asof 回测行为不变）
- [ ] 测试套件全绿 + ruff/mypy 基线无新增 + 提交历史干净（无凭据/临时文件）

## NAS 环境速查（2026-09-07 时点）

- SSH：`192.168.10.26`（内网）/ `100.114.122.111`（Tailscale，paramiko 需
  banner_timeout=60）；用户 `zxj6827111`；凭证不入任何文件/命令行历史
  （用 ~/.kiro/nas_credentials.json 或一次性脚本用后即删）
- 仓库：`/vol1/docker/StockAnalyzer`；数据集：
  `/vol1/docker/volumes/stock_analyzer_runtime_artifacts/_data/phase2/`
  （pit_dataset_ext 旧版 240 万行、checkpoints/fold_01-18.json、归因结果
  feature_attribution_20260907T170151.json、双口径 feature_dual_metric_20260907T175409.json）
- 容器：scheduler-critical（3GiB，跑分析任务）、scheduler-heavy（3GiB）、
  api（4GiB）；compose 基线必须 `source scripts/nas_compose_files.sh` 禁止手拼 -f
