# NAS Support Bundle

## 作用

部署到 NAS 之后，后续如果需要定位问题、评估是否要改代码、或者准备升级，可以先导出一份支持包：

- 运行态接口快照：`/health`、`/runtime/stage`、`/acceptance/week4/latest`、`/portfolio/reconcile/latest`
- 当前运行卷里的 `runtime_state.json`
- 关键部署文件摘要：`.env`、`config/default.yaml`、`docker-compose*.yml`
- 容器状态、挂载、已脱敏环境变量
- API / scheduler 最近日志
- Redis `runtime_realtime:*` key 情况

以后你只需要把导出的 JSON 给我，我就可以先在本地按同样口径判断问题是在代码、配置，还是 NAS 运行环境。

## 导出命令

在项目根目录执行：

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  exec api \
  python /app/scripts/export_support_bundle.py \
  --base-url "http://127.0.0.1:8000" \
  --log-tail 200
```

默认输出：

```text
artifacts/support/nas_support_bundle.json
```

如果 NAS 暴露的 API 不是本机 `8001`，可以指定：

```bash
docker cp stock-analyzer-api:/app/artifacts/support/nas_support_bundle.json ./nas_support_bundle.json
```

## 建议使用时机

- 白天发现页面阶段异常、调度不滚动、告警异常
- 收盘后准备让我分析盘后任务有没有按预期跑完
- 升级前做一次基线留档
- 升级后做一次新版本验收对比

## 升级协作方式

1. 在 NAS 上执行 `docker compose ... exec api python /app/scripts/export_support_bundle.py`
2. 把 `artifacts/support/nas_support_bundle.json` 发给我
3. 我在本地仓库按这个支持包还原问题口径并修改代码
4. 本地验证通过后，再给你明确的更新步骤
5. 你把新版本重新部署到 NAS，再导出一份新支持包做回归验收

如果你要单独验证通知链路，可以执行：

```bash
curl -X POST "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/notify/test" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"NAS通知测试\",\"content\":\"检查自动通知链路\"}"
```

不要优先在 NAS 主机直接执行 `python3 scripts/export_support_bundle.py`，主机通常没有项目依赖。

在 API 容器内执行的标准命令如下：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  exec api \
  python /app/scripts/export_support_bundle.py \
  --base-url "http://127.0.0.1:8000" \
  --log-tail 200
```

## 注意

- 支持包会保留关键运行参数，但会自动脱敏 `token`、`secret`、`password`、`webhook`、`chat_id` 一类字段
- 如果 Docker 不可用，脚本仍会导出 HTTP 与本地文件快照，但容器和 Redis 细节会缺失
- 如果你后面改成反向代理或不同端口，只需要在命令里改 `--base-url`
