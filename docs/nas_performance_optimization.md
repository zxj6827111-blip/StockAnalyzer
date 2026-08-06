# NAS 性能优化方案

> 状态：`verify_tushare_batch_permission.py` 已落地；批量补数参数（`--batch` /
> `--index-path`）与扫描 Commit 1（profiling/缓存）尚在实现中，文中以
> "待实现" 标注，参数名以最终实现为准。

## 1. 背景与目标

NAS 硬件：Intel G4560（2 核 4 线程）、7.6 GB 内存。每晚任务两个瓶颈：

| 任务 | 现状 | 目标 | 手段 |
| --- | --- | --- | --- |
| Tushare 补数（vendor 日线/复权因子） | 约 4h | 约 1h | `trade_date` 批量拉全市场（1 次调用返回全市场），替代逐股循环 |
| 全市场扫描（week5） | 约 12h | 约 2h | Commit 1 profiling 热点定位 + 重复计算缓存 |

Tushare `pro.daily` / `pro.daily_basic` / `pro.adj_factor` 支持按
`trade_date` 一次性返回全市场，但**该能力依赖账号积分等级**（通常 2000+
分）。因此实施前必须先验证 NAS 上 token 是否具备权限，见第 2 节。

## 2. 前置条件：批量权限验证

### 2.1 运行验证脚本

```bash
# 在 NAS 上，用与补数相同的方式注入 token：
docker run --rm --env-file /vol1/docker/StockAnalyzer/.env \
  stock-analyzer:latest \
  python3 /tools/verify_tushare_batch_permission.py --check-basic
```

或本机直接运行（token 从环境变量读取）：

```bash
python scripts/verify_tushare_batch_permission.py --check-basic
```

token 解析顺序与 `update_vendor_daily_from_tushare.py` 一致（见该脚本
738-740 行）：

1. `--token` 参数；
2. 环境变量 `TUSHARE_TOKEN`；
3. 环境变量 `SA__MARKET_WAREHOUSE__TUSHARE_TOKEN`。

三者都没有时脚本报错退出（exit 2），token 不会出现在输出中。

### 2.2 输出判定

stdout 输出单个 JSON（含 `trade_date` 探测日期、`trade_cal_ok`、
`daily_rows`、`daily_basic_rows`、`adj_factor_rows`、`verdict`、`hint`）：

| verdict | 含义 | 处置 |
| --- | --- | --- |
| `full_market_batch_allowed` | `pro.daily(trade_date)` 返回 ≥ 3000 行（约全市场） | 可启用批量模式（第 3.1 节） |
| `partial_permission` | 返回 0 < 行数 < 3000 | 积分不足或接口受限：检查积分等级，暂缓批量切换 |
| `failed` | 空返回或异常（输出异常类名与信息） | 检查积分等级 / 接口权限；若异常为 `unexpected keyword argument 'trade_date'`，说明容器镜像未装 tushare SDK（HTTP fallback 不支持该参数），需换镜像 |

`--check-basic` 会额外对 `pro.daily_basic` 与 `pro.adj_factor` 各测一次
`trade_date` 批量调用，结果行数分别写入 `daily_basic_rows` /
`adj_factor_rows`（未指定 `--check-basic` 时为 -1）。

**未通过（partial/failed）时的回退**：不改任何部署，保持逐股模式
（补数不传 `--batch`），先提升积分等级或确认接口权限后再验证。

## 3. 改动清单与部署步骤

### 3.1 补数：`update_vendor_daily_from_tushare.py` 批量模式

**待实现**：`--batch` 与 `--index-path` 参数尚未在
`scripts/update_vendor_daily_from_tushare.py` 落地（并行任务实现中），
以下命令为计划形态，参数名以最终实现为准。

现有已落地参数（argparse 已确认）：

```
--vendor-root /data          # vendor 库根目录（NAS 挂载"股票历史数据"）
--daily-dir 全A日K
--factors-dir 复权因子
--end-date YYYY-MM-DD
--symbols-file <file>        # 缺省用 ZIP 里已有股票
--limit N
--dry-run
--checkpoint <path>          # 记录型 checkpoint
--interval-sec 0.6
--max-retries 3
--max-workers 1
--skip-factors
```

计划新增（占位）：

- `--batch`：按 `trade_date` 一次拉全市场（`pro.daily` /
  `pro.daily_basic` / `pro.adj_factor` 各 1 次），写入 vendor 增量；
- `--index-path <json>`：vendor 索引 JSON 路径（由
  `scripts/build_vendor_zip_daily_index.py --root ... --output ...` 生成，
  参考 `docs/vendor_zip_overlay_nas_deployment.md` 第 5 节的
  `vendor_overlay/daily_index.json` 用途）。

部署命令（计划形态）：

```bash
docker run --rm -v /vol1/你的数据目录/股票历史数据:/data:rw \
  -v /vol1/docker/StockAnalyzer/scripts:/tools:ro \
  --env-file /vol1/docker/StockAnalyzer/.env \
  stock-analyzer:latest \
  python3 /tools/update_vendor_daily_from_tushare.py --batch \
    --index-path /vol1/docker/tools/vendor_daily_index.json \
    --vendor-root /data --end-date 2026-08-06 \
    --checkpoint /data/.vendor_update_checkpoint.json
```

先 dry-run 对比（见 4.2），确认符号数与日期范围与逐股模式一致后再正式
执行。`--batch` 不传即旧逐股行为，随时可回退。

### 3.2 扫描：week5-scan-run profiling / 缓存（Commit 1）

**待实现**：Commit 1 尚未落地，以下为实施方向，以实际实现为准。

现状：全市场扫描走 `stock-analyzer week5-scan-run`（CLI 定义见
`src/stock_analyzer/cli.py` 的 `week5-scan-run` 命令），对 watchlist
逐股执行 week5 分析，无缓存，12h 主要耗在重复的数据加载与特征计算。

Commit 1 内容：

1. **profiling 定位热点**：对单只股票跑一次完整 week5 分析，
   cProfile 采样（`python -m cProfile -o /tmp/w5.prof ...`），按
   cumtime 排序找出占比最高的模块；
2. **缓存**：对重复计算（数据读取、特征宽表生成）加进程内/落盘缓存，
   命中缓存的标的跳过重算；
3. 在 2 核 4 线程、7.6 GB 内存约束下以 2h 为验收线。

验收命令（以实际实现为准）：

```bash
stock-analyzer week5-scan-run --no-notify-enabled \
  > /tmp/w5_scan.json 2>/tmp/w5_scan.err
```

### 3.3 NAS 本机 `stock_updater.sh` 修改建议

`stock_updater.sh` 是 NAS 本机脚本（不在本仓库），建议改动两处命令行，
其余逻辑不动：

```bash
# 补数：追加 --batch 与 --index-path（批量参数落地后），去掉逐股循环语义
docker run --rm -v /vol1/你的数据目录/股票历史数据:/data:rw \
  -v /vol1/docker/StockAnalyzer/scripts:/tools:ro \
  --env-file /vol1/docker/StockAnalyzer/.env \
  stock-analyzer:latest \
  python3 /tools/update_vendor_daily_from_tushare.py --batch \
    --index-path /vol1/docker/tools/vendor_daily_index.json \
    --vendor-root /data --checkpoint /data/.vendor_update_checkpoint.json

# 扫描：保持 week5-scan-run 命令，Commit 1 落地后按缓存参数追加开关
stock-analyzer week5-scan-run --no-notify-enabled
```

上线顺序建议：先在 `stock_updater.sh` 里保留逐股命令、另加一行批量
dry-run 对比（4.2），确认一致后整行替换；首晚观察日志与 `--checkpoint`
记录，异常时回退到逐股命令（第 4.1 节）。

## 4. 回退与验证

### 4.1 回退

- 逐股模式完整保留：`--batch` 不传即旧行为，NAS 上只需把
  `stock_updater.sh` 改回原命令行即可，无需删除任何数据。
- 批量模式只写 vendor 增量与受影响的年度 ZIP（原子重建），索引 JSON
  损坏时用 `build_vendor_zip_daily_index.py` 重建，历史 ZIP 不受影响。
- 扫描侧 Commit 1 未上线前不部署；上线后回退 = 去掉缓存开关/回退镜像，
  参考 `docs/nas_full_remediation_runbook.md` 的镜像回滚流程。

### 4.2 验证命令

1. **dry-run 对比**（批量 vs 逐股，同一 `--end-date`）：

   ```bash
   python3 /tools/update_vendor_daily_from_tushare.py --batch \
     --index-path /vol1/docker/tools/vendor_daily_index.json \
     --vendor-root /data --end-date <T> --dry-run \
     > /tmp/batch_dry.json
   python3 /tools/update_vendor_daily_from_tushare.py \
     --vendor-root /data --end-date <T> --dry-run \
     > /tmp/symbol_dry.json
   ```

   对比两边 JSON 的 `symbols_total`、`end_date` 与逐股 `fetched` 计数
   的一致性。

2. **基准测量**（NAS 上）：

   ```bash
   /usr/bin/time -v python3 /tools/update_vendor_daily_from_tushare.py --batch \
     --index-path /vol1/docker/tools/vendor_daily_index.json \
     --vendor-root /data --end-date <T> --dry-run
   ```

   关注 `Elapsed (wall clock) time`（目标 ≤ 1h）与 `Maximum resident set
   size`（7.6 GB 内存约束，留足余量）。

3. **pytest**（仓库内，变更后回归）：

   ```bash
   python -m pytest tests/ -q -k "vendor or tushare"
   ```

   （以仓库实际测试组织为准，覆盖补数脚本与 provider 的既有用例。）

4. **vendor 只读不变式（sha256 probe）**：扫描任务对 vendor 目录必须
   只读。参考 `scripts/p0_run_nas_advisory_probe.py` 的受控 probe 模式
   ——只读执行、不改 runtime 状态、证据归档。在 NAS 上：

   ```bash
   # 扫描前后各生成一次 sha256 清单，比对必须一致：
   find /vol1/你的数据目录/股票历史数据 -type f -print0 \
     | sort -z | xargs -0 sha256sum > /tmp/vendor_sha256.before
   stock-analyzer week5-scan-run --no-notify-enabled
   find /vol1/你的数据目录/股票历史数据 -type f -print0 \
     | sort -z | xargs -0 sha256sum > /tmp/vendor_sha256.after
   diff /tmp/vendor_sha256.before /tmp/vendor_sha256.after
   ```

   - 扫描前后 diff 必须为空（只读不变式）。
   - 补数任务例外：`update_vendor_daily_from_tushare.py` 的 Phase B 会
     原子重建"受影响年份"的 ZIP 与复权因子 ZIP，属预期写入；对未受影响
     年份抽查 sha256 不变即可。补数前先跑一遍上述清单作为基线，补数后
     只允许受影响文件变化。

## 5. 已知限制与后续

- **一期限制**：批量补数先以单进程落地（G4560 2C4T、7.6 GB 内存），
  全市场日线数据量与内存占用需在 dry-run/首晚观察后确认余量。
- **进程池二期**：验证内存占用与限速（tushare 按 token 限速，参考
  `TushareProvider._call_with_retry` 的全局间隔控制）后，再评估进程池
  并行补数。
- **特征宽表缓存二期**：扫描 Commit 1 缓存落地后，再评估特征宽表整体
  缓存（一期只覆盖重复计算点）。
- **硬件升级 i7-7700（4C8T）**：二期可选，扫描耗时可进一步下降；
  在此之前以当前硬件的 2h 扫描线为准。
