# StockAnalyzer 部署与安装指南（v1.6 最新完整版）

本文档将详细指导您如何在服务器或本地机器上，正式、安全地部署并运行包含 **StockAnalyzer主线交易系统 (v7.0)** 及 **盘后智能进化引擎 (v1.6)** 的核心服务。本程序设计为 7x24 小时无人值守运行，请务必仔细阅读“核心防坑配置”。

---

## 一、 系统要求

*   操作系统：Linux (推荐 Ubuntu 20.04+ / Debian 11+) 或 macOS/Windows 上的 WSL2。
*   环境基础：Docker Engine 20.10+ 及 Docker Compose v2+。（强烈推荐此法）
*   硬件建议：由于盘后（晚间至凌晨）将进行大量的历史数据交叉验证、回测回滚检测与舆情计算，服务器至少应具备 **2核4G** 内存，且拥有不少于 **20GB** 磁盘空间。

---

## 二、 部署前准备：核心环境变量 (.env)

为了防止系统被恶意干预，以及确保能够把紧急信息推送给您，请在项目根目录下创建一个名为 `.env` 的文件。您可以参考自带的 `.env.example`。

**以下是必须修改或关注的核心配置**：

```ini
# 1. [极度关键] 通信与指令安全密钥，用于防伪造的敏感操作（比如人工一键清仓/暂停买入）
SA__COMMAND_CHANNEL__SECRET_KEY=请修改为你自己的复杂密码（例如：MySecr3tToken$2026）

# 2. [推荐] 微信预警推送方式（主/备双通道，防挂机失联）
# 可选方案 A: pushplus公众号个人推送 (获取Token请访问 www.pushplus.plus)
SA__NOTIFICATIONS__PRIMARY=pushplus
SA__NOTIFICATIONS__PUSHPLUS_TOKEN=你的这段Token填在这里
# 可选方案 B: 企微群机器人 (如果不填此项或置空，系统报警只会存日志)
# SA__NOTIFICATIONS__PRIMARY=wecom
# SA__NOTIFICATIONS__WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx

# 3. [防坑关键] 当前进化系统承认的“卫冕冠军”策略版本（不用动，除非你自己确认了新策略上位）
SA__EVOLUTION__ACTIVE_CHAMPION_ID=champion_v7_202603
SA__EVOLUTION__CODE_COMMIT_ID=git:release-20260301-local

# 4. [可选] 底层依赖强检。如果在非定制的云服务环境中，没有 cpulimit 等 Linux 指令，请设为 false。
SA__EVOLUTION__STRICT_DEPENDENCY_CHECK=false
```

---

## 三、 修改 Docker 配置（防数据丢失 & 保证时间准确）

在构建前，请务必修改根目录下的 `docker-compose.yml`，补上**北京时区**和**硬盘持久化目录**，否则一旦断电或更新代码，辛辛苦苦积攒的机器智能记忆全丢！

请将您的 `api` 和 `scheduler` 服务做如下修改（**特别是环境变量 `TZ` 和挂载卷 `volumes`**）：

```yaml
services:
  api:
    build: .
    # ... (原有配置不变)
    environment:
      # （原有环境变量保留即可），下面是必须增加的：
      - TZ=Asia/Shanghai                 # 确保时区正确！否则时间窗守护神(TimeGuard)会严重误判
      - SA__DATA_SOURCE__PRIMARY=efinance # 容器默认优先使用更稳定的模拟盘在线数据源
    volumes:
      - ./artifacts:/app/artifacts           # 持久化运行态、验收报告、冠军模型、合规库等全部工件
      - ./suggestions:/app/suggestions       # 记录 M8/M3 等模块推举的高分种子策略
    # ...
```
同样的，也给 `scheduler` 服务加上 `TZ=Asia/Shanghai` 和 `volumes`。

> **小贴士**：当前仓库内的 `Dockerfile` 已默认安装 `cpulimit`；如果您自定义过镜像，请确认该库仍然存在，否则 `evolution preflight` 会因为严格依赖检查而阻塞。

---

## 四、 一键启动（Docker Compose）

如果您做好了上述两步（配了`.env`及`挂载目录`），那么后续的每一次更新与启动，都只需要这两句魔法口诀：

1.  **构建并拉取环境镜像**（首次时间较长）：
    ```bash
    docker compose build
    ```

2.  **后台启动全家桶**（API接口、闲时调度器、Redis高速缓存）：
    ```bash
    docker compose up -d
    ```

就是这么简单！几秒钟后，您可以：
*   **访问控制台**：打开浏览器访问 `http://服务器IP:8000/health` 查看心跳；
*   **查看系统滚动日志**：
    ```bash
    docker compose logs -f api
    docker compose logs -f scheduler
    ```

---

## 五、 手动调试与故障排查 (避坑指南)

部署完成后，作为系统掌控者，您可以通过附带的命令行工具随时干预系统或验证功能：

### 1. 测试安全风控和消息推送是否跑通
在服务器上（或者进入 Docker 容器内）执行：
```bash
docker compose exec api python -m stock_analyzer.cli sign-command --action SET_EQUITY --payload "{}"
```
*   **现象**：如果配置正确，您的手机微信（PushPlus/企微）会收到一条系统风控层发出的模拟授权通知，证明通讯通畅。

### 2. 演习并监控：闲时进化系统 (v1.6) 是否正常工作
不需要死等到半夜！你可以随时发起一次模拟演习（干跑，Dry-run）：
```bash
docker compose exec scheduler python -m stock_analyzer.cli evolution-drill --now "2026-03-02T20:41:00"
```
*   **现象**：您会在日志中立刻看到 `M9 数据质量检查`、`SCORE FUSION` 打分乃至最终生成一封 `Change Proposal` 提案的完整流水。

### 3. 被封 IP / 无任何买入动作？
如果发现连续几天“毫无波动”，请查阅日间调度日志：
*   **可能是 AKShare 源被公共云封锁了**。进入配置文件检查回退网关或通过代理 IP 绕行。
*   或者是今天触发了 `time_guard.py` 中的硬退避红线。确保您的服务器时钟（`date` 命令）和挂载的容器时区是准确无误的 `CST` (北京时间)。

---

*(最后更新：2026年3月 | 面向 V7.0 及 智能进化系统 V1.6)*
