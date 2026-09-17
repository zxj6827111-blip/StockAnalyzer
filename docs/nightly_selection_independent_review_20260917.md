# 晚间选股与飞书交付：独立验收 2026-09-17

## 结论

检查时间：2026-09-17 08:07 起（北京时间）。本地 HEAD d033aa3；NAS checkout/实际 build 6869481；两者差异仅实施文档，无业务代码差异。

结论：部署通过、回放飞书 API 交付通过；本地四个目标测试套件独立执行通过。但可靠交付验收 NO-GO：以下并发与恢复问题已在当前生产镜像的隔离临时目录复现。尚无新链路真实交易日自动夜扫证据，不能判定已全部正常。

zcode 的实施文档本身也把 9/17 21:45 真实夜间验收和连续三个交易日稳定运行列为待完成，不能把它的本地通过解释为生产闭环通过。

## 当前 NAS 证据

- api / critical / heavy 同一镜像 sha256:b9d91cef03496f8565714a34ac06385f98c5fc3b3b84d9a8b3ff6a1682cabd72。
- 三容器 9/17 07:27 左右启动，RestartCount=0、OOMKilled=false；critical/heavy 心跳正常且 leader=true。
- /health 为 ok；build=686948184b3845362864e388da0591c5a403c719、trusted=true、dirty=false。
- nightly.enabled=true；simulation、advisory_only=true。
- nightly_reports 仅见 9/16 回放 rp-20260916；published_report_id 为空，不能当作正式晚报。
- 回放主应用与企业渠道均 delivered、attempts=1，时间分别为 00:47:12 / 00:47:13，有 message_id。
- 新自动链尚未经历今晚 21:45，自然夜间功能与三个交易日稳定性均未验收。
- 本轮没有发送新消息，因此没有取得本轮新的用户收件确认。

## R1 / P1：日期状态锁未取得也继续写入

位置：src/stock_analyzer/runtime/services/nightly_report_service.py:237—259，尤其第248行。

update_date_state 调用 lock.acquire() 后忽略返回值；而 DistributedFileLock.acquire() 被其他持有者占用时返回 False，不会抛错。此时仍继续读、合并和替换 state.json。

生产镜像隔离复现：先由另一个锁对象持有 state.lock，再调用 update_date_state。

```json
{"lock_owner_alive":true,"state":"OVERWRITTEN_WITHOUT_LOCK"}
```

影响：heavy 发布报告与 critical 登记交付/提醒可以同时操作同一交易日状态，可能丢失报告指针、notices、delivery_ids。原子文件替换只能保证文件完整，不能避免更新相互覆盖。

此外，publish 的读当前版本、分配 revision、冻结文件与更新指针不在同一个锁范围内；仅修正 acquire 返回值还不足以完成发布事务。

整改：取得日期锁才可读改写；失败要明确返回繁忙或有界等待。版本分配与冻结/指针发布使用同一日期级事务边界；避免嵌套重新取得相同非重入锁。列表/映射的合并必须在锁内从最新状态计算。补两个写入者保留各自更新、并发发布不覆盖冻结文件的测试。

## R2 / P1：补发与恢复绕过交付锁，能改写在途发送

位置：nightly_delivery_service.py 的 request_retry（382起）、_apply_pending_receipt（479起）、_recover_stale_sending（518起）。

- request_retry 直接保存 pending 和 attempts=0，不获取交付锁，不排除 sending。
- _recover_stale_sending 新建一个锁对象后调用 is_held()；该方法仅表示“这个对象自己是否持锁”，新对象固定未持锁，不能探测另一个进程的持有状态。
- 回执恢复也没有在相同交付锁内完成读取和写回。

隔离复现：交付锁持有者仍存活，记录为 sending；调用补发后记录改成 pending。随后设置过期 lease、仍保持实际锁，恢复函数仍把记录改成 retry_wait。

```json
{"owner_alive":true,"response_queued":true,"record_state":"pending"}
{"owner_alive":true,"changed":true,"record_state":"retry_wait"}
```

影响：发送端与补发/恢复端对同一记录产生竞争，可能覆盖 delivered、重置次数或提前重试。发送入口自身有锁不代表所有写入者都已受保护。是否最终重复推送取决于竞争时序及远端幂等窗口；本次证据直接证明的是在途状态可被错误改写。

整改：ensure_records、request_retry、回执并账、sending恢复、过期unknown处理均统一到每delivery_id同一锁下；拿锁后重新读取最新状态。活跃发送返回in_progress，不改次数与UUID。恢复程序必须实际尝试取得锁，而不是用新对象的is_held作为占用检测。补“sender写入delivered与补发并发”测试，确保delivered不可被旧副本覆盖。

## R3 / P1：报告落盘后、指针发布前崩溃无法恢复

位置：nightly_report_service.py:321 的 reports_for 与 nightly_delivery_service.py:449 的 recover。

recover 只遍历 reports_for 返回值；reports_for 只认 state.json 中已经发布的报告和notice指针。因此 freeze_report 成功、更新日期指针之前进程退出，恢复程序看不到已冻结文件。

隔离复现：保存 nr-20260917-01.json、不写published_report_id，然后调用recover。

```json
{"created_records":[],"frozen_exists":true,"discoverable":0,"delivery_files":0}
```

影响：报告明明已经生成并保存，仍可能一直没有待发记录，最终漏发或被误报成未完成。这正是v2要求恢复的中断窗口，不属于超范围增强。

整改：在日期事务中记录可恢复的发布意图，或仅在最近两个日期目录中有界枚举并核验冻结文件。校验日期、report_id、revision和内容摘要后修复正式指针并幂等建交付记录；旧版本和回放不得冒充新正式结果。补freeze后/指针前、指针后/记录前两个独立崩溃测试。

## R4 / P2：全局关闭通知未阻止晚报自动发送

位置：nightly_delivery_service.py:1003 的 _is_enabled 只读取 nightly.enabled；tick后续未读取 notifications.enabled。

使用假发送器、notifications.enabled=False、nightly.enabled=True，正常调用tick：

```json
{"notifications_enabled":false,"fake_send_calls":1,"delivered":1}
```

无真实网络调用。说明全局停发开关对新链路失效；当前线上开关为开启，因此不是已经观察到的现网停发事故。

整改：自动与手动入口分别定义开关语义；自动链必须同时服从nightly与notifications全局开关，以及项目现有外部通知禁用机制。暂停时保留pending，恢复后继续；补开关组合测试，不只测nightly.enabled=False。

## 验证范围与限制

独立执行通过（exit=0）：

- tests/test_nightly_report.py
- tests/test_nightly_delivery.py
- tests/test_nightly_scheduling.py
- tests/test_overextension_inputs.py

初次用本地Python3.12直接加载历史依赖目录收集失败，原因是目录中的numpy/pydantic_core扩展为cp313。随后使用Codex bundled Python及其原生包优先、项目历史依赖路径后追加的方式成功运行上述套件。环境故障不计为代码测试失败。

未独立重跑全量2966项、lint及mypy；文档中的全量结论仍是zcode自报，不作为本轮独立结果。目标套件通过不能覆盖上面新发现的反例。

所有反例在NAS容器/tmp随机临时目录完成，使用简单配置替身和假notifier。没有实例化生产完整服务来触发业务，没有修改生产artifacts、配置、调度或数据库，没有发送飞书消息，没有重启容器。

## 放行条件

1. 修复R1—R3并新增真实竞争/崩溃窗口测试；R4同批修复。
2. 再次运行上述目标回归及新增反例；检查无关旧通知行为未回归。
3. 按已有NAS部署脚本发布明确提交，核对三容器build一致。
4. 完成一个真实交易日自动链：当日数据 → 正常完成/正常空结果 → 正式冻结报告 → 真实飞书目标回执 → 用户实收一致。
5. 连续三个交易日通过后再标记运行稳定。仅故障通知送达不算选股成功。

本轮是独立检查，未代替zcode修改业务代码。
