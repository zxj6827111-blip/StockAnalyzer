import { Newspaper, RefreshCw, Trash2 } from 'lucide-react';

import { apiGet, apiPost } from '../lib/api';
import { formatDateTime, formatNumber, truncateText } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface NewsItem {
  symbol?: string;
  title?: string;
  source?: string;
  url?: string;
  published_at?: string;
  news_component?: number;
}

interface NewsBriefResponse {
  phase_label?: string;
  records?: number;
  raw_records?: number;
  generated_at?: string;
  real_news_available?: boolean;
  cache_hit?: boolean;
  items?: NewsItem[];
}

interface NewsScoreItem {
  symbol?: string;
  news_component?: number;
  status?: string;
  reasons?: string[];
}

interface NewsScoreResponse {
  records?: number;
  items?: NewsScoreItem[];
  summary?: {
    average_news_component?: number;
    positive_records?: number;
    neutral_records?: number;
    negative_records?: number;
  };
}

interface NewsPageBundle {
  premarket: NewsBriefResponse;
  midday: NewsBriefResponse;
  watchlist: NewsScoreResponse;
}

interface NewsPanelProps {
  title: string;
  emptyText: string;
  warningText: string;
  response: NewsBriefResponse | undefined;
  loading: boolean;
}

function NewsPanel({ title, emptyText, warningText, response, loading }: NewsPanelProps) {
  const hasLoaded = Boolean(response);
  const items = response?.items ?? [];
  const showWarning = hasLoaded && !loading && response?.real_news_available === false;
  const generatedAt = response?.generated_at ? formatDateTime(response.generated_at) : '-';
  const metaLine = hasLoaded
    ? `简报生成于 ${generatedAt} · 展示的是新闻发布时间，盘前出现昨晚新闻属于正常现象`
    : '正在抓取并整理这组新闻...';

  return (
    <div className="glass-panel p-6 space-y-4">
      <div className="space-y-2">
        <h2 className="text-xl font-bold">{title}</h2>
        <div className="text-xs text-muted">{metaLine}</div>
      </div>

      {showWarning ? (
        <div className="rounded-xl border border-warn bg-[rgba(255,127,80,0.08)] p-4 text-sm text-warn">
          {warningText}
        </div>
      ) : null}

      <div className="space-y-3">
        {!hasLoaded && loading ? (
          <div className="text-muted">正在抓取真实新闻标题...</div>
        ) : items.length ? (
          items.map((item, index) => (
            <div
              key={`${title}-${item.symbol}-${index}`}
              className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4"
            >
              <div className="font-bold">
                {item.symbol || '-'} · {truncateText(item.title, 44)}
              </div>
              <div className="mt-2 text-xs text-muted">
                发布时间 {formatDateTime(item.published_at)} · {item.source || '未知来源'}
              </div>
              {item.url ? (
                <a
                  className="mt-2 inline-block text-xs text-accent"
                  href={item.url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看原文
                </a>
              ) : null}
            </div>
          ))
        ) : (
          <div className="text-muted">{emptyText}</div>
        )}
      </div>
    </div>
  );
}

export default function News() {
  const { data, error, loading, refresh, lastUpdated } = useAutoRefresh<NewsPageBundle>(
    async () => {
      const [premarket, midday, watchlist] = await Promise.all([
        apiGet<NewsBriefResponse>('/news/briefing/latest?phase=premarket&limit=6'),
        apiGet<NewsBriefResponse>('/news/briefing/latest?phase=midday&limit=6'),
        apiGet<NewsScoreResponse>('/news/score/watchlist?limit=20'),
      ]);
      return { premarket, midday, watchlist };
    },
    30000,
  );

  async function forceRefresh(): Promise<void> {
    await Promise.all([
      apiGet<NewsBriefResponse>('/news/briefing/latest?phase=premarket&limit=6&force_refresh=true'),
      apiGet<NewsBriefResponse>('/news/briefing/latest?phase=midday&limit=6&force_refresh=true'),
    ]);
    await refresh();
  }

  async function clearCache(): Promise<void> {
    await apiPost('/news/score/cache/clear', {
      symbol: '',
      strategy: '',
    });
    await refresh();
  }

  const hasLoaded = Boolean(data);
  const scoreItems = data?.watchlist.items ?? [];
  const summary = data?.watchlist.summary;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
            <Newspaper className="h-7 w-7 text-accent" /> 新闻与因子
          </h1>
          <p className="mt-2 text-muted">
            这里展示真实新闻抓取结果与新闻因子评分，不再把加载中的状态误显示成“无新闻”。
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right text-xs text-muted">
            <div>最近刷新：{lastUpdated ? formatDateTime(lastUpdated) : '-'}</div>
            <div>状态：{loading ? '更新中' : '已同步'}</div>
          </div>
          <button className="btn-outline" onClick={() => void forceRefresh()}>
            <RefreshCw className="h-4 w-4" /> 强制刷新
          </button>
          <button
            className="btn-outline border-warn text-warn hover:bg-warn hover:text-white"
            onClick={() => void clearCache()}
          >
            <Trash2 className="h-4 w-4" /> 清空评分缓存
          </button>
        </div>
      </div>

      {error ? <div className="glass-panel border-warn p-4 text-warn">加载失败：{error}</div> : null}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <NewsPanel
          title="盘前重点新闻"
          emptyText="暂无盘前新闻标题。"
          warningText="当前没有抓到可用的盘前真实新闻标题；系统不会继续拿代理分数冒充新闻。"
          response={data?.premarket}
          loading={loading}
        />
        <NewsPanel
          title="午盘前重点新闻"
          emptyText="暂无午盘前新闻标题。"
          warningText="当前没有抓到可用的午盘前真实新闻标题；请优先结合盘中异动复核。"
          response={data?.midday}
          loading={loading}
        />
      </div>

      <div className="glass-panel p-6">
        <div className="mb-4 flex items-center justify-between gap-4">
          <h2 className="text-xl font-bold">观察池新闻因子</h2>
          <div className="text-sm text-muted">
            {hasLoaded ? (
              <>
                平均分 {formatNumber(summary?.average_news_component, 3)} · 正向{' '}
                {summary?.positive_records ?? 0} · 中性 {summary?.neutral_records ?? 0} · 负向{' '}
                {summary?.negative_records ?? 0}
              </>
            ) : (
              '正在计算新闻因子摘要...'
            )}
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>代码</th>
                <th>新闻因子</th>
                <th>状态</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {!hasLoaded && loading ? (
                <tr>
                  <td colSpan={4} className="text-muted">
                    正在拉取观察池新闻因子...
                  </td>
                </tr>
              ) : scoreItems.length ? (
                scoreItems.map((item) => (
                  <tr key={`score-${item.symbol}`}>
                    <td>{item.symbol || '-'}</td>
                    <td>{formatNumber(item.news_component, 3)}</td>
                    <td>{item.status || '-'}</td>
                    <td>{truncateText((item.reasons ?? []).join('；'), 72) || '-'}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={4} className="text-muted">
                    暂无新闻因子数据
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
