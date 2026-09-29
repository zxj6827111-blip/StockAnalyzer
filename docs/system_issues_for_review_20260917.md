# StockAnalyzer 选股链路问题汇总（2026-09-17）

供独立评审（GPT6）定位问题所在。**文中严格区分「实测事实」「代码可查事实」「推断」三类**，凡推断均已标注。所有数字都附测量方式与产物路径，可复核。

---

## 0. 环境与版本

| 项 | 值 |
|---|---|
| 生产机 | 飞牛 NAS，仓库 `/vol1/docker/StockAnalyzer` |
| API | 宿主机 `localhost:18001` → 容器 8000；`GET /health` |
| 当前镜像 | `7e9e33b`（分支 `fix/asof-breadth-gate-coverage-0917`，基于 `codex/nightly-selection-delivery`） |
| 容器 | api / scheduler-critical / scheduler-heavy，本次部署后 0 重启 |
| 行情库 | `/app/artifacts/vendor_delta/market_delta.duckdb`，`daily_bars` 237 万行，2018-12-24 ～ 2026-09-16 |
| 回测产物 | `/app/artifacts/backtest/asof_scan/`（`latest.json`、`history.jsonl`、`_range_runs/*.json`） |
| 夜扫历史 | `/app/artifacts/runtime/runtime_state_history/week5_scan_history.jsonl` |

---

## 1. P0-A 广度门在回放与生产两条路径上行为相反（**已修，待评审**）

### 现象
2026-09-04 起连续 9 个交易日，历史回放终门 100 个候选被**全部**拒掉，拒因 `data_gate:market_breadth_blocked`，`final_count=0`。

### 实测事实（覆盖率）
在容器内用历史 provider 直接测量（脚本见 §7）：

| 日期 | 全索引 `list_symbols()` | 当日有行情标的数 | coverage_ratio | `coverage_ok` |
|---|---|---|---|---|
| 2026-09-01 | 5833 | 5542 | **0.9501** | true |
| 2026-09-16 | 5833 | 5535 | **0.9489** | false |

门槛 `DEFAULT_MIN_COVERAGE = 0.95`。**两天相差万分之十二，决定整个市场能否开仓。** 缺失的约 5% 是当日停牌/未上市的非交易标的（不是取数故障）。

### 代码可查事实
- `ops/market_breadth.py::build_breadth_snapshot`：`available = scoring.available and coverage_ok`，`coverage_ok = coverage_ratio >= 0.95`。
- `ops/market_breadth.py::compute_market_breadth_from_warehouse`：`coverage_ratio = total_symbols / max(1, len(symbols))`，分母是**全索引**。
- `ops/market_breadth.py::breadth_usage_policy`：`available` 为假 → `{"block_new_buy": True, "reason": "breadth_score_unavailable"}`，**无放行分支**。
- 历史侧：`runtime/services/week5_selection_engine.py::_historical_market_breadth`（约 L1771）现算快照后交 policy；命中 block 时引擎把终门状态置为 `market_breadth_blocked`（约 L1209）。
- live 侧：`runtime/service.py::_apply_market_breadth_gate`（约 L7650）读预写快照，`snapshot is None` → `{"block_new_buy": False, "reason": "breadth_unavailable"}`，**fail-open**。

**同一件事（广度算不出来），两条路径默认值相反。**

### 本轮修复
`7e9e33b`，仅改 `_historical_market_breadth`：`reason == "breadth_score_unavailable"` 且 score > 0 时，退回用同一个 `market_breadth_disable_if_below`(45) 判分数本身——分数仍低照旧禁买，达标才放行并标 `breadth_ok_low_coverage`；score ≤ 0 维持不阻断；meta 新增 `coverage_ratio`。**仅 historical policy 可达，live 路径未动。**

### 修复后实测（9/1–9/16 重跑，before/after 均在 `_range_runs/`）

| 日期 | 广度门 前→后 | 广度分 | final |
|---|---|---|---|
| 9/4、9/7、9/8、9/14 | score_unavailable → **ok_low_coverage** | 47.94 / 58.09 / 62.00 / 53.83 | 0 → 0（被终门分拒） |
| 9/9、9/10、9/11、9/15 | 仍拦 | 41.13 / 30.06 / 26.75 / 29.40 | 0 → 0 |
| **9/16** | → **ok_low_coverage** | 67.92 | **0 → 1（300497）** |

### 仍未修
**生产 `artifacts/runtime/market_breadth.json` 不存在**（`find /app/artifacts -name "*breadth*"` 为空），生产每次都走 fail-open 分支 ⇒ **生产广度门处于静默失效状态**。写方是 `runtime/services/market_sync_service.py::_write_market_breadth`。修它会改变生产行为（部分低广度日将禁买），尚未动。

---

## 2. P0-B 打分排序缺乏预测力（**未解，本轮最重要**）

### 实测事实（2026-09-17）
取修复后回测中每天得分最高的 3 只（11 个有效日 × 3 = 33 笔），按系统自身规则模拟退出（止盈 +8% / 止损 −5% / 持有 10 交易日，含成本与涨跌停、T+1 约束，复用 `backtest/holding_curve.py::analyze_symbol_holding` + `ExecutionMatcher`）：

- **平均净收益 −3.44%，胜率 15%（5/33），23/33 触发止损**（多为 −5% 附近），最好 +6.70%，最差 −6.04%。
- 买入持有口径（入场收盘 → 9/16 收盘）：平均 **−4.51%**。

基准为**全市场等权**（`market_delta.duckdb.daily_bars`，5547 只，同日窗口）：

| 买入日 | 候选平均 | 全市场等权 | 超额 |
|---|---|---|---|
| 9/1 | −3.47% | −3.06% | −0.41 |
| 9/2 | −10.12% | −2.40% | **−7.72** |
| 9/3 | −11.17% | −2.00% | **−9.17** |
| 9/4 | −7.10% | −1.65% | **−5.45** |
| 9/7 | −4.91% | −2.58% | −2.33 |
| 9/8 | −5.88% | −3.20% | −2.68 |
| 9/9 | −4.56% | −2.76% | −1.80 |
| 9/10 | −4.17% | −1.43% | −2.74 |
| 9/11 | −1.96% | +0.58% | −2.54 |
| 9/14 | −0.39% | +0.04% | −0.43 |
| 9/15 | **+4.15%** | +1.42% | **+2.73** |

**11 个窗口中 10 个跑输等权基准。**

### 与历史结论一致
2026-08-31 的 8 月回测（`docs/week5_backtest_report_20260831.md`）已记录"分数-收益 Spearman 4/5 持仓期为负""日内分数第 1 名 N10 平均 −2.89%"。本次 9 月窗口**同向复现**，非新问题。

### 必须同时声明的限制（否则结论会被误用）
1. **这 33 笔不是被买入的票。** 终门把它们全拒了；唯一真正通过的 9/16 300497 尚无后续行情（日线截至 9/16，9/17 数据次日才有）。所以本文只能说"排序无预测力"，**不能说"选出的票亏了"**。
2. 33 笔来自同一约两周窗口、彼此重叠，**不是独立样本，不可外推**。
3. **带前视**：回放用 `trained_at=2026-09-15` 的模型给 9/1–9/14 打分（见 §3）。
4. 恰恰因为如此：**"降低阈值以增加出票"目前得不到任何证据支持——这个窗口里阈值 70 挡掉的是一批负期望交易。**

---

## 3. P0-C 训练时间穿越，回测结论均非干净样本外（**未解**）

### 代码可查事实
- `runtime/services/week5_historical_runner.py::_resolve_model_info` **没有按 `as_of` 选择当时模型**的逻辑，直接取当前 champion / 最新模型。
- `asof_backtest_service.py` 的 caveat `lookahead_bias: True` 是**硬编码**，不是检测结果。
- 本次 9/1–9/16 回放全部使用 `model_trained_at = 2026-09-15T07:32:35`。

### 影响
所有历史回测（含 §2）都不是样本外结论。泄漏通常**高估**表现——在泄漏条件下仍亏损，方向上值得警惕，但强度不能作为定论。

---

## 4. P0-D champion 身份链断裂，线上模型不可知（**未解**）

（来源：2026-08-31 审计 + 本次回放 caveat 复现）
- 模型 registry 全部记录 `artifact_content_hash` 为空、无 champion role、多个 `model_id` 指向同一 `model_v1.json`。
- 运行时经 `auto_load_predictor` 绕过 registry，导致回放/生产载荷里 `historical_context.model.model_id == ""`（本次 9/1–9/16 回放全部为空）。
- 后果：**无法回答"线上正在用哪个模型"**，A/B 对比与回滚都缺少基准身份。

---

## 5. P0-E 政策 / 新闻 / 主题三层设施全部"只观察、不参与"（**未解**）

生产 9/16 22:07 扫描报告的实测内容：

| 设施 | 配置/产物证据 | 实际效果 |
|---|---|---|
| 新闻 / 政策分量 | `evolution.m7_news_records_path = ""`（**空字符串**），`exists=False` ⇒ `news/provider.py::available` 恒 False | 20 个候选中 17 个标 `news_component_unavailable`，**取值全为 0.5（中性）** ⇒ 对排序零贡献 |
| M12 主题层 | `theme.enabled=true`，**`mode=shadow`、`dry_run=true`**；9/16 报告 `active_themes=["geo_oil"]`、`pinned_pool` 10 只 | `boost_symbol_count=0`、`boost_max=0.0` ⇒ **不加分** |
| 市场雷达 | `.env`：`SA__WEEK5__MARKET_RADAR_ENABLED=false` | 关闭 |
| 新闻风险决策 | `evolution.news_risk_mode = shadow` | 影子 |

补充代码事实：
- `pipeline.py::_score_news_component`（约 L2174）：provider 不可用 → 返回 `(0.50, False)`；news 分量在 L743 / L1604 进入打分。
- 生产通过 `runtime/news_provider_factory.py::build_news_provider` 构造 `ArtifactNewsSignalProvider(path=config.evolution.m7_news_records_path, ...)`，路径为空即无法工作。
- **回放侧另有硬性中性化**：`Week5RunPolicy.historical()` 设 `news_neutralized=True`，引擎以 `news_mode_override="off"` 执行终门 —— 这是防泄露设计，但副作用是**用现有回测无法评估政策分析的边际贡献**。

---

## 6. P1 组：阈值与口径错配（**待决**）

1. **终门阈值 vs 分数分布**：`week5.final_signal_min_threshold = 70`；9/1–9/16 十二天候选最高分区间 **63.32 ～ 75.14**。修复广度门后 4 天放行，仍 0 票，拒因为 `below_min_threshold` 100/100 与 `cross_review_failed` 96–100。最接近的是 9/8 的 601086 = **69.97（差 0.03）**。
2. **回测与生产的质量池口径不同**：回测走 `week5.universe_quality_target_size`（默认 **100**），生产夜扫走 `night_quality_target`（**300**）。例：9/1 回测 5211→3678→100，生产 5481→3639→300。**回测选池 ≠ 生产选池**。
3. **回测账户为中性假设**（空仓、无连败、equity=1.0），与生产实际账户状态不同。
4. **同标的分数不一致**：9/16 的 300497 回放 75.14 / 生产 77.49（候选池 100 vs 20/50 所致）。
5. **选池偏动量（结构性观察，非实测因果）**：候选的 `shortlist_components` 以 trend / price_volume / signal 为主；300497 被选中前 5 个交易日已上涨 10.5%。**推断**：这一构成偏向"已经涨过"的标的。

---

## 7. 复现方法

```bash
# 覆盖率实测（容器内）
docker exec -i stock-analyzer-api python - <<'PY'
from datetime import date, datetime
from pathlib import Path
import tempfile, pandas as pd
from stock_analyzer.config import get_config
from stock_analyzer.data.asof_provider import AsOfMarketDataProvider
from stock_analyzer.ops.market_breadth import compute_market_breadth_from_warehouse
from stock_analyzer.runtime.services.week5_historical_runner import build_historical_base_provider
cfg = get_config(); td = Path(tempfile.mkdtemp(dir="/tmp"))
base = build_historical_base_provider(cfg, task_dir=td)
for iso in ("2026-09-01", "2026-09-16"):
    d = date.fromisoformat(iso); p = AsOfMarketDataProvider(base, d)
    now = datetime.combine(d, datetime.min.time()).replace(hour=15, minute=30)
    print(iso, len(p.list_symbols()), compute_market_breadth_from_warehouse(p, now=now)["coverage_ratio"])
PY

# 区间回测（3 段，每段 ≤5 个交易日，因 week5_max_dates_per_run=5）
# POST /backtest/asof-scan {start_date, end_date, algorithm:"week5_daily", horizon_days:10}
# 轮询 GET /tasks/{task_id}；结果落 latest.json（每次覆盖，需自行备份）

# 基准（全市场等权）
docker exec -i stock-analyzer-api python -c "
import duckdb
con=duckdb.connect('/app/artifacts/vendor_delta/market_delta.duckdb', read_only=True)
print(con.execute(\"select count(*) from daily_bars where date=DATE '2026-09-16'\").fetchone())"
```

产物：`_range_runs/r{1,2,3}_*.json`（修复前）、`_range_runs/fix_r{1,2,3}_*.json`（修复后）。

---

## 8. 请评审回答的问题

1. **回测可信度**：应先修"按 as_of 选模型"（消除穿越），还是先做"政策开/关对照"？两者谁阻塞谁？
2. **政策/主题转正的判定标准**：什么证据算"有正边际"？在 shadow 模式下应该收集哪些量、多长时间，才足以支撑转正决策？
3. **§2 的负向结果如何归因**：是模型问题、标签问题、选池问题，还是这 2 周的市场结构（小盘/成长风格回撤）？如何设计一个能区分这几者的实验？
4. **阈值 70**：证据指向"不动"还是"重校准"？若重校准，用什么不变量（分数分位 vs 未来收益单调性？）而不是拍数？
5. **生产广度门失效**：是否应立即修（会改变生产每日出票行为），还是与 §3/§4 一起改？
6. **在排序无预测力未解决前**，是否应继续维持 fail-closed（宁可 0 票）？

---

## 9. 已修清单（避免重复评审）

| 提交 | 内容 |
|---|---|
| `6a9f9ba`（PR #82，已在 main） | 过热闸改喂真 ma5/atr14，去掉占位 fallback——此前任何股价 > 1.15 元都被判过热、无条件否决买入路径。修复效果：同一 9/1 回放 `overextension_reject_new_buy` 98 → 1 |
| `d1d10b7` | 过热闸显式 `insufficient_input` + 买入准入 fail-closed |
| `7e9e33b`（本条，分支待评审） | §1 历史广度门覆盖率边缘修正 + 6 条回归测试 |
| `df2ce92` 等（`codex/nightly-selection-delivery`） | 晚间选股报告冻结/交付/重试/恢复链路（独立验收 R1–R4） |

**未修**：§1 生产快照缺失、§2 排序预测力、§3 训练穿越、§4 champion 身份链、§5 政策/主题未接线、§6 阈值与口径。
