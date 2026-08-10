# Vendor ZIP → delta 基线导入与每日增量同步

## 背景

Week5 universe 质量选择（5472 符号 × 240 天）此前每次 batch 都从 27 个年度 ZIP
逐符号解压 + 逐符号遍历 qfq 因子 ZIP 的 namelist（79314 条目），全市场一次约
60 分钟。本方案把该成本收敛为：

- **一次性全量导入**：ZIP 历史 → delta DuckDB（`daily_bars` 表）基线；
- **每日增量同步**：updater 更新 ZIP 后，只把新增日期写入 delta；
- **batch 走 delta**：`fetch_universe_quality_metrics` 优先读 delta，仅对 delta
  覆盖不足（缺失/深度不足/整体滞后）的符号回读 ZIP，合并语义与改造前完全一致
  （delta 行赢、财务 PIT join 保留）。

本地性能实测（1000 符号外推 5472）：ZIP 全量 batch ~2.2 分钟（raw，SSD，qfq
因子遍历另需 ~25 分钟）→ delta batch **~6 秒**；探针 `--max-elapsed-ms 300000`
目标余量 50 倍以上。

## 前置条件

- 容器内路径约定（vendor-overlay 部署）：
  - ZIP 根：`/data/vendor_history`（只读卷）
  - 索引：`/app/artifacts/vendor_overlay/daily_index.json`
  - delta 库：`/app/artifacts/vendor_delta/market_delta.duckdb`（runtime 卷）
- 先确认索引已构建（`scripts/build_vendor_zip_daily_index.py`）。

## 1. 全量导入（一次性，NAS 首次执行）

```bash
docker exec <api容器> python scripts/import_vendor_zip_to_delta.py \
    --data-root /data/vendor_history \
    --index-path /app/artifacts/vendor_overlay/daily_index.json \
    --delta-db-path /app/artifacts/vendor_delta/market_delta.duckdb \
    --price-series-mode qfq \
    --limit-days 400
```

- 每符号读取最近 400 天（Week5 lookback 240 + 余量），不是 27 年全历史；
- 幂等：delta 已存在的日期不重复写入、不被覆盖（delta 行赢，与 overlay 合并
  规则一致）；中断后重跑只补缺口；
- qfq 缺因子符号（如新股 301707 等）跳过并统计，不失败；
- 全市场预计 10~30 分钟（NAS 机械盘），只跑一次。

子集 / 预演：

```bash
python scripts/import_vendor_zip_to_delta.py ... --symbols 600000,000001 --dry-run
```

## 2. 每日增量同步

### 方式 A（推荐）：updater 钩子

`update_vendor_daily_from_tushare.py` 新增 `--sync-vendor-delta` 参数：每日
ZIP 更新 + 索引刷新完成后自动执行增量导入。

```bash
python scripts/update_vendor_daily_from_tushare.py --vendor-root /data \
    --end-date $(date +%F) \
    --index-path /vol1/docker/tools/daily_index.json \
    --sync-vendor-delta /app/artifacts/vendor_delta/market_delta.duckdb
```

`stock_updater.sh` 只需在现有 python 调用行追加该参数即可接入。结果里
`delta_sync.updated` 为同步结果。

### 方式 B：独立增量命令

```bash
python scripts/import_vendor_zip_to_delta.py \
    --data-root /data/vendor_history \
    --index-path /app/artifacts/vendor_overlay/daily_index.json \
    --delta-db-path /app/artifacts/vendor_delta/market_delta.duckdb \
    --incremental
```

增量逻辑：

- 每符号对比 delta 最新日期与 ZIP 索引 `latest_date`，只写新增日期；
- delta 缺失的符号（新股等）自动按全量补齐；
- **qfq 因子漂移检测**：除权导致因子重标定时，历史 qfq 价格整体变化；通过
  delta 锚点日期的因子值（≠1.0 即漂移）检出，该符号整段重算并覆盖；
- 每日预计 1~5 分钟（NAS）。

## 3. 验收

```bash
python scripts/probe_universe_quality_selector.py \
    --config config/default.yaml \
    --max-elapsed-ms 300000
```

`ok=true` 即通过（探针保持 read_only，不写 delta）。

## 4. 回滚 / 重跑

- 全量导入可随时重跑（幂等）；`--overwrite-existing` 可强制覆盖（一般不需要）；
- 删除 delta 库文件即回到旧的 ZIP 全量路径（batch 自动回退，仅变慢不失正确）；
- 增量滞后：delta 相对 ZIP 索引滞后超过
  `data_source.vendor_zip_delta_max_staleness_days`（默认 3 天）时，batch 整批
  回退 ZIP 全量，保证数据不过期。
