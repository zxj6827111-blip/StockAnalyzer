# StockAnalyzer 飞牛 NAS 更新部署手册

本文适用于当前这套运行环境：

- NAS 项目目录：`/vol1/docker/StockAnalyzer`
- NAS 架构：`x86_64`
- 部署方式：`docker compose` + `runtime localvol`
- API 对外端口：`18001`
- 前端访问入口：`http://NAS-IP:18001/ui/`
- 健康检查入口：`http://NAS-IP:18001/health`

## 1. 当前目录用途说明

项目相关文件统一放在：

- `/vol1/docker/StockAnalyzer`

这个目录里应放：

- 项目源码
- `Dockerfile`
- `docker-compose.yml`
- `docker-compose.runtime.yml`
- `docker-compose.runtime.localvol.yml`
- `.env`
- 本地构建后上传到 NAS 的镜像包 `*.tar`

下面这个目录不要当作项目代码目录使用：

- `/vol1/docker/volumes`

它是 Docker 的运行数据卷目录，当前和本项目相关的卷主要有：

- `stock_analyzer_runtime_artifacts`
- `stock_analyzer_runtime_suggestions`
- `stockanalyzer_redis_data`

不要手动把源码或镜像包上传到 `/vol1/docker/volumes/...` 下面。

## 2. 为什么当前不建议在 NAS 本地 build

当前 NAS 到 Docker Hub 的网络存在问题，表现为：

- `docker pull python:3.11-slim` 超时
- `docker pull node:22-slim` 超时

因此当前推荐流程不是“NAS 本地 build”，而是：

1. 在 Windows 本地电脑构建镜像
2. 导出为 `tar`
3. 上传到 NAS
4. 在 NAS 执行 `docker load`
5. 在 NAS 通过 `docker compose up -d --no-build ...` 启动

## 3. 以后每次更新代码的标准流程

### 第一步：在本地更新代码

先把你修改后的最新代码放到本地项目目录：

- `E:\Software Development\StockAnalyzer`

确认本地 Docker Desktop 已启动。

在 Windows PowerShell 执行：

```powershell
cd "E:\Software Development\StockAnalyzer"
docker version
docker info
```

如果这两条命令都正常，再继续下一步。

### 第二步：本地构建 NAS 可用镜像

当前 NAS 是 `x86_64`，所以本地要构建 `linux/amd64` 镜像。

在 Windows PowerShell 执行：

```powershell
cd "E:\Software Development\StockAnalyzer"

docker buildx build --platform linux/amd64 -t stock-analyzer:latest --load .
docker pull --platform linux/amd64 redis:7-alpine

docker save -o stock-analyzer-latest-amd64.tar stock-analyzer:latest
docker save -o redis-7-alpine-amd64.tar redis:7-alpine
```

构建成功后，这两个文件会出现在：

- `E:\Software Development\StockAnalyzer\stock-analyzer-latest-amd64.tar`
- `E:\Software Development\StockAnalyzer\redis-7-alpine-amd64.tar`

可用下面命令检查：

```powershell
dir "E:\Software Development\StockAnalyzer\*.tar"
```

### 第三步：把镜像包上传到 NAS

把下面两个文件上传到：

- `/vol1/docker/StockAnalyzer`

上传的文件为：

- `stock-analyzer-latest-amd64.tar`
- `redis-7-alpine-amd64.tar`

注意：

- 上传到 `/vol1/docker/StockAnalyzer`
- 不要上传到 `/vol1/docker/volumes`

### 第四步：在 NAS 导入镜像

登录 NAS，执行：

```bash
cd /vol1/docker/StockAnalyzer
docker load -i stock-analyzer-latest-amd64.tar
docker load -i redis-7-alpine-amd64.tar
```

如果看到类似输出，说明导入成功：

```text
Loaded image: stock-analyzer:latest
Loaded image: redis:7-alpine
```

### 第五步：确认 `.env` 端口设置

当前建议固定使用 `18001`，避免和 NAS 上其他服务冲突。

在 NAS 执行：

```bash
cd /vol1/docker/StockAnalyzer
grep '^SA_API_HOST_PORT=' .env
```

如果没有输出，执行：

```bash
echo 'SA_API_HOST_PORT=18001' >> .env
```

如果已经有这一行但不是 `18001`，执行：

```bash
sed -i 's/^SA_API_HOST_PORT=.*/SA_API_HOST_PORT=18001/' .env
```

### 第六步：启动或更新容器

在 NAS 执行：

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --no-build redis api scheduler
```

说明：

- 当前使用 `--no-build`
- 原因是 NAS 本地无法稳定拉取基础镜像
- 这一步会直接使用你刚刚 `docker load` 导入的镜像

## 4. 部署后验证

### 查看容器状态

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  ps
```

正常情况下应看到：

- `stock-analyzer-api` 为 `Up`
- `stock-analyzer-redis` 为 `Up`
- `stock-analyzer-scheduler` 为 `Up`

### 查看日志

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  logs --tail=120 api

docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  logs --tail=120 scheduler
```

### 浏览器验证

前端页面：

- `http://192.168.10.26:18001/ui/`

健康检查：

- `http://192.168.10.26:18001/health`

注意：

- 当前前端入口使用 `/ui/`
- 不再使用 `/dashboard`
- 如果 NAS IP 变化，请把上面的 IP 换成新的 NAS IP

## 5. 常见问题处理

### 问题 1：`failed to bind host port ... 0.0.0.0:8001 ... address already in use`

原因：

- 端口 `8001` 被其他服务占用

处理：

在 `.env` 中设置：

```bash
SA_API_HOST_PORT=18001
```

然后重新启动 `api`：

```bash
cd /vol1/docker/StockAnalyzer
docker rm -f stock-analyzer-api 2>/dev/null || true

docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --no-build api
```

### 问题 2：NAS 本地 `docker compose build` 失败

常见现象：

- `python:3.11-slim` 拉取失败
- `node:22-slim` 拉取失败
- `registry-1.docker.io` 超时或解析异常

处理：

- 当前不要继续在 NAS 上 `--build`
- 使用“本地构建 -> 导出 tar -> NAS 导入 -> NAS 无构建启动”的流程

### 问题 3：本地 Windows 执行 Docker 命令时报错

常见现象：

```text
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine
```

原因：

- Docker Desktop 没有启动

处理：

先启动 Docker Desktop，再执行：

```powershell
docker version
docker info
```

确认本地 Docker 正常后再构建。

### 问题 4：`scheduler` 日志中出现大量 `ResourceWarning`

当前观察到的日志主要来自 `mootdx` 备用行情源，现象是：

- 容器仍然是 `Up`
- 系统仍然可访问
- 但日志会出现较多 `ResourceWarning`

这类告警当前不是致命错误，系统仍可继续运行。

如果想减少这类日志，可以在 `.env` 中加入：

```bash
SA__MARKET_DEPTH__BACKUP=disabled
```

然后重启：

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --no-build api scheduler
```

## 6. 日常更新时最短命令版

### Windows 本地

```powershell
cd "E:\Software Development\StockAnalyzer"

docker buildx build --platform linux/amd64 -t stock-analyzer:latest --load .
docker pull --platform linux/amd64 redis:7-alpine

docker save -o stock-analyzer-latest-amd64.tar stock-analyzer:latest
docker save -o redis-7-alpine-amd64.tar redis:7-alpine
```

### NAS

```bash
cd /vol1/docker/StockAnalyzer
docker load -i stock-analyzer-latest-amd64.tar
docker load -i redis-7-alpine-amd64.tar

docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --no-build redis api scheduler
```

### 检查状态

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  ps
```

## 7. 当前这套环境的最终结论

当前这套 NAS 已经验证通过的可用方式是：

- 本地 Windows 构建 `linux/amd64`
- 导出 `tar`
- 上传到 `/vol1/docker/StockAnalyzer`
- NAS 使用 `docker load`
- NAS 使用 `docker compose ... up -d --no-build`
- 前端访问 `http://NAS-IP:18001/ui/`

如果未来 NAS 到 Docker Hub 的网络恢复正常，再考虑切回 NAS 本地 `build`。
