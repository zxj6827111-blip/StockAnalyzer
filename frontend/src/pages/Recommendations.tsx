import { AlertTriangle, CheckCircle2, RefreshCw, Target, TrendingUp } from 'lucide-react';

import { apiGet } from '../lib/api';
import { formatDateTime, formatNumber, formatPercent } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface TradePlan {
  status?: string;
  entry_range?: number[];
  entry_low?: number;
  entry_high?: number;
  stop_loss_price?: number;
  take_profit_prices?: number[];
  invalid_after?: string;
  max_hold_days?: number;
}

interface RecommendationItem {
  symbol?: string;
  strategy?: string;
  status?: string;
  first_recommended_at?: string;
  updated_at?: string;
  last_signal_score?: number;
  trade_plan?: TradePlan;
  entry_price?: number;
  exit_price?: number;
  exit_alert_reason?: string;
  closed_reason?: string;
  realized_return_pct?: number;
  current_return_pct?: number;
  holding_days?: number;
  is_open_position?: boolean;
  outcome_status?: string;
}

interface RecommendationResponse {
  records?: number;
  summary?: {
    records?: number;
    open_records?: number;
    closed_records?: number;
    win_records?: number;
    loss_records?: number;
    win_rate?: number;
    avg_realized_return_pct?: number;
    total_realized_return_pct?: number;
    avg_open_return_pct?: number;
    avg_holding_days?: number;
    status_breakdown?: Record<string, number>;
  };
  items?: RecommendationItem[];
}

function statusTone(status: string | undefined): string {
  if (status === 'closed' || status === 'sold') {
    return 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good';
  }
  if (status === 'sell_alert') {
    return 'border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.10)] text-warn';
  }
  if (status === 'dropped' || status === 'expired') {
    return 'border-[rgba(255,123,123,0.28)] bg-[rgba(255,123,123,0.10)] text-bad';
  }
  return 'border-[rgba(65,214,179,0.28)] bg-[rgba(65,214,179,0.10)] text-accent';
}

function statusText(status: string | undefined): string {
  const mapping: Record<string, string> = {
    recommended: '推荐待触发',
    entry_triggered: '已触发买入',
    bought: '已买入',
    holding: '持有中',
    sell_alert: '卖出提醒',
    sold: '已卖出',
    closed: '已结束',
    watching: '观察',
    dropped: '已放弃',
    expired: '已失效',
  };
  return mapping[status || ''] || status || '-';
}

function planEntryRange(plan: TradePlan | undefined): string {
  const range = plan?.entry_range;
  const low = Array.isArray(range) ? range[0] : plan?.entry_low;
  const high = Array.isArray(range) ? range[1] : plan?.entry_high;
  if (!low || !high) {
    return '-';
  }
  return `${formatNumber(low, 2)} - ${formatNumber(high, 2)}`;
}

function planTakeProfit(plan: TradePlan | undefined): string {
  const prices = plan?.take_profit_prices ?? [];
  if (!prices.length) {
    return '-';
  }
  return prices.slice(0, 3).map((price) => formatNumber(price, 2)).join(' / ');
}

function activeReturn(item: RecommendationItem): number {
  if (typeof item.realized_return_pct === 'number' && item.realized_return_pct !== 0) {
    return item.realized_return_pct;
  }
  return item.current_return_pct ?? 0;
}

export default function RecommendationsPage() {
  const { data, error, loading, refresh, lastUpdated } = useAutoRefresh<RecommendationResponse>(
    () => apiGet<RecommendationResponse>('/recommendations/lifecycle?limit=1000'),
    15000,
  );

  const summary = data?.summary ?? {};
  const items = data?.items ?? [];
  const openItems = items.filter((item) => item.is_open_position || ['holding', 'bought', 'sell_alert'].includes(item.status || ''));
  const closedItems = items.filter((item) => ['closed', 'sold'].includes(item.status || ''));

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
            <Target className="h-7 w-7 text-accent" /> 推荐收益汇总
          </h1>
          <p className="mt-2 text-muted">每只推荐票从计划、买入、持有提醒到卖出结束都集中在这里。</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right text-xs text-muted">
            <div>最近刷新：{lastUpdated ? formatDateTime(lastUpdated) : '-'}</div>
            <div>状态：{loading ? '更新中' : '已同步'}</div>
          </div>
          <button className="btn-outline" onClick={() => void refresh()}>
            <RefreshCw className="h-4 w-4" /> 刷新
          </button>
        </div>
      </div>

      {error ? <div className="glass-panel border-warn p-4 text-warn">加载失败：{error}</div> : null}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-4">
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">已结束</div>
          <div className="mt-2 font-mono text-4xl font-bold">{summary.closed_records ?? 0}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">胜率</div>
          <div className="mt-2 font-mono text-4xl font-bold text-good">{formatPercent(summary.win_rate, 1)}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">平均已实现</div>
          <div className="mt-2 font-mono text-4xl font-bold">{formatPercent(summary.avg_realized_return_pct, 2)}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">当前未结束</div>
          <div className="mt-2 font-mono text-4xl font-bold text-accent">{summary.open_records ?? openItems.length}</div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[0.85fr,1.15fr]">
        <div className="glass-panel p-6">
          <h2 className="mb-4 flex items-center gap-2 text-xl font-bold">
            <TrendingUp className="h-5 w-5 text-accent" /> 未结束票
          </h2>
          <div className="space-y-3">
            {openItems.length ? openItems.map((item) => (
              <div key={`${item.symbol}-${item.updated_at}`} className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-mono text-lg font-bold">{item.symbol || '-'}</div>
                  <span className={`rounded-full border px-3 py-1 text-xs ${statusTone(item.status)}`}>{statusText(item.status)}</span>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-3 text-sm text-muted">
                  <div>买入区间：{planEntryRange(item.trade_plan)}</div>
                  <div>止损：{formatNumber(item.trade_plan?.stop_loss_price, 2)}</div>
                  <div>止盈：{planTakeProfit(item.trade_plan)}</div>
                  <div>当前收益：{formatPercent(activeReturn(item), 2)}</div>
                  <div>持有天数：{item.holding_days ?? 0}</div>
                  <div>卖出原因：{item.exit_alert_reason || '-'}</div>
                </div>
              </div>
            )) : <div className="text-muted">暂无未结束推荐。</div>}
          </div>
        </div>

        <div className="glass-panel p-6">
          <h2 className="mb-4 flex items-center gap-2 text-xl font-bold">
            <CheckCircle2 className="h-5 w-5 text-good" /> 推荐明细
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-panelBorder text-left text-muted">
                  <th className="px-3 py-2">代码</th>
                  <th className="px-3 py-2">状态</th>
                  <th className="px-3 py-2">买入区间</th>
                  <th className="px-3 py-2">买入/卖出</th>
                  <th className="px-3 py-2">收益</th>
                  <th className="px-3 py-2">原因</th>
                  <th className="px-3 py-2">更新时间</th>
                </tr>
              </thead>
              <tbody>
                {items.length ? items.map((item) => (
                  <tr key={`${item.symbol}-${item.updated_at}-${item.status}`} className="border-b border-panelBorder/70">
                    <td className="px-3 py-3 font-mono font-bold">{item.symbol || '-'}</td>
                    <td className="px-3 py-3">
                      <span className={`rounded-full border px-2 py-1 text-xs ${statusTone(item.status)}`}>{statusText(item.status)}</span>
                    </td>
                    <td className="px-3 py-3">{planEntryRange(item.trade_plan)}</td>
                    <td className="px-3 py-3">{formatNumber(item.entry_price, 2)} / {formatNumber(item.exit_price, 2)}</td>
                    <td className={`px-3 py-3 font-mono ${activeReturn(item) >= 0 ? 'text-good' : 'text-bad'}`}>{formatPercent(activeReturn(item), 2)}</td>
                    <td className="px-3 py-3">{item.exit_alert_reason || item.closed_reason || '-'}</td>
                    <td className="px-3 py-3 text-muted">{formatDateTime(item.updated_at)}</td>
                  </tr>
                )) : (
                  <tr>
                    <td className="px-3 py-8 text-center text-muted" colSpan={7}>暂无推荐生命周期记录</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {closedItems.length === 0 ? (
        <div className="glass-panel flex items-center gap-3 border-warn p-4 text-warn">
          <AlertTriangle className="h-5 w-5" />
          当前还没有已结束推荐，胜率会在出现第一笔完整买入到卖出记录后开始有统计意义。
        </div>
      ) : null}
    </div>
  );
}
