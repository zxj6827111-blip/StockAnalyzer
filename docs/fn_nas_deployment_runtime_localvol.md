# 飞牛 NAS 部署指南（当前版 / runtime localvol）

适用范围：

- 当前仓库已经切到 `market_warehouse + runtime localvol` 运行方式
- 需要把现有本地运行态迁移到飞牛 NAS
- 需要保留 `artifacts/`、`suggestions/`、运行状态、训练产物、市场主库

本指南优先级高于旧文档：

- 根目录 `Deploy_Guide_v1.6.md`：偏通用 Docker 部署
- 根目录 `NAS_迁移与首轮上线清单_v1.0.md`：偏旧版 bind mount 路线

当前推荐部署组合：

- `docker-compose.yml`
- `docker-compose.runtime.yml`
- `docker-compose.runtime.localvol.yml`

其中：

- `docker-compose.runtime.localvol.yml` 负责把运行态切到 Docker 命名卷
- `docker-compose.yml` 已支持通过 `SA_API_HOST_PORT` 调整 API 暴露端口
- 飞牛 NAS 建议把 `SA_API_HOST_PORT` 设为 `18001`，避免和系统管理端口冲突

## 1. 当前架构要点

当前正式运行态依赖以下数据：

- 市场主库：`artifacts/warehouse/market.duckdb`
- 运行数据包：`artifacts/warehouse/package/`
- 运行状态：`artifacts/runtime/`
- 训练与演化产物：`artifacts/training/`、`artifacts/evolution/`
- 策略建议：`suggestions/`

当前正式容器建议使用两个命名卷保存运行态：

- `stock_analyzer_runtime_artifacts`
- `stock_analyzer_runtime_suggestions`

这样做的好处：

- 升级代码时不依赖项目目录 bind mount
- 运行态和镜像解耦
- 后续重建容器更稳

## 2. 飞牛 NAS 部署前准备

建议 NAS 满足：

- 已安装 Docker / 容器服务
- 能通过 SSH 进入 shell
- 能执行 `docker compose`
- NAS 时区设置为 `Asia/Shanghai`

建议项目目录：

- `/vol1/docker/StockAnalyzer`

建议准备一个 TDX 占位目录：

- `/vol1/docker/StockAnalyzer/tdx_empty`

如果 NAS 上没有真实的 `vipdoc`，也必须提供一个存在的目录给 `TDX_VIPDOC_HOST_ROOT`，否则 compose 挂载会失败。

## 3. 先从本机导出最新运行态

如果你本机当前运行的是 local volume 版，而不是直接用项目目录里的 `artifacts/` / `suggestions/`，先在本机导出一次：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/export_runtime_from_local_volumes.ps1
```

导出完成后，项目目录里的以下内容会被刷新为最新状态：

- `artifacts/`
- `suggestions/`

如果你确认本机项目目录里的 `artifacts/` 和 `suggestions/` 本来就是最新的，这一步可以跳过。

## 4. 复制到 NAS

推荐直接复制整个项目目录到 NAS，而不是只复制最小子集。

至少应复制：

- `artifacts/`
- `suggestions/`
- `config/`
- `src/`
- `scripts/`
- `frontend/`
- `.env`
- `.env.example`
- `docker-compose.yml`
- `docker-compose.runtime.yml`
- `docker-compose.runtime.localvol.yml`
- `Dockerfile`
- `pyproject.toml`
- `README.md`
- `docs/`

如果启用了 AI，还应复制：

- `config/llm.local.yaml`

## 5. NAS 上的 `.env` 最少要改什么

如果 NAS 上还没有 `.env`，先从示例复制：

```bash
cp .env.example .env
```

至少确认这些变量：

```ini
SA__COMMAND_CHANNEL__SECRET_KEY=请改成你自己的强密钥
SA__NOTIFICATIONS__PRIMARY=wecom
SA__NOTIFICATIONS__BACKUP=console
SA__NOTIFICATIONS__WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
TDX_VIPDOC_HOST_ROOT=/vol1/docker/StockAnalyzer/tdx_empty
SA_API_HOST_PORT=18001
```

说明：

- `SA_API_HOST_PORT=18001` 是飞牛 NAS 推荐端口
- 如果 NAS 上有真实的 TDX `vipdoc`，把 `TDX_VIPDOC_HOST_ROOT` 改成真实路径
- 如果没有真实 `vipdoc`，就保持指向占位目录

## 6. 首次部署前创建占位目录

在 NAS shell 中执行：

```bash
cd /vol1/docker/StockAnalyzer
mkdir -p tdx_empty
mkdir -p artifacts suggestions
```

## 7. 首次部署推荐流程

### 第 1 步：创建运行卷

```bash
docker volume create stock_analyzer_runtime_artifacts
docker volume create stock_analyzer_runtime_suggestions
```

### 第 2 步：把项目目录里的数据灌入运行卷

```bash
docker run --rm \
  -v "$PWD/artifacts:/source:ro" \
  -v stock_analyzer_runtime_artifacts:/target \
  alpine sh -lc 'mkdir -p /target && cd /source && tar cf - . | tar xf - -C /target'

docker run --rm \
  -v "$PWD/suggestions:/source:ro" \
  -v stock_analyzer_runtime_suggestions:/target \
  alpine sh -lc 'mkdir -p /target && cd /source && tar cf - . | tar xf - -C /target'
```

### 第 3 步：构建并启动

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --build api scheduler
```

说明：

- `api`、`scheduler` 启动时会自动带起 `redis`
- 后续代码更新时，也使用同一组 compose 文件

## 8. 启动后立即验证

### 容器状态

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  ps
```

### 查看最近日志

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  logs api --tail=120

docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  logs scheduler --tail=120
```

### 检查 HTTP 接口

```bash
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/health"
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/runtime/stage"
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/acceptance/week4/latest"
```

如果 NAS 没有 `curl`，直接用浏览器打开：

- `http://NAS-IP:18001/health`
- `http://NAS-IP:18001/dashboard`
- `http://NAS-IP:18001/runtime/stage`

## 9. 上线后你应该重点看什么

至少确认：

- `/health` 正常返回
- `/dashboard` 能打开
- `/runtime/stage` 能看到阶段滚动
- `/acceptance/week4/latest` 能正常返回
- 企业微信通知正常
- Redis 正常工作，盘中缓存可以复用

## 10. 后续更新命令

以后更新代码后，进入项目目录执行：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --build api scheduler
```

如果只是重启，不重新构建：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d api scheduler
```

## 11. 如果 NAS 构建失败

如果 NAS 因镜像源、网络或 Docker Hub 鉴权问题导致构建失败，可以走“本机构建、NAS 导入”：

### 本机导出镜像

```powershell
docker save stock-analyzer:latest -o stock-analyzer-latest.tar
```

### 把镜像包复制到 NAS

例如复制到：

- `/vol1/docker/StockAnalyzer/stock-analyzer-latest.tar`

### NAS 导入镜像

```bash
docker load -i stock-analyzer-latest.tar
```

### 用现有镜像启动，不重新构建

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d api scheduler
```

## 12. 以后需要我远程分析时怎么配合

先在 NAS 上导出支持包：

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

- `artifacts/support/nas_support_bundle.json`

如果需要把支持包拷到当前目录：

```bash
docker cp stock-analyzer-api:/app/artifacts/support/nas_support_bundle.json ./nas_support_bundle.json
```

如果需要直接测试通知链路：

```bash
curl -X POST "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/notify/test" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"NAS通知测试\",\"content\":\"检查自动通知链路\"}"
```

不要优先在 NAS 主机直接执行 `python3 scripts/export_support_bundle.py`，主机通常没有项目依赖。

把这个 JSON 发给我，我就能先按 NAS 的真实运行态判断是代码、配置还是部署环境的问题。
