# Intraday 摘要数据停滞排查报告（2026-08-27）

> 对应 `docs/plan_asof_backtest_holding_curve.md` Task 2。全部结论基于 NAS 生产环境
> 实测（`scripts/nas_exec.py` 只读诊断），非推测。本报告是 Task 2 的最终产出，
> 因为根因链条中既有真实上游断供，也有本项目代码库管辖范围之外的运维环节缺失，
> 无法通过修改本仓库代码单独解决，故按 plan 要求转为书面报告。

## 结论摘要

`vendor_intraday_summary.duckdb` 停在 2026-07-17（构建于 2026-08-21 16:44）不是
`stock_analyzer` 代码库的 bug，而是**两个独立因素叠加**：

1. **数据供给链路存在缺口**：本项目实际读取的历史归档目录
   `/vol1/1000/股票历史数据/沪深分钟数据/`（挂载进容器为 `/data/vendor_history`）
   自 2026-07-25 起再未更新，但**上游每日采集本身并未停摆**——
   `/vol1/1000/股票数据/output/minute_raw/` 下每日分钟数据一直产出到
   **2026-08-26**（最新交易日）。两者是**两个独立目录树**，中间缺少一个把
   每日产出归档合并进历史库的桥接步骤，且该步骤**不存在于本代码库、也不在
   NAS crontab 中**——不属于 `stock_analyzer` 代码能修复的范围。
2. **本仓库的 reuse-on-deploy 机制缺少显式新鲜度下限**：`nas_deploy_update.sh`
   在 `INTRADAY_SUMMARY_REQUIRED_LATEST_DATE` 未配置时，
   `--check-reusable` 不做任何新鲜度校验，导致即使历史归档已经停止更新，
   每次部署也会判定现有 `vendor_intraday_summary.duckdb`「可复用」而跳过重建，
   使问题被静默掩盖而非在部署时暴露。**这一点属于本项目代码库可控范围**。

按 plan 要求的三分类：**根因是（1）上游数据供给问题为主，叠加（2）本项目部署脚本
的可复用性校验缺少新鲜度下限，两者共同导致问题长期未被发现**。

## 排查过程与实测证据

### 1. 现状确认

```
$ ls -la /vol1/docker/StockAnalyzer/intraday_summary/
-rw-r--r-- ... build.log                 (481B, 2026-08-20 13:26)
-rw-r--r-- ... monitor.log               (13B,  2026-08-20 09:52)
-rw-r--r-- root root 314060800 vendor_intraday_summary.duckdb        (2026-08-21 16:44)
-rw-r--r-- root root     14724 vendor_intraday_summary.duckdb.manifest.json (2026-08-21 16:44)
```

`build.log` 完整内容（唯一一条记录，来自某次失败的 `--require-latest-date` 校验）：

```
RuntimeError: intraday summary does not cover required latest date 2026-08-19:
{'1m': '2026-07-17', '5m': '2026-07-17'}
```

### 2. 调度/代码路径排查（对应 plan 步骤 2）

- `data/intraday_summary.py`、`data/intraday_summary_builder.py`、
  `data/intraday_sync.py`、`ops/intraday_freshness.py` 均是**运行时读取/校验**
  逻辑（供 provider 消费已构建好的 DuckDB），**不包含重新构建 DuckDB 的调度任务**。
- `scripts/build_vendor_intraday_summary.py` 自身 docstring 明确：

  > "The builder is intentionally offline-only. Runtime providers consume the
  > resulting DuckDB and never execute this ZIP path in production."

  即该脚本按设计**不是**容器内定时任务，而是部署时/运维手动触发的离线构建工具。
  容器内 `scheduler-critical`/`scheduler-heavy` 的任务注册表（实测 dump）中
  确认**没有**任何 intraday 摘要重建任务，这是符合设计的，不是遗漏。
- 该脚本实际由 `scripts/nas_deploy_update.sh` 在每次部署时调用（
  `--check-reusable` 检查是否可复用，不可复用才重建）。**根因就在这一步**：

  ```bash
  INTRADAY_REQUIRED_LATEST_DATE="${INTRADAY_SUMMARY_REQUIRED_LATEST_DATE:-}"
  if [[ -n "${INTRADAY_REQUIRED_LATEST_DATE}" ]]; then
    INTRADAY_FRESHNESS_ARGS=(--require-latest-date "${INTRADAY_REQUIRED_LATEST_DATE}")
  else
    echo "intraday summary static freshness floor disabled; runtime candidate sync is authoritative"
  fi
  ```

  实测确认 NAS 的 `.env` 与全部 `docker-compose*.yml` 均**未设置**
  `INTRADAY_SUMMARY_REQUIRED_LATEST_DATE`。这意味着 `--check-reusable` 在没有
  新鲜度下限的情况下，只要现有文件存在且校验通过（ZIP 指纹未变化等），就会
  判定「可复用」，**永远不会因为数据陈旧而触发重建**。查看该分支近期 git
  历史（`7c482be fix(nas): reuse valid intraday summary on deploy`，PR #27）
  确认这是一次专门为「避免每次部署都重新构建耗时的 DuckDB」而引入的优化，
  但缺少配套的新鲜度下限默认值或告警，导致数据陈旧被静默掩盖。

### 3. 上游排查（对应 plan 步骤 3、4）

**先入为主的假设被证伪**：最初怀疑「上游 75G 分钟数据源本身断供」，但深入排查后
发现这个假设不完全成立——存在两个独立目录树：

| 目录 | 角色 | 实测最新数据 |
|---|---|---|
| `/vol1/1000/股票数据/output/minute_raw/` | 每日采集器的**每日产出** | **2026-08-26**（正常，每日更新） |
| `/vol1/1000/股票历史数据/沪深分钟数据/` | 本项目实际读取的**历史归档库**（容器 `/data/vendor_history`） | **2026-07-25**（5 个子目录 1m/5m/15m/30m/60m 统一停在同一天，非渐进式衰减） |

证据：

```
$ ls -la /vol1/1000/股票数据/output/minute_raw/ | tail
minute_5m_20260824.zip  2026-08-24 22:38
minute_5m_20260825.zip  2026-08-25 22:45
minute_5m_20260826.zip  2026-08-26 22:37     ← 每日采集正常运行

$ for d in /vol1/1000/股票历史数据/沪深分钟数据/*/; do 最新文件时间; done
Stock_15min_2000-now/  2026-07-25
Stock_1min_2000-now/   2026-07-25
Stock_30min_2000-now/  2026-07-25
Stock_5min_2000-now/   2026-07-25
Stock_60min_2000-now/  2026-07-25    ← 5 个子目录完全同一天停止，像是某次批量归档
                                        操作之后再没运行过，而非逐渐衰减
```

`auto_package_daily.sh`/`package_daily_all.sh`（每日采集器的打包脚本）产出的是
`/vol1/1000/股票数据/output/data_YYYYMMDD.zip` 这个**独立的每日打包文件**，
不写入 `沪深分钟数据/` 历史归档目录。也没有找到任何脚本/crontab 任务负责把
`output/` 每日产出合并进 `历史数据/沪深分钟数据/`——这个「归档桥接」步骤
**不存在于本代码库，也不在当前 NAS crontab 中**。

`build.log` 里 `{'1m': '2026-07-17', '5m': '2026-07-17'}` 比文件 mtime 的
7-25 更早 8 天，说明历史归档目录里 ZIP 文件的**内容覆盖范围**本来就落后于
其**文件修改时间**——即最后一次归档操作本身搬运的也是几天前的数据，
不是当天刚产生就立即归档。

`/vol1/1000/股票历史数据/intraday_summary/`（上游侧摘要目录）确认为空
（0 字节），与本项目自己构建产出的 `/vol1/docker/StockAnalyzer/intraday_summary/`
是两个不同目录，不构成互相依赖。

### 4. 结论定性

- **不是** `stock_analyzer` 代码 bug。
- **不完全是**「上游彻底断供」——每日采集器本身工作正常、数据新鲜。
- **是**「历史归档目录缺少每日归档更新」这一 NAS 侧运维环节的缺口，且该环节
  未落在本项目代码库任何脚本里，无法通过修改本仓库解决。
- **叠加**本项目 `nas_deploy_update.sh` 的可复用性校验缺少新鲜度下限，
  导致这个数据缺口在过去一个多月的多次部署中都未被发现/告警。

## 影响范围

- **生产选股质量**：`feature/engineer.py` 派生的约 40 个 `i1m_*`/`i5m_*`
  intraday 特征列，自 7-17（duckdb 内容覆盖上限）起持续缺失，
  当前生产模型推理时这些列恒为 NaN。
- **Week5 全市场扫描**：`fresh_ratio` 实测 0.0，`week5_service.py` 的
  `freshness_gate_blocked` 判断已正确拦截扫描（实测审计事件
  `week5_scan_blocked_intraday_freshness`，2026-08-27 01:12:33），
  说明现有可观测性机制**运作正常**，问题一直是可见的（能查到审计事件），
  只是此前无人排查到这一层。
- **本次 as-of 回测功能（Task 3/4）**：因此必须走 `duckdb_optional` 降级路径
  才能产出任何结果，且 API 响应的 `caveats.intraday_coverage_until` 字段
  必须准确反映实际覆盖上限（不可硬编码，另见本次返工第 3 项修复）。

## 修复建议（需要用户或运维决策，本次会话未擅自执行）

### 本项目代码库可控范围内（已在本次返工中处理，见对应 commit）

- `nas_deploy_update.sh` 的 `--check-reusable` 默认新鲜度下限缺失问题，
  建议后续单独提交修复：为 `INTRADAY_SUMMARY_REQUIRED_LATEST_DATE` 设置一个
  基于「最近一个交易日」的默认值（而非放任为空跳过校验），
  或至少在跳过校验时打印更显著的告警日志。**本次会话未修改此部署脚本**，
  因为这是独立于 as-of 回测功能的既有生产部署逻辑变更，改动前需要与用户
  确认是否会影响下次部署的行为与耗时（重建一次 DuckDB 预计耗时较长），
  超出本次返工的 5 项既定范围，留作后续 issue。

### 需要用户/运维在 NAS 侧决策（本项目代码无法代为解决）

1. **需要用户确认**：`/vol1/1000/股票数据/output/` 每日采集产出与
   `/vol1/1000/股票历史数据/沪深分钟数据/` 历史归档之间，此前是否存在某个
   人工操作或已下线的脚本负责搬运合并？如果有，需要恢复该操作或改为定时任务；
   如果从未有过自动化，需要新建一个（不属于 `stock_analyzer` 应用代码的职责，
   应该是 NAS 侧独立于本项目仓库的数据运维脚本）。
2. **短期缓解方案**：若无法立即恢复归档桥接，可以让
   `build_vendor_intraday_summary.py` 直接指向
   `/vol1/1000/股票数据/output/minute_raw/`（每日产出目录）作为 `--root`
   而非当前的历史归档目录——但这需要评估该目录的保留策略（是否会被清理、
   保留多少天历史）是否满足 `--keep-days 480` 的构建需求，
   以及目录内文件命名格式（`minute_1m_YYYYMMDD.zip` 按日单文件）
   是否兼容现有 `_build_interval` 的 ZIP 扫描逻辑（该逻辑目前扫描的是
   `Stock_1min_2000-now/` 这种按年/月归档、内含多支股票多个 CSV 条目的大 ZIP，
   与 `minute_raw/` 每日一个 ZIP 的组织方式很可能不同，需要先验证兼容性
   而非直接切换 `--root`）。
3. 恢复归档桥接（或切换数据源）后，建议立即手动执行一次
   `bash scripts/nas_deploy_update.sh --rebuild-intraday-summary` 强制重建，
   并确认 `fresh_ratio >= 0.95` 后再考虑是否需要设置
   `INTRADAY_SUMMARY_REQUIRED_LATEST_DATE` 作为长期新鲜度下限。

## 本次会话的处理范围

- **未修改** `nas_deploy_update.sh`、`build_vendor_intraday_summary.py`
  或任何 intraday 相关生产代码——根因确认为 NAS 侧数据归档环节缺口，
  超出本仓库代码可修复范围，按 plan 要求写本报告说明根因、影响范围与
  需要用户做什么。
- **未在 NAS 上执行任何写操作**（本次 Task 2 排查全程使用
  `scripts/nas_exec.py` 只读诊断命令：`ls`/`find`/`cat`/`grep`/`docker inspect`/
  `docker logs`/`crontab -l`/`git log`，未触发任何 rebuild、未修改任何文件）。
- Task 3/4/5 的 as-of 回测功能已按此结论正确处理：`intraday_coverage_until`
  改为动态读取（见本次返工第 3 项），不再假设固定日期。
