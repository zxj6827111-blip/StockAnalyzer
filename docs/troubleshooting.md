# 故障排查（常见问题 Troubleshooting）

基于真实排查经验的 FAQ。按 HTTP 状态码 / 现象分节。

## 1. 认证失败：401 / 403

**现象**：调用写操作端点（POST）返回 `401 missing_api_token` 或 `403 invalid_api_token`。

**原因与排查**：

- 鉴权逻辑在 `src/stock_analyzer/main.py`：无凭证 → `401 missing_api_token`；
  凭证错误 → `403 invalid_api_token`。
- 未显式设置 `SA__SECURITY__API_AUTH_ENABLED` 时 **fail-closed 强制开启**（默认安全）。
- 支持两种凭证头，二选一即可：
  - `Authorization: Bearer <token>`
  - `X-SA-API-Key: <token>`
- 例外：`POST /command/execute` 走签名指令通道（HMAC + 幂等，不需要 API token）；
  `GET/POST /wecom/callback`、`POST /feishu/callback` 使用企微/飞书平台签名校验。

**修复**：

```powershell
# 本地联调示例
Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/week5/scan/run' `
  -Headers @{ 'Authorization' = "Bearer $env:SA__SECURITY__API_TOKEN" } `
  -ContentType 'application/json' -Body $body
```

1. 确认 `.env` 已设置 `SA__SECURITY__API_TOKEN` 且服务已重启加载。
2. 确认 token 与请求头一致（注意 Bearer 后有一个空格）。
3. 若只是想临时本地验证，可显式设置 `SA__SECURITY__API_AUTH_ENABLED=false`
   （生产环境不建议）。

## 2. 参数冻结：423 Locked

**现象**：盘中调用交易参数变更类端点返回 `423`，detail 为 `params_frozen`。

**原因**：PRD §8.7 参数冻结窗口（默认交易日 `09:15-15:00`，半开区间
`[start, end)`，可配 `param_freeze.freeze_windows`）。冻结期间拒绝：

- 交易参数变更端点（仅 `param_freeze.frozen_paths` 中列出的路径受保护）；
- 交互通道的变更类查询（`param_freeze.frozen_queries`，如 execution_mode 切换）。

**不受冻结影响**：GET 查询、报告生成（如复盘报告）、调度任务、签名指令通道
`/command/execute`。

**排查**：

```bash
# 当前是否处于冻结窗口
python -c "from stock_analyzer.param_freeze import is_params_frozen; from stock_analyzer.config import load_config; import pathlib; print(is_params_frozen(config=load_config(pathlib.Path('config/default.yaml')).param_freeze))"
```

**修复**：收盘后（15:00 之后）重试；或确认调用路径是否需要走
`/command/execute`（冻结通道豁免）。

## 3. 长任务返回 202：如何查询结果

**现象**：`POST /run/pipeline`、`POST /week5/scan/run`、`POST /research/*/report`、
`POST /train/*` 等立即返回 `202 Accepted` 和 `task_id`，没有直接结果。

**原因**：长耗时写端点经 FastAPI `BackgroundTasks` 异步执行
（注册表在 `src/stock_analyzer/ops/background_tasks.py`）。

**排查**：

```powershell
# 轮询任务状态：queued / running / succeeded / failed
Invoke-RestMethod -Uri 'http://localhost:8001/tasks/<task_id>'
# 最近任务列表
Invoke-RestMethod -Uri 'http://localhost:8001/tasks?limit=50'
```

- `404 task_not_found`：task_id 不存在或已过期，检查是否复制完整。
- 失败时响应体带错误信息；日志在容器内用 `docker compose logs -f api` 查看。
- 部分端点（未异步化）仍是同步响应，见 README「API」一节说明。

## 4. artifacts 污染导致测试失败

**现象**：本地反复跑测试时，偶发断言失败；删除 `artifacts/` 后重跑即通过。

**原因**：部分测试/运行会写真实 artifacts（例如
`artifacts/research/phase_d/*_latest.json`、`artifacts/runtime/runtime_state.json`、
`artifacts/test_*.json`），旧文件会残留到下一次运行并影响状态类断言
（历史、最新报告等）。compose 会把宿主 `./artifacts` 挂载进容器，污染会在
容器与宿主间共享。

**修复**：

```powershell
# 清理测试与运行产物后重跑
Remove-Item -Recurse -Force artifacts
pytest
```

CI 或干净环境跑测试前建议先清空 `artifacts/`（保留目录本身）。不要把
`artifacts/` 下的临时文件提交进 git。

## 5. 覆盖率门禁失败

**现象**：`make quality-evolution` 或带 `--cov-fail-under` 的 pytest 报
`Coverage failure: total of X% is less than Y%`。

**原因**：质量门禁对指定模块要求最低覆盖率（例如 evolution 相关模块 80%，
见 Makefile `quality-evolution`；`pyproject.toml` 未全局配置 cov-fail-under，
各门禁按模块配置）。

**修复**：

1. 先跑目标模块的覆盖报告定位缺口：
   ```powershell
   pytest tests -k evolution --cov=src/stock_analyzer/evolution --cov-report=term-missing
   ```
2. 为新增分支/函数补测试（尤其异常分支、None 分支）。
3. 确认测试收集正常（`pytest --collect-only`）后再跑门禁。

## 6. 数据源降级告警

**现象**：收到数据源降级告警，或 `GET /health/deep` / `GET /risk/status` 显示
primary 数据源不可用、已切 backup。

**原因**：数据源抽象带降级开关与健康监控：

- primary 连续失败达到 `SA__DATA_SOURCE__SWITCH_AFTER_FAILURES`（默认 3）后
  自动切换到 backup（如 tushare → akshare，market_warehouse → vendor_zip_overlay）。
- 健康监控按成功率/延迟判定劣化，触发风控降级（stop-new-buy 等）。

**排查**：

```powershell
# 数据源/风控状态
Invoke-RestMethod -Uri 'http://localhost:8001/risk/status'
Invoke-RestMethod -Uri 'http://localhost:8001/health/deep'

# 查看供应商侧错误分类（限流/超时/参数错误等）
python -m stock_analyzer.cli audit-events --limit 200 --event-type data_source_degrade
```

**修复**：

1. 检查对应供应商的 token/额度（如 `SA__MARKET_WAREHOUSE__TUSHARE_TOKEN`）。
2. 检查网络与代理；TDX 离线包路径 `SA__TDX_SYNC__VIPDOC_ROOT` 是否存在。
3. 临时改 primary/backup 顺序或恢复开关，见 `.env.example` 中 `SA__DATA_SOURCE__*`。
4. 若为限流导致，等窗口期后重试；持续失败按告警模板里的 payload 追 trace。

## 7. 调度任务没跑 / 首次启动无后台任务

**现象**：`docker compose ... up -d api` 后调度任务（08:30 预扫描、15:30 对账等）
没有执行。

**原因**：firstscan 覆盖（`docker-compose.firstscan.yml`）会把 scheduler 放到
独立 profile 后面，首次启动不会自动拉起后台任务，这是设计行为。

**修复**：

```powershell
docker compose -f docker-compose.yml -f docker-compose.firstscan.yml --profile scheduler up -d scheduler
```

之后可用 `python -m stock_analyzer.cli scheduler-run-due --now "..."` 手动触发验证。

## 8. 端口冲突 / 页面打不开

**现象**：容器起来后 `localhost:8001` 无响应，或 8000 被占用。

**排查**：

- compose 对外端口由 `SA_API_HOST_PORT` 决定（默认 `8001`），容器内固定 `8000`。
- 本地裸跑 `uvicorn` 默认 `8000`，与容器共存时改端口。

**修复**：`SA_API_HOST_PORT=18001`（NAS 推荐示例）后重建容器；
`docker compose ps` 确认端口映射。

## 附录：快速定位路径

| 问题域 | 入口 |
|---|---|
| 认证 | `src/stock_analyzer/main.py`、`src/stock_analyzer/api/deps.py` |
| 参数冻结 | `src/stock_analyzer/param_freeze.py`、`config/default.yaml` 的 `param_freeze` |
| 后台任务 | `src/stock_analyzer/ops/background_tasks.py`、`GET /tasks/{id}` |
| 数据源降级 | `SA__DATA_SOURCE__*` 配置、`/risk/status`、`/health/deep` |
| 调度 | `src/stock_analyzer/runtime/service.py` 的 `_register_*`、`scheduler-run-due` |
| 复盘报告 | `src/stock_analyzer/research/monthly_review_report.py`、`daily_review_report.py` |

首次使用请先读 [docs/quickstart.md](quickstart.md)。
