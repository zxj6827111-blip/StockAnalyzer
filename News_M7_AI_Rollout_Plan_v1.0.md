# 真实新闻采集 + M7 入库 + AI 辅助审核开关 v1.0

## 已落地内容

本轮已完成以下能力：

1. 新增真实新闻采集入口 `m7-live-news-sync`
2. 复用 `AKShare stock_news_em` 采集个股新闻
3. 将新闻统一落盘到 `m7_news_latest.jsonl`
4. 夜间 `M7` 构建阶段可直接复用 live 新闻记录
5. 新增 AI 审核开关，可对新闻做情绪/方向辅助审核

## 当前工作方式

### 真实新闻采集
- 数据源：`AKShare stock_news_em`
- 范围：默认优先采集观察池 / 最新候选 / watchlist 标的
- 结果：写入 `artifacts/evolution/inputs/m7_news_latest.jsonl`

### M7 入库
- live 新闻记录会标准化为以下字段：
  - `event_id`
  - `symbol`
  - `headline`
  - `content`
  - `published_at`
  - `source`
  - `url`
  - `sentiment`
  - `llm_sentiment`
  - `llm_verdict`
  - `llm_confidence`

### AI 辅助审核开关
- 默认关闭
- 打开后，会对 live 新闻进行语义审核
- 输出：
  - `llm_sentiment`
  - `llm_confidence`
  - `llm_news_verdict`
  - `llm_reason`

## 推荐启用方式

### 仅启用真实新闻采集

配置：

```yaml
evolution:
  m7_live_news_enabled: true
```

### 启用真实新闻 + AI 辅助审核

配置：

```yaml
evolution:
  m7_live_news_enabled: true
  m7_ai_review_enabled: true
  llm_provider: "openai_compatible"
  llm_base_url: "你的兼容地址"
  llm_model: "你的模型"
  llm_api_key: "你的 key"
```

## 手动执行命令

```bash
python -m stock_analyzer.cli m7-live-news-sync --symbols 600000,000001 --force-refresh
```

启用 AI 审核：

```bash
python -m stock_analyzer.cli m7-live-news-sync --symbols 600000,000001 --force-refresh --enable-ai-review
```

## 建议的下一步

1. 先在本地模拟盘启用 `m7_live_news_enabled`
2. 连续观察 3~5 个交易日的新闻落盘质量
3. 再启用 `m7_ai_review_enabled`
4. 最后再把盘前 / 午间推送与这份 live 新闻产物打通
