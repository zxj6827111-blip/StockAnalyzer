# 信号调参档案（2026-03-20）

## 1. 文档用途

本文档用于固定 2026-03-20 这一轮信号系统调参结论，避免后续开新线程时丢失上下文。

这里的 `V1 / V2` 不是项目原生发布版本号，也不是仓库里长期存在的正式产品标签，而是本轮围绕“买点长期不触发”问题定义的两档调参方案：

- `V1`：已经落地并上线到 NAS 的轻放松版
- `V2`：尚未落地的下一档候选方案

## 2. 本轮问题背景

在分析 NAS 导出的 support bundle 后，确认系统长期无买点并不完全是市场本身导致，还存在两类问题：

1. 结构层问题：
   - `evolution` 的软保守态被当成了硬故障处理
   - 结果是系统被错误地禁止开新仓
2. 参数层问题：
   - 结构修复后，系统虽然不再被 `risk_gate` 一刀切封禁
   - 但 `cross_review` 和 `financial_filter` 仍然过严，导致大部分股票继续停留在 `hold`

## 3. 结构修复内容

以下修复已经落地：

1. 硬降级 / 软降级拆分
   - 只有硬故障才禁止新开仓
   - `evolution conservative` 仅保留为保守运行提示、阈值抬升、仓位缩放
2. 增加 `decision_trace`
   - 每只票现在都能看到卡在哪个 gate
   - 重点包括 `risk_gate`、`liquidity_gate`、`cross_review_gate`、`financial_gate`、`final_decision`
3. 修复 watchlist 生命周期
   - `keep_if_empty` 不再无限续命
   - 新增空名单宽限次数和最大保留时长

## 4. V1 定义

### 4.1 状态

`V1` 已落地，属于当前正式观察版本。

### 4.2 参数内容

`V1` 的核心目标是“轻放松”，优先放松最可能导致长期 0 actionable 的两个位置：

1. 财务缺失数据策略
   - `financial_filter.missing_data_policy: reject -> allow`
2. 交叉评审阈值
   - `p_lgbm_min: 0.62 -> 0.60`
   - `p_xgb_min: 0.60 -> 0.58`
   - `p_meta_min: 0.58 -> 0.56`
   - `max_diff: 0.12 -> 0.14`

### 4.3 当前代码中的 V1 实际值

- `financial_filter.missing_data_policy = allow`
- `models.cross_review.p_lgbm_min = 0.60`
- `models.cross_review.p_xgb_min = 0.58`
- `models.cross_review.max_diff = 0.14`
- `models.cross_review.p_meta_min = 0.56`

### 4.4 V1 不改动的部分

本轮 `V1` 不动以下阈值：

- `trend` 评分门槛
- `monster` 评分门槛
- `auto_sync_watchlist_min_score`

原因是当前判断应先解决“长期完全出不来信号”的问题，而不是先改总分口径。

## 5. V2 定义

### 5.1 状态

`V2` 目前只是候选方案，尚未落地。

### 5.2 触发条件

如果 `V1` 连续观察 2 到 3 个交易日后，仍然满足以下任一情况，则考虑升级到 `V2`：

- 连续多日 `actionable = 0`
- `decision_trace` 显示主要拦截仍集中在 `cross_review` 和评分门槛
- 系统结构正常，但买点数量仍明显偏低

### 5.3 候选内容

`V2` 在 `V1` 基础上继续放松：

1. 继续保留 `V1` 的全部调整
2. 下调策略分档门槛
   - `trend A/B: 65/55 -> 63/53`
   - `monster A/B: 64/54 -> 62/52`
3. 放松 watchlist 同步最低分
   - `auto_sync_watchlist_min_score: 65 -> 62`

## 6. 观察周期建议

建议从 `V1` 上线开始，先观察 `2 到 3` 个交易日，优先按 `3` 个交易日评估。

本轮建议观察的重点不是“有没有马上很多买点”，而是：

1. 是否从长期 `0 actionable` 改善为偶尔出现可执行信号
2. `decision_trace` 的拦截重心是否发生变化
3. 新出现的候选票质量是否明显变差
4. 系统是否仍长期处于 `degraded` 保守态

## 7. 每日 NAS 操作

### 7.1 收盘后导出 support bundle

在 NAS 项目目录执行：

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

如需拷贝到当前目录：

```bash
docker cp stock-analyzer-api:/app/artifacts/support/nas_support_bundle.json ./nas_support_bundle.json
```

### 7.2 代码更新后重建容器

把本地代码同步到 NAS 后，在 NAS 项目目录执行：

```bash
cd /vol1/docker/StockAnalyzer
docker compose \
  -f docker-compose.yml \
  -f docker-compose.runtime.yml \
  -f docker-compose.runtime.localvol.yml \
  up -d --build api scheduler
```

### 7.3 更新后健康检查

```bash
curl "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/health"
```

如需直接测试通知链路：

```bash
test -n "${SA__SECURITY__API_TOKEN:-}" || { echo "SA__SECURITY__API_TOKEN is required"; exit 1; }
curl -X POST "http://127.0.0.1:${SA_API_HOST_PORT:-18001}/notify/test" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${SA__SECURITY__API_TOKEN}" \
  -d "{\"title\":\"NAS通知测试\",\"content\":\"检查自动通知链路\"}"
```

避免反复或未带 token 调用 `/notify/test`；该接口会真实发送通知。

不要优先在 NAS 主机直接执行 `python3 scripts/export_support_bundle.py`，主机通常没有项目依赖。

在容器内执行的标准命令如下：

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

## 8. 新开线程时如何描述

如果后续新开线程，建议直接引用本文档并补一句当日情况。

推荐模板：

```text
请按 docs/signal_tuning_v1_v2_20260320.md 作为当前调参基线继续分析。
当前 NAS 已上线 V1。
这是今天收盘后的 nas_support_bundle，请判断：
1. 今天系统运行是否正常
2. 当前无买点的主因是什么
3. V1 应维持，还是升级到 V2
4. 如果要改，请给出明确修改方案
```

更简版模板：

```text
请基于 docs/signal_tuning_v1_v2_20260320.md 和今天的 nas_support_bundle，
判断 V1 现在是刚刚好，还是应该升级到 V2。
```

## 9. 当前结论

截至 2026-03-20：

- 结构修复已经完成
- `V1` 已经落地并完成本地验证
- 暂不建议每日人工改参数
- 先按 `V1` 连续观察 2 到 3 个交易日
- 收盘后以 support bundle 为准，再决定是否升级到 `V2`
