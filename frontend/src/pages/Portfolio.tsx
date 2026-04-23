import { Briefcase, RefreshCw } from 'lucide-react';

import { apiGet } from '../lib/api';
import { formatDateTime, formatNumber, formatPercent, truncateText } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface HoldingAlertItem {
  symbol?: string;
  severity?: string;
  reason?: string;
  detail?: string;
}

interface PortfolioResponse {
  summary?: {
    current_equity?: number;
    open_positions?: number;
    recent_trades?: number;
  };
  positions_panel?: Array<Record<string, unknown>>;
  holding_alerts?: {
    summary?: {
      warn?: number;
      info?: number;
    };
    items?: HoldingAlertItem[];
  };
  recent_trades?: Array<Record<string, unknown>>;
  execution_quality?: {
    reconcile_alignment_rate?: number;
    latest_reconcile_status?: string;
  };
  reconcile_weekly?: {
    records?: number;
    mismatch_records?: number;
    ok_records?: number;
  };
}

export default function Portfolio() {
  const { data, error, loading, refresh, lastUpdated } = useAutoRefresh<PortfolioResponse>(
    () => apiGet<PortfolioResponse>('/dashboard/portfolio?days=7&trade_limit=30'),
    [],
    15000,
  );

  const positions = data?.positions_panel ?? [];
  const alerts = data?.holding_alerts?.items ?? [];
  const trades = data?.recent_trades ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold font-mono tracking-wide flex items-center gap-3">
            <Briefcase className="w-7 h-7 text-accent" /> 持仓与实盘
          </h1>
          <p className="text-muted mt-2">这里显示后端实时持仓、预警、交易和对账数据。</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right text-xs text-muted">
            <div>最近刷新：{lastUpdated ? formatDateTime(lastUpdated) : '-'}</div>
            <div>状态：{loading ? '更新中' : '已同步'}</div>
          </div>
          <button className="btn-outline" onClick={() => void refresh()}>
            <RefreshCw className="w-4 h-4" /> 刷新
          </button>
        </div>
      </div>

      {error ? <div className="glass-panel p-4 border-warn text-warn">加载失败：{error}</div> : null}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">当前净值</div>
          <div className="text-4xl font-bold mt-2 font-mono">{formatNumber(data?.summary?.current_equity, data?.summary?.current_equity && data.summary.current_equity <= 10 ? 4 : 2)}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">持仓数量</div>
          <div className="text-4xl font-bold mt-2 font-mono">{data?.summary?.open_positions ?? 0}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">最近交易</div>
          <div className="text-4xl font-bold mt-2 font-mono">{data?.summary?.recent_trades ?? 0}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">对账一致率</div>
          <div className="text-4xl font-bold mt-2 font-mono text-good">{formatPercent(data?.execution_quality?.reconcile_alignment_rate, 1)}</div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div className="glass-panel p-6">
          <h2 className="text-xl font-bold mb-4">当前持仓</h2>
          <div className="space-y-3">
            {positions.length ? positions.map((item, index) => (
              <div key={`position-${index}`} className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-sm">
                <div className="font-bold">{truncateText(JSON.stringify(item), 96)}</div>
              </div>
            )) : <div className="text-muted">当前没有持仓。</div>}
          </div>
        </div>

        <div className="glass-panel p-6">
          <h2 className="text-xl font-bold mb-4">持仓预警</h2>
          <div className="space-y-3">
            {alerts.length ? alerts.map((item, index) => (
              <div key={`alert-${item.symbol}-${index}`} className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="font-bold">{item.symbol || '-'}｜{item.severity || 'info'}</div>
                <div className="text-sm mt-2">{item.detail || item.reason || '-'}</div>
              </div>
            )) : <div className="text-muted">当前没有持仓预警。</div>}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div className="glass-panel p-6">
          <h2 className="text-xl font-bold mb-4">最近交易</h2>
          <div className="space-y-3">
            {trades.length ? trades.map((item, index) => (
              <div key={`trade-${index}`} className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-sm">
                <div>{truncateText(JSON.stringify(item), 96)}</div>
              </div>
            )) : <div className="text-muted">暂无最近交易。</div>}
          </div>
        </div>

        <div className="glass-panel p-6 space-y-3">
          <h2 className="text-xl font-bold mb-4">对账概览</h2>
          <div>最新状态：{data?.execution_quality?.latest_reconcile_status || '-'}</div>
          <div>近 7 日对账记录：{data?.reconcile_weekly?.records ?? 0}</div>
          <div>近 7 日异常次数：{data?.reconcile_weekly?.mismatch_records ?? 0}</div>
          <div>近 7 日正常次数：{data?.reconcile_weekly?.ok_records ?? 0}</div>
        </div>
      </div>
    </div>
  );
}
