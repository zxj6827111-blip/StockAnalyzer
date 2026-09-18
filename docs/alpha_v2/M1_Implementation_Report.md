# M1 Implementation Report（S00–S10，Alpha V2.0 Correctness Foundation）

> 批次：`M1 = S00–S10`
> 分支：`feat/alpha-v2-m1-0917`（无 upstream，未 push）
> 评审基线：`7e9e33bdb9d03506cff5dfff29c78b1c95019541`
> 本报告落盘目的：Codex N4（M1 施工报告归档，便于独立验收追溯）
> 生成日期：2026-09-18

---

## 1. Batch Status

```text
Batch = M1
Status = DONE（等待 Codex 整批验收）
Stages Completed = S00 S01 S02 S03 S04 S05 S06 S07 S08 S09 S10
Codex Acceptance = PENDING
```

首轮 Codex 验收：**FAIL**（唯一原因＝批次不完整，S03–S10 未实施；S00–S02 判定 PASS）。
本轮按「先验收后继续」补齐 S03–S10，并逐条落实复审要求（N1–N5、DF-S02-001/002/003）。

## 2. Preflight

```text
branch = feat/alpha-v2-m1-0917
starting HEAD = 7e9e33bdb9d03506cff5dfff29c78b1c95019541（与评审基线一致）
working tree = 仅 1 个用户未跟踪文件 docs/system_issues_for_review_20260917.md（未动）
```

## 3. Commit 链

```text
83e4fd7 S00 Feature Flag / No-op Baseline
f6ed795 S00 PROGRESS 记录
a78a990 S01 Model Identity Truth
5b1e504 S02 T+1 Entry Simulation
faf0a1e M1 批次小结（首轮）
3d919c7 S03 PIT Historical Universe
89f08f0 S04 SelectionContract 300/100/50
6243cd0 S05 Registry / Archive Governance
8d3248d S06 HistoricalModelResolver
1a199a6 S07 Feature / Execution Price Split
053b4b1 S08 Data Health / Breadth Split
d0a58f7 S09 Model / Label Semantic Guard
be28ff9 S10 Decision Log + Outcome Maturation
```

## 4. Stage Matrix

| Stage | Task | 状态 | 定向测试 | 关键交付 |
|---|---|---|---|---|
| S00 | Feature Flag / No-op Baseline | DONE | 40 | `alpha_v2` 配置块（默认关）/ 审计基座 / golden 契约 / 架构守卫 |
| S01 | Model Identity Truth | DONE | 447 | 事实（工件）与补充（registry/bootstrap）分离；`trained_at` 取工件 created_at |
| S02 | T+1 Entry Simulation | DONE | 197 | `simulate_entry`；`entry_date > signal_date` 否则 `no_fill` |
| S03 | PIT Historical Universe | DONE | 162 | `asof_universe`；未来上市硬排除 + expected_active 分母 + 快照 id |
| S04 | SelectionContract 300/100/50 | DONE | 139 | `contracts/alpha_v2.py`；历史 night-equivalent 与夜扫同契约 |
| S05 | Registry / Archive Governance | DONE | 109 | serving manifest / 六类对账 / 归档容量管理 |
| S06 | HistoricalModelResolver | DONE | 135 | 两模式时间闸门；无合法模型 → `unscorable`，绝不回退在服模型 |
| S07 | Feature / Execution Price Split | DONE | 154 | price contract；修 DF-S02-001/002/003；N3 措辞 |
| S08 | Data Health / Breadth Split | DONE | 58 | 七项健康检查 + 分层门；缺失不得 healthy；灰度默认只观测 |
| S09 | Model / Label Semantic Guard | DONE | 55 | `output_kind` 声明 + 展示口径守卫；legacy 只标 degraded_unverified |
| S10 | Decision Log + Outcome Maturation | DONE | 13 | decisions/outcomes/manifests JSONL；成熟门；不编造 V2 Head 字段 |

## 5. 行为变化（V2 新增 vs Legacy 不变量）

**新增**：V2 feature flag 与审计基座；pipeline 只读模型身份；T+1 可成交入场；PIT 历史股票池；
SelectionContract；serving manifest 与对账；历史模型解析闸门；价格口径契约；数据健康分层门；
语义守卫；决策/outcome 台账。

**修正的真实缺陷**：
1. 报告模型身份与实际加载工件脱钩（bootstrap 时间冒充训练时间）；
2. 盘后信号用 T 日收盘价成交（不可实现成交）；
3. 历史股票池透传当前索引（未来上市票可入选）；
4. 历史 100/100/20 vs 夜扫 300/100/50 不可比；
5. 历史回测可加载 as_of 之后创建的模型；
6. 缺列时注入 ±10% 估算涨跌停（掩盖 ST/20%/IPO 真实幅度，一字涨停被判可成交）；
7. NaN 涨跌停价 → 涨停门 fail-open；
8. 执行滑点主口径 = 0。

**Legacy 不变量（未改）**：`final_signal_min_threshold=70`；Cross Review 四阈值；
night 300/100/50 与 final cap 数值；风险门（breadth/overextension/board）；
在服模型与 challenger；飞书正式通知；生产 live 决策路径（S04 保留 live 缺省 legacy 目标）。

## 5b. 第二轮 Codex 验收（半批审）修复

第一轮 S00–S02 通过；第二轮（完整 S00–S10）判 **FAIL**，原因是三个实证缺陷与三个必修项，
本轮已全部修复并各自带回归/对抗测试：

| 项 | 缺陷 | 修复 | 回归证据 |
|---|---|---|---|
| **B1**（阻断） | S06 解析结果与加载路径脱钩："resolve 了但不加载 resolved 的那个"，config 指向更新的在服工件时未来模型进历史回测 | scorable 后把加载路径绑定到 `resolution.artifact_uri`；加载后复核实算哈希与 `created_at <= decision_time`，不符即 `unscorable` | helper 级 3 例 + **runner 级 1 例**（报告哈希 = 旧 PIT 哈希 ≠ 新在服哈希） |
| **B2**（阻断） | `analyze_holding_curve` 无 `slippage_ratio` 参数，服务层按关键字传入 → 有候选必 TypeError（滑点修复空转） | 补参数透传；端到端夹具改为真的产生候选 | 端到端断言"有候选 → holding 段存在且 `entry_mode=next_session_open`" |
| **B3**（阻断） | NaN 封堵只到 `limit_rule`，引擎/matcher 数值层仍 fail-open（整列 NaN + 无 pre_close 的一字涨停可成交） | `engine`/`matcher` 的 `_optional_numeric` 统一过滤 NaN/Inf | Case B 形态回归 + Inf 同口径回归 |
| N1 | `prune_model_bundle_archive` 容量分支 no-op | 预算约束整个归档：预算内不删、超预算从最旧删到进预算、不低于保留下限 | 两条回归（预算充裕不删 / 超预算删到进预算） |
| N2 | `valid_symbol_count=None` 被当 1.0 判 ok | 分子缺失 → `degraded` + 进 `missing_artifacts` | 回归 1 例 |
| N3 | `compute_outcomes` 用裸默认 matcher | 支持 `matcher`/`config` 入参复用运行配置 | 一致性回归 1 例 |
| N4 | `test_nightly_scheduling` 墙钟敏感（午间静默窗） | 用例显式清空 `quiet_windows` | 该文件 25 passed |

## 6. Codex 复审要求落实

| 项 | 落实位置 |
|---|---|
| N1 Stage Matrix 重复行 | `docs/alpha_v2/PROGRESS.md` |
| N2 工件缺失硬拒绝（与 registry 状态无关） | `models/historical_resolver.py::_reject_reason`（首条判定）+ 测试 |
| N3 `EntrySimulation.slippage` 措辞 | `backtest/matcher.py` docstring + 测试 |
| N4 本报告落盘 | 本文件 |
| N5 在服权威口径 | `models/serving_manifest.py::authority` + 对账报告 `authority_note` |
| DF-S02-001 | `backtest/holding_curve.py`、`backtest/walk_forward.py`（删除估算注入） |
| DF-S02-002 | `data/limit_rule.py::_optional_float`（NaN/Inf → 缺失） |
| DF-S02-003 | `runtime/services/asof_backtest_service.py`（策略静态滑点） |

## 7. 测试与证据

- 各阶段定向测试：见 §4（合计 1509，含跨阶段重复计入的回归集）。
- 批次级集成：`python -m pytest -n 4 --dist loadfile`
  → **3202 passed / 2 skipped / 0 failed（633.33s）**，在 S00–S10 + 半批审修复全部提交后运行
  （见 `artifacts/alpha_v2/audit/m1_summary.json` 的 `batch_tests`）。
- 审计工件：`artifacts/alpha_v2/audit/{baseline_manifest,s00..s10_validation,model_registry_reconciliation,m1_summary}.json`。
- 测试隔离修复（本批次自查发现）：S06/S07 夹具最初向**共享** `learning_protocol.duckdb` 写模型，
  多 xdist worker 并发时撞 DuckDB 锁 → 间歇性失败；已改为进程内 registry 桩（2/2 压力复跑通过）。

## 8. 已知未闭合项（如实记录，不构成"已修复"）

| ID | 级别 | 内容 | 目标 |
|---|---|---|---|
| DF-S00-001 | low | `model_dump()`→`model_validate` 整份配置不可回填（limit_rule alias `from`） | S04/M2 |
| —— | 已闭合 | B1/B2/B3（第二轮阻断项）、N1/N2/N3/N4 均已修复并带回归 | 本轮 |
| DF-S03-001 | medium | 上市日仍为"窗口内 bar 数"代理（新上市 vs 长期停牌不可区分） | 数据侧/M2 |
| DF-S03-002 | medium | 当日停牌票仍进 expected_active 分母（执行层 no_fill 兜住） | S08 |
| DF-S05-001 | info | 本机 registry 0 行；权威对账需 NAS 跑同一 CLI | 部署期 |
| DF-S06-001 | medium | 无逐日激活历史 → strict_production_replay 当前几乎恒 unscorable | M2 |
| DF-S06-002 | medium | asof（非 week5）回测的 as_of 闸门未接 | M2 |
| DF-S07-001 | high | 本机受跟踪执行口径 = qfq（生产应为 raw）；守卫会标 execution_uncertain | 部署核验 |
| DF-S07-002 | medium | corporate action 完整治理未做（当前以 execution_uncertain 标注） | M2 |
| DF-S08-001/002 | medium/high | 生产侧 Data Health 数据源接线 + live 广度缺失 fail-open 接线 | 部署灰度期 |
| DF-S09-001 | medium | 生产 basis `soup_10d_tp8_before_sl5` 未登记 → 在服语义 unknown（方向安全） | M2 |
| DF-S09-002 | medium | UI/飞书文案层替换 | M2 (S21) |
| DF-S10-001/002 | medium | 决策日志与 outcome 成熟未接生产调度 | M2 (S20/S21) |

## 9. 回滚

- 分阶段：`git revert <stage commit>`；整批：删除分支 `feat/alpha-v2-m1-0917`。
- 无生产影响：未 push、未部署、未重启容器、未改 .env；`artifacts/alpha_v2/**` 为新增目录。
- 配置向后兼容：无 `alpha_v2` 块的历史配置仍可加载（有测试覆盖）。

## 10. Codex Acceptance

**PENDING** —— 建议重点复核：
0. 半批审 B1/B2/B3 的对抗测试是否确实拦住原缺陷（B1 runner 级、B2 端到端 holding 段、B3 Case B）；
1. S03 未来上市票是否**同时**排除出 universe 与覆盖率分母；
2. S04 生产夜扫与历史 night-equivalent 是否都报 `night_alpha_v2_v1` 与 300/100/50；
3. S06 是否存在任何"回退当前在服模型"的路径（应为零）；
4. S07 三条执行缺陷是否真的闭合（NaN / 估算涨跌停 / 0 滑点）；
5. S08 的"缺失不得 healthy"与"coverage 坏但广度高分不得放行"；
6. S09 非概率输出是否仍可能被文案概率化；
7. S10 outcome 是否可能在信号当天写入（应为零）。

## 11. Next Batch

**LOCKED** —— 等待 Codex M1 验收 PASS 后才进入 M2（S11–S23）。
