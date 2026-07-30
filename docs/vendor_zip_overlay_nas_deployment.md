# NAS 外挂 ZIP 历史数据部署指南

本方案用于直接复用 NAS 上已有的股票历史 ZIP，不复制、不解压，也不把约 84GB 历史数据放进项目 `package/`。

运行时数据结构是：

```text
NAS 年度 ZIP 历史数据（只读）
        +
Tushare 增量 DuckDB（可写）
        ↓
按股票和交易日合并；同一天以 DuckDB 增量为准
```

## 1. 已支持的数据目录

下面这种目录可以原样保留：

```text
股票历史数据/
├── 全A日K/
│   ├── 2000.zip
│   ├── 2000(1).zip
│   ├── ...
│   ├── 2026.zip
│   └── 2026(1).zip
├── 沪深分钟数据/
│   ├── Stock_1min_2000-now/
│   ├── Stock_5min_2000-now/
│   ├── Stock_15min_2000-now/
│   ├── Stock_30min_2000-now/
│   └── Stock_60min_2000-now/
├── 京市分钟数据/
│   ├── StockJ_1min_2005-now/
│   ├── StockJ_5min_2005-now/
│   ├── StockJ_15min_2005-now/
│   ├── StockJ_30min_2005-now/
│   └── StockJ_60min_2005-now/
└── 复权因子/
```

当前支持范围：

- `全A日K/YYYY.zip`：支持，ZIP 内 CSV 按需读取。
- `YYYY(1).zip`：自动视为重复副本并忽略，优先使用标准 `YYYY.zip`。
- 沪深、北交所 `1min` 和 `5min`：支持，按需读取并生成日内摘要。
- `15min`、`30min`、`60min`：文件可以保留，但当前运行逻辑不读取。
- `复权因子`：暂不使用。外挂模式固定采用 `raw` 价格，避免把不可信复权因子混入 Tushare 增量。

单位转换已经按这套本地数据格式配置：

- 日线 `volume × 100`。
- 日线 `amount × 1000`，转换成元。
- 分钟 `volume × 100`。
- 分钟 `amount × 1`，原值已经是元。

## 2. 存储占用

外挂模式不会建立完整历史 CSV/Parquet package：

- 原始约 84GB ZIP：保留在 NAS 原位置，只读挂载，空间不翻倍。
- ZIP 索引：本机真实数据生成结果为 `11,863,961` 字节，约 11.3 MiB。
- Tushare 增量：写入 `market_delta.duckdb`，只随新增交易日和扩展字段增长。
- 元数据：每只股票少量 JSON，用于价格契约、同步检查点等。
- `package/bars/*.csv` 和分钟 package 导出：外挂模式已禁用。

现有 `YYYY(1).zip` 本身已经占用重复空间，但系统只忽略它们，不会自动删除。

## 3. NAS 路径和 `.env`

以下命令假设项目目录是：

```text
/vol1/docker/StockAnalyzer
```

NAS 历史数据可以放在任意共享目录。只需要把实际绝对路径写入项目 `.env`：

```ini
SA_VENDOR_HISTORY_HOST_ROOT=/vol1/你的数据目录/股票历史数据
SA__MARKET_WAREHOUSE__TUSHARE_TOKEN=你的_Tushare_Token
SA_API_HOST_PORT=18001
```

注意：

- `SA_VENDOR_HISTORY_HOST_ROOT` 必须直接指向包含 `全A日K`、`沪深分钟数据`、`京市分钟数据` 的根目录。
- 不要把 Token 提交到 Git；真实值只保留在 NAS 的 `.env`。
- 已有 NAS `.env` 时只合并以上新变量，不要用 `.env.example` 覆盖现有密钥。

先在 NAS 上检查目录：

```bash
cd /vol1/docker/StockAnalyzer
VENDOR_ROOT='/vol1/你的数据目录/股票历史数据'

test -d "$VENDOR_ROOT/全A日K"
ls -lh "$VENDOR_ROOT/全A日K/2026.zip"
```

Compose 启动时会自动读取项目 `.env`，不需要在 shell 中 `source .env`。

## 4. Compose 组合

当前 NAS 推荐继续使用 runtime local volume，并叠加外挂数据源：

```bash
sa_compose() {
  docker compose \
    -f docker-compose.yml \
    -f docker-compose.runtime.yml \
    -f docker-compose.runtime.localvol.yml \
    -f docker-compose.advisory.yml \
    -f docker-compose.vendor-overlay.yml \
    "$@"
}
```

该组合会强制：

- `advisory_only=true`。
- `training.enabled=false`。
- `auto_promotion.enabled=false`。
- 历史数据挂载到 `/data/vendor_history:ro`。
- 增量库写入 runtime artifacts 卷中的 `vendor_delta/market_delta.duckdb`。
- 价格契约为 `raw + explicit_cashflow`。

创建或确认运行卷：

```bash
docker volume create stock_analyzer_runtime_artifacts
docker volume create stock_analyzer_runtime_suggestions
```

先检查最终 Compose 配置：

```bash
sa_compose config | \
  grep -E 'vendor_zip_overlay|vendor_history|market_delta|ADVISORY_ONLY'
```

## 5. 构建镜像和生成 ZIP 索引

先构建镜像，但暂不启动 scheduler：

```bash
export STOCK_ANALYZER_BUILD_COMMIT="$(git rev-parse HEAD)"
export STOCK_ANALYZER_BUILD_SHORT_COMMIT="$(git rev-parse --short HEAD)"
export STOCK_ANALYZER_BUILD_DIRTY="$(test -z "$(git status --porcelain)" && echo false || echo true)"
export STOCK_ANALYZER_BUILD_TIME_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

sa_compose build api scheduler
```

索引过程只读取 ZIP 中央目录和每只股票的最后交易日，不解压归档：

```bash
sa_compose run --rm --no-deps api \
  python scripts/build_vendor_zip_daily_index.py \
  --root /data/vendor_history \
  --output /app/artifacts/vendor_overlay/daily_index.json
```

查看索引摘要：

```bash
sa_compose run --rm --no-deps api \
  python -c "import json,pathlib; p=pathlib.Path('/app/artifacts/vendor_overlay/daily_index.json'); d=json.loads(p.read_text(encoding='utf-8')); dates=[v.get('latest_date','') for v in d['symbols'].values() if isinstance(v,dict)]; print({'symbols_total':d['symbols_total'],'archives_total':d['archives_total'],'ignored_duplicates':len(d['ignored_duplicate_archives']),'latest_date':max(dates)})"
```

如果以后 NAS 历史 ZIP 被替换、增加新年度 ZIP 或新增股票，需要重新执行索引命令，然后重启 `api` 和 `scheduler`。正常的每日 Tushare 更新不需要重建 ZIP 索引。

## 6. 首次小范围增量验证

第一次不要直接启动夜间全市场任务。先用 2 只股票验证 Tushare Token、DuckDB 写入和价格契约：

```bash
sa_compose run --rm --no-deps api \
  /app/scripts/docker-entrypoint.sh \
  python -m stock_analyzer.cli warehouse-sync-run \
  --symbols 600000,000001 \
  --no-force \
  --no-notify-enabled \
  --source-trace-id vendor-overlay-smoke
```

检查状态：

```bash
sa_compose run --rm --no-deps api \
  /app/scripts/docker-entrypoint.sh \
  python -m stock_analyzer.cli warehouse-sync-status

sa_compose run --rm --no-deps api \
  sh -lc 'du -sh /app/artifacts/vendor_overlay /app/artifacts/vendor_delta 2>/dev/null || true; find /app/artifacts/vendor_delta -maxdepth 4 -type d -name bars -print'
```

预期：

- `storage_mode=vendor_zip_readonly_plus_duckdb_delta`。
- `vendor_zip_overlay.enabled=true`。
- `delta_package_writes_enabled=false`。
- `market_delta.duckdb` 已创建。
- 最后一条 `find` 不应输出 `package/bars` 目录。

## 7. 启动 API 和夜间更新

小范围验证通过后启动服务：

```bash
sa_compose up -d redis api scheduler
sa_compose ps
sa_compose logs api --tail=120
sa_compose logs scheduler --tail=120
```

检查接口：

```bash
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/health"
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/warehouse/sync/status"
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/warehouse/sync/latest"
```

默认 scheduler 会在每天 `21:45` 运行全市场增量更新。首次运行会从 ZIP 索引记录的每只股票最新日期开始，并加 5 天重叠缓冲，不会回退到默认的 750 天全历史下载。

本机这套历史数据截至 `2026-07-17`。在 `2026-07-30` 部署时，首次全市场任务需要补齐约 13 个日历日，实际耗时仍取决于 Tushare 权限、配额、网络和扩展数据接口。建议先完成上面的 2 只股票验证，再让 scheduler 在夜间执行全市场任务。

## 8. 查询合并规则

查询某只股票时：

1. 从年度 ZIP 读取历史日线或分钟数据。
2. 从 DuckDB 读取 Tushare 增量。
3. 按交易时间合并和排序。
4. 相同交易日存在两份记录时，DuckDB 增量覆盖 ZIP 历史。

因此 NAS 原始 ZIP 始终保持只读；Tushare 不会改写 ZIP，也不会把完整历史重新复制到 DuckDB。

## 9. 本机真实数据验证记录

`2026-07-30` 已对本机目录执行真实只读验证：

```text
E:\AStockData\raw\local_vendor\original_files\incoming\股票历史数据
```

结果：

- 标准年度 ZIP：27 个，`2000.zip` 至 `2026.zip`。
- 重复 ZIP：27 个，全部识别为 `YYYY(1).zip` 并忽略。
- 股票数：5796。
- 索引最新交易日：`2026-07-17`，其中 5522 只股票到该日期。
- 日线抽样读取通过：`000001`、`300750`、`600000`、`688981`、`920002`。
- 1 分钟抽样通过：`600000`、`920002`。
- 5 分钟抽样通过：`600000`。
- Compose 合并检查通过：runtime artifacts 使用命名卷，历史根目录为只读 bind mount。

这些是本机代码和真实数据格式证据，不等于 NAS 已经部署完成。NAS 上仍需按本指南验证实际挂载、Tushare Token、首次增量和运行日志。

## 10. 回滚

外挂模式出现问题时，不要删除原始 ZIP 或 runtime artifacts 卷。先停止当前组合：

```bash
sa_compose down
```

然后使用原来的 Compose 组合启动，即不叠加 `docker-compose.vendor-overlay.yml`。这样会恢复原 `market_warehouse` 数据源；外挂 ZIP 和 `vendor_delta` 数据仍保留，可继续排查或重新生成索引。
