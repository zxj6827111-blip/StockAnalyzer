import { Activity, AlertTriangle, RefreshCw, ShieldAlert, Waves } from 'lucide-react';

import { apiGet } from '../lib/api';
import { asNumber, formatDateTime, formatNumber, formatPercent, truncateText } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface HealthResponse {
  runtime?: {
    state?: {
      current_equity?: number;
      pause_new_buy?: boolean;
    };
    portfolio?: {
      open_positions?: number;
      recent_trades?: number;
    };
    reconcile?: {
      last_report?: {
        status?: string;
      };
    };
  };
}

interface DashboardPortfolioResponse {
  summary?: {
    current_equity?: number;
    open_positions?: number;
    recent_trades?: number;
  };
  execution_quality?: {
    reconcile_alignment_rate?: number;
    latest_reconcile_status?: string;
  };
  recent_events?: AuditEvent[];
}

interface Week5Candidate {
  symbol?: string;
  board_stage?: string;
  score?: number;
  leader_score?: number;
  action?: string;
  isolated?: boolean;
  isolation_reason?: string;
}

interface Week5ReportResponse {
  report?: {
    timestamp?: string;
    empty_signal?: {
      triggered?: boolean;
    };
    first_board?: {
      candidates?: Week5Candidate[];
      leaders?: Week5Candidate[];
    };
    summary?: {
      first_board_candidates?: number;
      leaders?: number;
      anomalies?: number;
    };
  };
}

interface NewsBriefItem {
  symbol?: string;
  title?: string;
  source?: string;
  published_at?: string;
}

interface NewsBriefResponse {
  phase_label?: string;
  records?: number;
  real_news_available?: boolean;
  items?: NewsBriefItem[];
}

interface OpsStateResponse {
  execution_mode?: string;
  advisory_only?: boolean;
  enabled?: boolean;
}

interface AuditEvent {
  event_id?: string;
  timestamp?: string;
  event_type?: string;
  level?: string;
  payload?: {
    title?: string;
    content?: string;
  };
}

function boardStageLabel(value: string | undefined): string {
  if (!value) {
    return '-';
  }
  if (value === 'first_board') {
    return '首板';
  }
  if (value.endsWith('_board')) {
    const prefix = value.replace('_board', '');
    if (/^\d+$/.test(prefix)) {
      return `${prefix}板`;
    }
  }
  return value;
}

function candidateText(item: Week5Candidate | undefined, mode: 'candidate' | 'leader'): string {
  if (!item?.symbol) {
    return '暂无';
  }
  if (mode === 'leader') {
    return `${item.symbol}｜龙头分 ${formatNumber(item.leader_score, 1)}`;
  }
  return `${item.symbol}｜${boardStageLabel(item.board_stage)}｜评分 ${formatNumber(item.score, 1)}`;
}

function sectionTimeLabel(primaryTime: string | undefined, fallbackTime: string): string {
  return primaryTime ? formatDateTime(primaryTime) : fallbackTime ? formatDateTime(fallbackTime) : '-';
}

function reconcileText(status: string | undefined): string {
  switch (status) {
    case 'matched':
      return '已一致';
    case 'warning':
      return '待复核';
    case 'mismatch':
      return '不一致';
    default:
      return status || '未知';
  }
}

function opsModeText(ops: OpsStateResponse | null): string {
  if (ops?.advisory_only) {
    return '仅建议';
  }
  return ops?.enabled ? '允许执行' : '暂停执行';
}

function toneClass(tone: 'good' | 'warn' | 'accent' | 'bad' | 'muted'): string {
  if (tone === 'good') {
    return 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good';
  }
  if (tone === 'warn') {
    return 'border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.10)] text-warn';
  }
  if (tone === 'bad') {
    return 'border-[rgba(255,123,123,0.28)] bg-[rgba(255,123,123,0.10)] text-bad';
  }
  if (tone === 'accent') {
    return 'border-[rgba(65,214,179,0.28)] bg-[rgba(65,214,179,0.10)] text-accent';
  }
  return 'border-panelBorder bg-[rgba(12,33,48,0.45)] text-muted';
}

export default function Dashboard() {
  const healthState = useAutoRefresh<HealthResponse>(() => apiGet<HealthResponse>('/health'), [], 15000);
  const portfolioState = useAutoRefresh<DashboardPortfolioResponse>(
    () => apiGet<DashboardPortfolioResponse>('/dashboard/portfolio?days=7&trade_limit=20'),
    [],
    20000,
  );
  const week5State = useAutoRefresh<Week5ReportResponse>(() => apiGet<Week5ReportResponse>('/week5/scan/latest'), [], 15000);
  const newsState = useAutoRefresh<NewsBriefResponse>(
    () => apiGet<NewsBriefResponse>('/news/briefing/latest?phase=premarket&limit=5'),
    [],
    60000,
  );
  const opsState = useAutoRefresh<OpsStateResponse>(() => apiGet<OpsStateResponse>('/dashboard/ops/state'), [], 15000);

  const equity =
    portfolioState.data?.summary?.current_equity ??
    healthState.data?.runtime?.state?.current_equity ??
    0;
  const openPositions =
    portfolioState.data?.summary?.open_positions ??
    healthState.data?.runtime?.portfolio?.open_positions ??
    0;
  const recentTrades =
    portfolioState.data?.summary?.recent_trades ??
    healthState.data?.runtime?.portfolio?.recent_trades ??
    0;
  const alignmentRate = portfolioState.data?.execution_quality?.reconcile_alignment_rate ?? 0;
  const reconcileStatus = reconcileText(portfolioState.data?.execution_quality?.latest_reconcile_status);
  const week5 = week5State.data?.report;
  const candidates = week5?.first_board?.candidates ?? [];
  const leaders = week5?.first_board?.leaders ?? [];
  const emptySignal = Boolean(week5?.empty_signal?.triggered);
  const opsMode = opsModeText(opsState.data ?? null);
  const newsItems = newsState.data?.items ?? [];
  const recentEvents = portfolioState.data?.recent_events ?? [];

  const bootstrapLoading =
    !healthState.data &&
    !portfolioState.data &&
    !week5State.data &&
    !newsState.data &&
    !opsState.data &&
    (healthState.loading || portfolioState.loading || week5State.loading || newsState.loading || opsState.loading);

  const partialErrors = [
    healthState.error ? '健康检查' : '',
    portfolioState.error ? '持仓对账' : '',
    week5State.error ? '观察池扫描' : '',
    newsState.error ? '新闻摘要' : '',
    opsState.error ? '执行状态' : '',
  ].filter(Boolean);

  async function refreshAll(): Promise<void> {
    await Promise.all([
      healthState.refresh(),
      portfolioState.refresh(),
      week5State.refresh(),
      newsState.refresh(),
      opsState.refresh(),
    ]);
  }

  return (
    <div className="space-y-6">
      <div className="glass-panel relative overflow-hidden p-6 md:p-7 bg-gradient-to-br from-[rgba(10,34,53,0.98)] via-[rgba(8,27,44,0.97)] to-[rgba(5,18,31,0.98)] border-[rgba(65,214,179,0.22)] shadow-[0_0_42px_rgba(65,214,179,0.08)]">
        <div className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full bg-[rgba(65,214,179,0.12)] blur-3xl" />
        <div className="pointer-events-none absolute -left-8 bottom-0 h-32 w-32 rounded-full bg-[rgba(61,123,255,0.10)] blur-3xl" />

        <div className="relative grid grid-cols-1 xl:grid-cols-[1.2fr,0.8fr] gap-6">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full border border-[rgba(65,214,179,0.25)] bg-[rgba(65,214,179,0.08)] px-3 py-1 text-xs tracking-[0.28em] text-accent">
              <Activity className="w-3.5 h-3.5" />
              REALTIME CONTROL DECK
            </div>
            <h1 className="mt-4 text-3xl font-bold font-mono tracking-wide">核心控制台</h1>
            <p className="mt-3 max-w-3xl text-sm leading-7 text-muted">
              扫描结果、持仓状态、运维开关、新闻摘要现在分块刷新；即使某个慢接口还在补载，也不会把整个大屏拖成“持续加载中”。
            </p>
            <div className="mt-5 flex flex-wrap gap-3 text-xs text-muted">
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                扫描时间：{sectionTimeLabel(week5?.timestamp, week5State.lastUpdated)}
              </div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                持仓刷新：{sectionTimeLabel(undefined, portfolioState.lastUpdated)}
              </div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                新闻刷新：{sectionTimeLabel(undefined, newsState.lastUpdated)}
              </div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                运维刷新：{sectionTimeLabel(undefined, opsState.lastUpdated)}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-2xl border border-[rgba(65,214,179,0.22)] bg-[rgba(8,25,39,0.86)] p-4 shadow-[0_0_18px_rgba(65,214,179,0.05)]">
              <div className="text-xs tracking-wider text-muted">执行模式</div>
              <div className="mt-2 text-xl font-bold text-accent">{opsMode}</div>
              <div className="mt-2 text-xs text-muted">{opsState.loading ? '运维状态同步中…' : '运行开关已接入实时状态'}</div>
            </div>
            <div className="rounded-2xl border border-[rgba(255,184,77,0.22)] bg-[rgba(8,25,39,0.86)] p-4 shadow-[0_0_18px_rgba(255,184,77,0.05)]">
              <div className="text-xs tracking-wider text-muted">新买入开关</div>
              <div className={`mt-2 text-xl font-bold ${healthState.data?.runtime?.state?.pause_new_buy ? 'text-warn' : 'text-good'}`}>
                {healthState.data?.runtime?.state?.pause_new_buy ? '暂停新买入' : '允许新买入'}
              </div>
              <div className="mt-2 text-xs text-muted">{healthState.loading ? '健康状态同步中…' : '风险门实时接入'}</div>
            </div>
            <div className="rounded-2xl border border-[rgba(61,123,255,0.22)] bg-[rgba(8,25,39,0.86)] p-4">
              <div className="text-xs tracking-wider text-muted">对账状态</div>
              <div className="mt-2 text-xl font-bold text-ink">{reconcileStatus}</div>
              <div className="mt-2 text-xs text-muted">一致率 {formatPercent(alignmentRate, 1)}</div>
            </div>
            <div className="rounded-2xl border border-[rgba(255,123,123,0.22)] bg-[rgba(8,25,39,0.86)] p-4">
              <div className="text-xs tracking-wider text-muted">新闻状态</div>
              <div className={`mt-2 text-xl font-bold ${newsState.data?.real_news_available ? 'text-accent' : 'text-warn'}`}>
                {newsState.data?.real_news_available ? '已抓到实盘标题' : '待补抓标题'}
              </div>
              <div className="mt-2 text-xs text-muted">当前 {newsItems.length} 条新闻摘要</div>
            </div>
          </div>
        </div>
      </div>

      {partialErrors.length ? (
        <div className="glass-panel border-warn p-4 text-sm text-warn">
          部分模块正在补载：{partialErrors.join('、')}。其余已成功加载的数据仍会正常显示。
        </div>
      ) : null}
      {bootstrapLoading ? <div className="glass-panel p-4 text-muted">控制台数据初始化中…</div> : null}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div className="glass-panel p-6 bg-gradient-to-br from-[rgba(13,51,76,0.96)] to-[rgba(7,23,36,0.92)]">
          <div className="text-sm font-bold tracking-wider text-muted">当前净值</div>
          <div className="mt-3 text-4xl font-bold font-mono">{formatNumber(equity, equity <= 10 ? 4 : 2)}</div>
          <div className="mt-2 text-xs text-muted">{portfolioState.loading ? '持仓模块刷新中…' : '来自持仓/健康状态'}</div>
        </div>
        <div className="glass-panel p-6 bg-gradient-to-br from-[rgba(13,51,76,0.96)] to-[rgba(7,23,36,0.92)]">
          <div className="text-sm font-bold tracking-wider text-muted">持仓数量</div>
          <div className="mt-3 text-4xl font-bold font-mono">{openPositions}</div>
          <div className="mt-2 text-xs text-muted">近 7 日成交 {recentTrades}</div>
        </div>
        <div className="glass-panel p-6 border-good bg-gradient-to-br from-[rgba(12,46,34,0.96)] to-[rgba(7,24,18,0.92)] shadow-[0_0_26px_rgba(77,223,126,0.08)]">
          <div className="text-sm font-bold tracking-wider text-muted">对账一致率</div>
          <div className="mt-3 text-4xl font-bold font-mono text-good">{formatPercent(alignmentRate, 1)}</div>
          <div className="mt-2 text-xs text-muted">状态：{reconcileStatus}</div>
        </div>
        <div className="glass-panel p-6 border-accent bg-gradient-to-br from-[rgba(9,43,52,0.96)] to-[rgba(6,23,29,0.92)] shadow-[0_0_26px_rgba(65,214,179,0.08)]">
          <div className="text-sm font-bold tracking-wider text-muted">扫描结论</div>
          <div className={`mt-3 text-2xl font-bold ${emptySignal ? 'text-warn' : 'text-accent'}`}>
            {emptySignal ? '仅观察' : '继续盯盘'}
          </div>
          <div className="mt-2 text-xs text-muted">
            首板 {asNumber(week5?.summary?.first_board_candidates)} / 龙头 {asNumber(week5?.summary?.leaders)} / 异常 {asNumber(week5?.summary?.anomalies)}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[1.15fr,0.85fr] gap-6">
        <div className="glass-panel p-6 bg-gradient-to-br from-[rgba(10,31,48,0.97)] to-[rgba(6,22,36,0.95)]">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-2 text-xl font-bold">
              <Activity className="w-5 h-5 text-accent" /> 当前扫描结论
            </div>
            <div className="text-right text-xs text-muted">
              <div>{week5State.loading ? '扫描结果刷新中…' : '扫描结果已同步'}</div>
              <div className="mt-1">{sectionTimeLabel(week5?.timestamp, week5State.lastUpdated)}</div>
            </div>
          </div>

          {week5State.error ? (
            <div className="mt-4 rounded-2xl border border-warn bg-[rgba(255,184,77,0.08)] p-4 text-sm text-warn">
              扫描结果暂时无法更新：{week5State.error}
            </div>
          ) : null}

          <div className={`mt-5 rounded-2xl border p-5 ${emptySignal ? toneClass('warn') : toneClass('accent')}`}>
            <div className="text-xs tracking-[0.24em]">TODAY STATUS</div>
            <div className="mt-3 text-2xl font-bold">{emptySignal ? '仅观察，暂不买入' : '有重点目标，继续盯盘'}</div>
            <div className="mt-3 text-sm leading-7 text-muted">
              首板候选代表当天首次涨停且值得观察的标的；龙头候选代表这些标的里更强、更值得优先盯盘的前排目标。
              真正可以考虑开仓，仍以独立的“买入候选/买入信号”推送为准。
            </div>
          </div>

          <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
              <div className="flex items-center justify-between gap-3">
                <div className="font-bold text-muted">首板候选</div>
                <div className="text-xs text-muted">{candidates.length} 只</div>
              </div>
              <div className="mt-3 space-y-3">
                {candidates.length ? candidates.slice(0, 4).map((item) => (
                  <div key={`candidate-${item.symbol}`} className="rounded-xl border border-panelBorder bg-[rgba(7,24,36,0.55)] p-3">
                    <div className="font-mono font-bold text-ink">{item.symbol}</div>
                    <div className="mt-1 text-sm text-muted">{candidateText(item, 'candidate')}</div>
                  </div>
                )) : (
                  <div className="text-sm text-muted">{week5State.loading ? '扫描补载中…' : '暂无首板候选。'}</div>
                )}
              </div>
            </div>

            <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
              <div className="flex items-center justify-between gap-3">
                <div className="font-bold text-muted">龙头候选</div>
                <div className="text-xs text-muted">{leaders.length} 只</div>
              </div>
              <div className="mt-3 space-y-3">
                {leaders.length ? leaders.slice(0, 4).map((item) => (
                  <div key={`leader-${item.symbol}`} className="rounded-xl border border-panelBorder bg-[rgba(7,24,36,0.55)] p-3">
                    <div className="font-mono font-bold text-ink">{item.symbol}</div>
                    <div className="mt-1 text-sm text-muted">{candidateText(item, 'leader')}</div>
                  </div>
                )) : (
                  <div className="text-sm text-muted">{week5State.loading ? '扫描补载中…' : '暂无龙头候选。'}</div>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="glass-panel p-6 bg-gradient-to-br from-[rgba(8,30,48,0.97)] to-[rgba(5,20,34,0.95)] shadow-[0_0_28px_rgba(65,214,179,0.06)]">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-2 text-xl font-bold">
              <Waves className="w-5 h-5 text-accent" /> 盘前重点新闻
            </div>
            <div className="text-right text-xs text-muted">
              <div>{newsState.loading ? '新闻摘要补载中…' : '新闻摘要已同步'}</div>
              <div className="mt-1">{sectionTimeLabel(undefined, newsState.lastUpdated)}</div>
            </div>
          </div>

          {!newsState.data?.real_news_available ? (
            <div className="mt-4 rounded-2xl border border-warn bg-[rgba(255,184,77,0.08)] p-4 text-sm text-warn">
              当前还没有抓到可用的实时个股新闻标题，页面会如实显示，不再用代理分数冒充新闻内容。
            </div>
          ) : null}
          {newsState.error ? (
            <div className="mt-4 rounded-2xl border border-warn bg-[rgba(255,184,77,0.08)] p-4 text-sm text-warn">
              新闻摘要更新失败：{newsState.error}
            </div>
          ) : null}

          <div className="mt-4 space-y-3">
            {newsItems.length ? newsItems.map((item, index) => (
              <div
                key={`${item.symbol}-${index}`}
                className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 hover:border-accent transition-colors"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="rounded-full border border-[rgba(65,214,179,0.22)] bg-[rgba(65,214,179,0.08)] px-2.5 py-1 text-xs font-mono text-accent">
                    {item.symbol || 'NEWS'}
                  </div>
                  <div className="text-xs text-muted">{formatDateTime(item.published_at)}</div>
                </div>
                <div className="mt-3 text-sm leading-7 text-ink">{truncateText(item.title, 58)}</div>
                <div className="mt-3 text-xs text-muted">来源：{item.source || '未知来源'}</div>
              </div>
            )) : (
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-sm text-muted">
                {newsState.loading ? '正在整理新闻标题…' : '暂无实时新闻标题。'}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[0.8fr,1.2fr] gap-6">
        <div className="glass-panel p-6">
          <div className="flex items-center gap-2 text-xl font-bold mb-4">
            <ShieldAlert className="w-5 h-5 text-warn" /> 运行状态
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
            <div className={`rounded-2xl border p-4 ${toneClass(opsState.data?.enabled ? 'accent' : 'warn')}`}>
              <div className="text-xs text-muted">执行权限</div>
              <div className="mt-2 font-bold text-base">{opsState.data?.enabled ? '已开启' : '已关闭'}</div>
            </div>
            <div className={`rounded-2xl border p-4 ${toneClass(healthState.data?.runtime?.state?.pause_new_buy ? 'warn' : 'good')}`}>
              <div className="text-xs text-muted">暂停新买入</div>
              <div className="mt-2 font-bold text-base">{healthState.data?.runtime?.state?.pause_new_buy ? '是' : '否'}</div>
            </div>
            <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
              <div className="text-xs text-muted">空信号触发</div>
              <div className="mt-2 font-bold text-base">{emptySignal ? '是' : '否'}</div>
            </div>
            <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
              <div className="text-xs text-muted">异常数量</div>
              <div className="mt-2 font-bold text-base">{asNumber(week5?.summary?.anomalies)}</div>
            </div>
            <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
              <div className="text-xs text-muted">首板数量</div>
              <div className="mt-2 font-bold text-base">{asNumber(week5?.summary?.first_board_candidates)}</div>
            </div>
            <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
              <div className="text-xs text-muted">龙头数量</div>
              <div className="mt-2 font-bold text-base">{asNumber(week5?.summary?.leaders)}</div>
            </div>
          </div>
        </div>

        <div className="glass-panel p-6">
          <div className="flex items-start justify-between gap-4 mb-4">
            <div className="flex items-center gap-2 text-xl font-bold">
              <AlertTriangle className="w-5 h-5 text-accent" /> 最近事件
            </div>
            <button className="btn-outline" onClick={() => void refreshAll()}>
              <RefreshCw className="w-4 h-4" /> 刷新全部
            </button>
          </div>

          {portfolioState.error ? (
            <div className="rounded-2xl border border-warn bg-[rgba(255,184,77,0.08)] p-4 text-sm text-warn">
              最近事件读取失败：{portfolioState.error}
            </div>
          ) : null}

          <div className="space-y-3 max-h-[360px] overflow-y-auto">
            {recentEvents.length ? recentEvents.slice().reverse().slice(0, 10).map((item) => (
              <div key={item.event_id} className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-bold text-sm">{item.event_type || 'unknown'}</div>
                  <div className={`text-xs ${item.level === 'warn' ? 'text-warn' : 'text-muted'}`}>{item.level || 'info'}</div>
                </div>
                <div className="mt-1 text-xs text-muted">{formatDateTime(item.timestamp)}</div>
                <div className="mt-3 text-sm leading-7">
                  {truncateText(item.payload?.title ?? item.payload?.content ?? '-', 78)}
                </div>
              </div>
            )) : (
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-sm text-muted">
                {portfolioState.loading ? '正在同步最近事件…' : '暂无最近事件。'}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
