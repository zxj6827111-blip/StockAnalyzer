# 晚间选股与飞书交付联合诊断（2026-09-16）

## 结论与范围

检查时间：2026-09-16 18:56—19:00（Asia/Shanghai）。本轮检查本地源码、NAS 容器、运行配置、调度产物、通知审计，并在当前容器执行无网络发送的最小复现。未修改业务代码、生产配置，未重启服务、触发完整夜扫或发送消息。

目标：交易日晚间完成选股，将当晚结果通过飞书交付；无合格候选时也交付原因。此目标不要求自动交易、训练新模型或自动晋升模型。

判定：基础运行 GO；“晚间选股结果稳定交付到飞书” NO-GO。最直接的阻断是夜扫路径主动禁止所有通知，没有独立的隔夜观察报告通知。

## 已核实的运行证据

| 检查 | 实测结果 |
| --- | --- |
| 本地与 NAS 仓库 HEAD | be6453c5b2d00569a2de17d8e5d0fcc452176d63，PR #82 |
| API / critical / heavy 镜像 | 相同 sha256:ce13eafe214c3fcc781898fab49c41a70f29a64adc1a791d34b304ba0b5802e9 |
| API build | be6453c，trusted=true，dirty=false |
| 容器 | 三个服务运行中，重启次数 0，当前 OOMKilled=false |
| API | 容器内 /health HTTP 200，宿主端口 18001；simulation / advisory_only=true |
| 调度心跳 | 19:00 critical/heavy 均 status=ok、leader=true |
| 实际工件路径 | stock_analyzer_runtime_artifacts 命名卷，不是仓库 artifacts 目录 |
| 有效配置 | week5 enabled/auto_run/auto_notify/full_market_automation_enabled 全 true；夜扫 21:45 |
| 飞书 | notifications enabled=true，primary=feishu_app，backup=console；必要凭据非空（未记录值） |
| 静默窗口 | 00:30—08:30，不覆盖正常夜扫时间 |
| 数据更新 | NAS crontab 工作日 19:45 执行 stock_updater.sh；21:30/22:30 日线同步；夜扫 21:45 |
| 最近 readiness | 9/15 日线与 delta 均 5535 只，delta coverage_ratio=1.0，index/daily/delta ok |

检查时尚未到 9/16 19:45，readiness 仍为 9/15 不能直接判断为更新故障。

9/15 夜扫运行 21:45:04—22:07:43，约 22 分 39 秒，调度状态 success。该轮构建是 28dede0，早于 PR #82，不能用它验收今天的新修复。

真实漏斗：5486 只输入 → 3678 只质量硬筛合格 → 300 只质量池 → 100 只轻筛 → 50 只深评。

- 隔夜观察池 1 只：001368，score=65.89，action=watch，actionable=false。
- 最终信号 0：最低门槛 70；当晚 50 只全部低于门槛。因此即使修正过热误判，也不能断言昨晚必然出现买入信号。
- 50 只过热 level 全为 reject；最终拒因计数包含过热 48、cross_review_failed 49、below_min_threshold 50（拒因可重叠）。
- 调度产物明确 notify_enabled=false、notification=null。
- 9/14 三次夜扫 blocked_data_gate，原因为 intraday_freshness_missing / intraday_freshness_insufficient；当日 23:51 及 9/15 后续运行已成功。历史失败不等于当前仍存在同一阻断。

## 必须处理的问题

### 1. P0：夜扫结果没有飞书交付路径

源码：

- `src/stock_analyzer/runtime/service.py:20153` 的 `_job_week5_night_scan` 固定传 `notify_enabled=False`，没有使用已开启的 auto_notify。
- `src/stock_analyzer/runtime/services/week5_automation_service.py:169` 调用底层扫描时继续禁止通知。
- 同文件 279—286：即使外部请求 notify_enabled=true，也只记录 sent=false / overnight_advisory_only，不执行发送。

禁止夜间买入通知有合理边界，但“观察报告”不应因此一起消失。只改环境变量或飞书配置不能修好这个问题。

最小修复：新增隔夜观察摘要交付，复用现有飞书通知设施；明确标注交易日、数据日期、候选代码/名称/分数/理由和风险，不生成买入指令。候选为空或扫描失败也必须发相应摘要，旧池回退须标明旧日期。

### 2. P1：调度 success 不代表用户收到结果

`service.py:20129` 的 `_week5_scheduler_result` 判断扫描状态和 pipeline 状态，不判断飞书交付。昨晚 success 与 notify_enabled=false 同时存在，就是实际反例。

`notify/channels.py:901` 的 FailoverNotifier 在主渠道失败后直接返回备用渠道结果；ConsoleNotifier 返回 success=true。当前备用渠道就是 console。

容器内无网络 mock 实测：主渠道模拟失败、console 模拟成功，最终返回 `success=true, channel=console, error=''`。这只是“日志写出”，不能作为飞书送达验收。

最小修复：扫描完成与报告交付分别记录；保存真实飞书渠道结果，console 只算降级记录。失败保留待发送状态，按交易日/报告版本幂等重试，避免重跑二十多分钟扫描。注意 `service.py:12048` 附近 `_notify_if_changed` 在发送前写入去重缓存，不能直接照搬为可靠交付逻辑，否则首次发送失败也可能抑制后续重试。

### 3. P1：PR #82 修了常规输入，但缺历史边界仍不符合说明

源码：`week5_service.py:2993` 的 `_overextension_row` 在历史不足时保留末根 OHLC；`_overextension_decision_dict` 仍调用 evaluator；`risk/overextension.py:181` 附近仍用 ma5=1.0、atr14=0.03 作为缺省值。

在当前 NAS 镜像上使用生产两个入口进行无副作用复现：

| 输入 | 实测 |
| --- | --- |
| 30 根平稳 OHLC（open/close=12，high=13，low=11） | ma5 存在，level=none，bias=0，atr_distance=0 |
| 相同价格仅 3 根 | ma5 缺失，level=reject，bias=11，atr_distance=366.666667 |

说明常规路径修复已进入镜像，但“历史不足会走无法评估/none”这一注释与实际不符。未测定生产候选中缺历史的比例，不能把此边界问题夸大成现在所有股票仍被误拒。

最小修复：显式返回 insufficient_input，保留缺失原因；观察报告可解释展示，是否允许升级为可执行买入另行定义。不能把假指标当真值，也不能为了保证有股票而关掉风险门槛。

## 飞书并非整体不可用

运行审计 `runtime/runtime_state_history/audit_events.jsonl`：

- 9/16 12:30:35 午盘前简报：success=true，channel=feishu_app+feishu_enterprise。
- 9/16 15:11:19 对账确认：同上。
- 9/16 15:11:39 盘后研究摘要：同上。
- 9/15 22:24:57 夜间进化摘要：同上，包含另一口径的 20 只候选，而当晚 night_pool 为 1 只。

这证明系统记录的飞书发送结果正常，不证明用户本人已阅读。未新发消息，也未核对终端实际收件。已有盘后研究/进化摘要与夜扫候选不是同一份报告，应统一晚间交付的事实来源，避免“20 只候选 / 1 只观察 / 0 个信号”被混为一谈。

## 后置项与范围控制

当天盘中雷达有 timeout/circuit_open；竞价任务最后状态 expired、旧连续失败计数 14；模型因子报告仍有历史 manifest 身份不一致失败。它们属于完整平台的待办，不必全部作为“晚间观察报告能交付”的前置。若今后承诺盘中交易或模型生产就绪，须单独验收。

## 建议实施顺序与验收

1. 实现夜扫观察报告：成功有候选、正常空候选、数据未就绪/失败、旧池回退四种结果都可交付。
2. 加入独立交付状态和可重试幂等；仅真实飞书成功算完成，保留渠道失败原因。
3. 修正过热缺输入边界并补生产入口反例；不下调选股阈值。
4. 本地使用隔离状态目录验证上述场景及重复触发/发送失败后重试。生产冒烟可复用一份已完成报告，不重跑扫描；旧报告必须明示日期，不能冒充当天结果。
5. 部署后验收一次真实交易日晚间链路：当日 readiness → 本轮完成报告 → 有候选或明确空结果 → 飞书渠道成功 → 用户实际收件。保存同一交易日与报告版本的证据。

验收不以“每天必须凑够 5 只”定义。每天应有可解释的报告；只有满足条件时才列合格候选。当前不建议扩大为模型训练、自学习或全平台重构。

## 本轮验证限制

NAS 生产镜像不含 pytest 与 tests 目录，因此未运行 pytest 全套；已直接执行当前镜像的纯函数复现与通知 failover mock。没有触发真实发送，没有执行今晚完整夜扫；不能宣称 PR #82 的今晚实际选股表现或端到端交付已经验收。
