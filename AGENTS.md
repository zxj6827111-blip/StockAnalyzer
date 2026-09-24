# AGENTS.md — StockAnalyzer 项目规约

> 本文件定义 StockAnalyzer 项目级工程事实、修改边界与验证要求。  
> 本机通用行为规则由全局 `AGENTS.md` 管理。  
> 原则：先理解现有系统，再修改；生产安全和数据正确性优先于开发速度。

---

## 1. 项目定位

StockAnalyzer 是长期运行的股票分析与研究系统。

当前重点系统包括 Alpha V2，以及与其相关的：

- 数据处理；
- 调度任务；
- 模型训练与冻结；
- Shadow 运行；
- Production Preflight；
- 双价格 QFQ / RAW 数据链路；
- Runtime Identity；
- 模型工件完整性；
- NAS Docker 生产运行环境。

这是长期维护项目，不按一次性脚本处理。

修改时优先保证：

1. 数据正确性；
2. 时间点一致性；
3. 可复现性；
4. fail-closed；
5. 生产运行稳定；
6. 审计证据完整。

---

## 2. 生产环境

StockAnalyzer 当前生产环境运行在飞牛 NAS。

项目部署目录：

```text
/vol1/docker/StockAnalyzer
```

主要 Docker 服务包括：

- `api`
- `scheduler-heavy`
- `scheduler-critical`
- `redis`

生产镜像：

```text
stock-analyzer:latest
```

仓库内可核实的生产事实（`docker-compose.yml`、`docker-compose.memlimit.yml`、
`scripts/nas_compose_files.sh`）：

- 服务名就是上面四个；`container_name` 只在基础 `docker-compose.yml` 里定义
  （`stock-analyzer-api` / `-scheduler-critical` / `-scheduler-heavy` / `-redis`）；
- 内存限制**只在** `docker-compose.memlimit.yml` 里设置（api / scheduler-heavy 4g、
  scheduler-critical 3g、redis 512m）。只用基础文件 = 没有内存上限；
- 生产用哪些 compose 文件的**唯一**组合来源是 `scripts/nas_compose_files.sh`。
  手工拼 `-f` 会漏掉文件并触发 `SA_NAS_PRODUCTION_GUARD`（2026-08-28 与 2026-09-10 两次
  同型事故的来源），守卫是 fail-closed，不要绕。

具体服务器 IP、Tailscale 地址、SSH 配置等机器级信息不得写入本仓库；需要时从本机 `machine-profile.md` 或 SSH 配置读取。

### 2.1 生产规则

生产环境已经存在真实运行任务。

任何生产修改之前必须先：

1. 查看当前运行状态；
2. 确认当前 commit / image；
3. 确认正在运行的 scheduler；
4. 判断修改是否影响历史数据、marker、artifact 或模型；
5. 明确回滚方案。

未经用户明确要求，不得：

- 自动重新部署生产；
- 自动重启所有容器；
- 自动删除 volume；
- 自动清理数据库；
- 自动覆盖 artifact；
- 自动修改历史 marker。

---

## 3. 项目知识读取顺序

接手较大任务时，按以下顺序理解项目。

### 3.1 第一跳：项目规约

读取：

```text
AGENTS.md
```

### 3.2 第二跳：知识索引

读取：

```text
.agents/notes/README.md
```

它第 4 节是**索引表**（文档 / Status / 主题 / 什么时候需要读）。

先看索引表再决定读哪一篇，了解已有的重要架构决策、事故与业务约束。

### 3.3 第三跳：命中的 Note / ADR

只读取与当前任务直接相关的 1～2 篇 Note / ADR。

禁止为了“全面了解项目”一次性读取所有 Notes。

代码索引用来回答：

> 代码现在是什么？

Notes / ADR 用来回答：

> 为什么这样设计？  
> 什么不能随便改？  
> 哪些方案以前已经失败过？

二者不能互相替代。

---

## 4. 修改前必须完成的检查

修改代码前至少确认：

1. 当前 Git 状态；
2. 当前分支；
3. 目标文件；
4. 当前实现；
5. 调用方；
6. 数据流；
7. 相关测试；
8. 是否存在对应 Note / ADR；
9. 是否涉及生产状态或历史数据。

至少执行：

```bash
git status
git diff
```

如果工作树已经存在用户修改，不得覆盖、回退或顺手整理无关代码。

---

## 5. 核心数据原则

StockAnalyzer 中以下问题属于高风险区域：

- PIT / Point-in-Time 数据；
- QFQ / RAW 双价格；
- 股票交易日；
- 停牌日；
- 数据可用性；
- 模型训练窗口；
- freeze；
- epoch；
- Runtime Identity；
- artifact identity；
- marker；
- Shadow / Production 边界。

修改这些区域时，不得仅因为测试通过就认为业务逻辑正确。

必须同时验证：

- 时间点；
- universe；
- 数据可用性；
- look-ahead；
- 缺失数据；
- fail-open / fail-closed；
- artifact 身份；
- commit 身份。

---

## 6. Alpha V2 特别规则

Alpha V2 属于项目关键路径。

涉及双价格冻结的核心代码（路径已对照仓库核实）：

```text
src/stock_analyzer/alpha_v2/dual_price_series.py
src/stock_analyzer/alpha_v2/validation/dual_price_freeze.py
src/stock_analyzer/alpha_v2/research/panel.py            # pit_universe / certify_price_mode
scripts/alpha_v2_shadow_model_freeze.py
scripts/alpha_v2_validation_freeze.py
scripts/alpha_v2_shadow_capture.py
scripts/alpha_v2_shadow_mature.py
scripts/alpha_v2_production_preflight.py
```

（注意：`dual_price_series.py` 在 `alpha_v2/` 根下，**不在** `validation/` 下。）

动手前先读 `.agents/notes/ADR-002-dual-price-freeze-contract.md` 与
`.agents/notes/NOTE-001-alpha-v2-production-gates.md`。

修改相关逻辑前必须检查对应调用关系与测试。

### 6.1 双价格原则

必须明确区分：

- PIT universe；
- 实际交易数据可用性；
- QFQ；
- RAW；
- 停牌；
- 真正的数据缺失。

不得为了让 freeze 通过而：

- 偷换 universe；
- 修改 Alpha 定义；
- 静默跳过真实数据缺失；
- 把 fail-closed 改成 fail-open。

如果只修复执行可用性 / 价格序列对齐问题，不得顺手修改 Alpha 信号逻辑。

---

## 7. Runtime Identity 与模型工件

模型训练、freeze、Shadow、Production 之间的代码身份必须可验证。

涉及以下内容时视为高风险修改：

- training code commit；
- runtime code commit；
- freeze manifest；
- epoch；
- model artifact；
- checksum / identity；
- Production Preflight。

缺失、unknown、malformed、不一致或篡改不得默认放行。

原则上采用：

```text
fail closed
```

除非已有明确 ADR 改变这一约束。

### 7.1 已核实的实现

身份有两种**互斥**的 runtime context（都不是"优先级"关系）：
`git_checkout`（源码检出：git HEAD 自证）与
`container_build_identity`（不可变容器，无 git 二进制、无 `.git`：
`.build_commit` 与 `build_manifest.commit` 互证）。

其余落点：唯一裁决入口是
`src/stock_analyzer/alpha_v2/validation/runtime_identity.py` 的
`resolve_runtime_code_identity()`（先判 context，再套规则）；
以及 `src/stock_analyzer/build_identity.py`、`scripts/generate_build_manifest.py`、
`src/stock_analyzer/alpha_v2/validation/{frozen_model,freeze,freeze_precheck,epoch,preflight}.py`、
`scripts/verify_container_build_identity.py`、`Dockerfile`。

### 7.2 硬性规则

1. 任何 CLI 或 service **不得**自行 `git rev-parse` 解析运行身份。四个 Alpha V2 CLI
   由 AST 测试 `test_alpha_v2_clis_use_shared_resolver_not_git_head` 钉住；
   新增入口要一并进那个测试的参数表。
2. 训练 commit 与运行 commit 是两个不同事实，必须相等且不得互相回填
   （`assert_model_training_commit`）。
3. 违例必须变成**真实退出码**（身份 5 / 价格口径 4 / Preflight 7）。文档写 4、进程退 1
   视为缺陷，不是小事。
4. 本机制只有 sha256 自锚哈希，**没有**签名体系。要改这条信任边界必须新开 ADR，
   不得在提交说明或注释里顺手当作已具备。

详见 `.agents/notes/ADR-001-runtime-identity-and-artifact-integrity.md`。

---

## 8. 代码修改原则

优先做最小必要修改。

不要因为局部问题而：

- 大规模重构目录；
- 更换框架；
- 改写无关模块；
- 修改公共接口；
- 修改数据模型。

如果确实需要跨模块调整，先给出影响分析和实施方案。

---

## 9. 测试与验证

修改完成后必须进行最小但有意义的验证。

优先顺序：

1. 目标单元测试；
2. 相关模块测试；
3. 静态检查；
4. 必要的集成验证；
5. 必要时再做完整 Preflight。

不得为了证明“没问题”直接跑大规模生产任务。

### 9.0 仓库里真实存在的验证命令

CI（`.github/workflows/quality.yml`）**不直接跑 pytest**，而是跑分层质量门：

```bash
python scripts/run_quality_gate.py --stage clean-scope --fail-on-error
python scripts/run_quality_gate.py --stage full --fail-on-error
```

可用 stage：`clean-scope` `smoke` `integration` `slow-report` `full` `all`
（定义在 `scripts/run_quality_gate.py`，实现在
`src/stock_analyzer/ops/quality_gate.py`）。

本地日常命令（`Makefile`）：

```bash
make lint        # ruff check src tests
make typecheck   # mypy src
make test        # pytest（pyproject 已配 addopts=-q, testpaths=tests）
pytest tests/test_alpha_v2_m3_enforcement.py        # 定向单文件
pytest -k "runtime_identity"                        # 定向关键字
```

注意规模：`tests/` 有 364 个平铺测试文件，`src/` 有 351 个模块文件。
全量套件属于"Level 3"，不是默认动作。

不得伪造以下状态：

- 测试通过；
- API 正常；
- scheduler 正常；
- freeze PASS；
- production ready。

### 9.1 状态必须区分

```text
代码完成
≠ 测试完成
≠ Freeze Ready
≠ Production Ready
≠ 已部署生产
```

汇报时必须说明当前属于哪一级。

---

## 10. Freeze / Training 操作边界

涉及以下操作时必须严格按照当前任务书执行：

- freeze；
- model freeze；
- epoch；
- training；
- Production Preflight；
- Shadow 切换；
- Production 切换。

如果前置硬门失败，立即停止后续阶段。

各阶段之间的门禁顺序、退出码含义与"当前哪些状态根本不可达"，见
`.agents/notes/NOTE-001-alpha-v2-production-gates.md`。
不要凭退出码字面数字推断失败原因，也不要假设某个晋升入口存在——先在
`scripts/` 里确认它真的存在。

不得为了“把流程跑完”绕过硬门。

出现代码缺陷导致 `BLOCKED` 时，先修代码和测试，再重新执行对应阶段。

---

## 11. NAS 操作边界

NAS 是真实运行环境，不是默认实验机。

生产 NAS 上优先做：

- 运行；
- 调度；
- 必要验证。

高内存训练、freeze 或一次性重计算是否适合 NAS，必须根据实际资源评估决定。

不得默认认为：

> “程序能启动” = “NAS 适合执行完整 freeze”。

---

## 12. Notes / ADR 同步规则

仓库决策知识位于：

```text
.agents/notes/
```

以下变化原则上需要同步 Note / ADR：

- 数据语义改变；
- PIT 规则改变；
- Alpha 核心定义改变；
- 状态机改变；
- 模型身份规则改变；
- freeze 合约改变；
- Production / Shadow 边界改变；
- 核心 Service 接口改变；
- 已有架构决策被推翻。

以下修改通常不需要新增 ADR：

- 修 typo；
- UI 小改；
- 单纯测试补充；
- 不改变语义的重构。

### 12.1 同步动作的具体含义

"同步 Note" = 同一个提交内改完这四样，缺一不可：

1. Note 正文（决策 / 不变量 / 落点）；
2. 该 Note 的 Implementation Locations（路径必须是真实存在的，逐条核对）；
3. 该 Note 头部的 `As-of`（核实日期 + 当时 HEAD SHA）；
4. `.agents/notes/README.md` 索引表的那一行（主题、什么时候需要读）。

`Status` 只能取 `Draft` / `Accepted` / `Superseded`。证据不足写 Draft，
不要为了看起来完整写 Accepted。

分工，不得互相复制正文：

```text
AGENTS.md            长期行为规则与项目边界（本文件）
notes/README.md      知识地图与索引，一行一条
ADR / NOTE           具体决策、被否决方案、事故复盘、代码落点
```

---

## 13. ADR 演进规则

重要设计发生改变时，不要直接修改旧 ADR 并抹掉历史判断。

正确做法：

1. 旧 ADR 标记：

   ```text
   Status: Superseded
   ```

2. 同时填写：

   ```text
   Superseded-by: ADR-XXX
   ```

3. 新建 ADR 记录新决策。

这样可以保留完整决策链。

---

## 14. Git 规则

提交前执行：

```bash
git status
git diff
```

确认：

- 只有目标修改；
- 没有临时文件；
- 没有密钥；
- 没有错误 artifact；
- 没有覆盖用户已有修改。

未经用户明确允许，不得执行：

```text
git reset --hard
git clean -f
force push
--no-verify
```

同时不得：

- 删除远端分支；
- 改写已经推送的历史。

仓库根目录长期存在数百个 `.pytest-*` / `tmp_*` 目录（已被 `.gitignore` 覆盖，所以
`git status` 看不到）。它们里面常有历史验证证据。**不得**用 `git clean -fdx`
之类的"清理工作区"动作去删它们；需要清理时先问用户。

优先使用新的 commit 修复问题。

---

## 15. 凭据与敏感信息

不得提交：

- API Key；
- Token；
- SSH 私钥；
- 密码；
- Cookie；
- 私有网关凭据。

`.env` 中真实凭据不得写入：

- `AGENTS.md`；
- Notes；
- ADR；
- 测试 fixture；
- 示例文档。

---

## 16. 完成标准

每次任务结束至少按以下四段汇报：

### 结果

实际修改了什么。

### 验证

实际运行了哪些命令，结果是什么。

### 风险

还有哪些没有验证。

### 下一步

下一项最小且必要的动作。

禁止使用“应该没问题”代替真实验证结果。
