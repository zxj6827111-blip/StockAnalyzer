import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Eye, RefreshCw, Search, TrendingDown, TrendingUp, Waves } from 'lucide-react';
import { useLayoutEffect } from 'react';

import { apiGet } from '../lib/api';
import { asNumber, cleanDisplayText, formatDateTime, formatNumber, formatPercent } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

const LIVE_LIMIT = 50;
const PREFETCH_COUNT = 8;
const CACHE_TTL_MS = 8_000;

type Tone = 'accent' | 'bad' | 'good' | 'muted' | 'warn';

interface Candidate {
  symbol?: string;
  score?: number;
  leader_score?: number;
  action?: string;
  action_label?: string;
  suggested_position?: number;
  isolated?: boolean;
  isolation_reason?: string;
  board_stage?: string;
  reasons?: string[];
  reason_summary?: string;
}

interface AnomalyEvent {
  symbol?: string;
  types?: string[];
  gap_pct?: number;
  volume_ratio_5d?: number;
}

interface Report {
  timestamp?: string;
  watchlist_size?: number;
  scan_profile?: string;
  runtime_source?: { mode?: string; provider?: string };
  prefilter?: { lookback_days?: number; top_k?: number };
  first_board?: { candidates?: Candidate[]; leaders?: Candidate[] };
  signal_pool?: { candidate_count?: number; candidates?: Candidate[] };
  anomalies?: { event_count?: number; events?: AnomalyEvent[] };
  empty_signal?: { triggered?: boolean; no_buy_streak?: number };
  summary?: { first_board_candidates?: number; leaders?: number; anomalies?: number };
  watchlist_sync?: { enabled?: boolean; updated?: boolean; symbols?: string[] };
}

interface ScanLatestResponse {
  report?: Report;
}

interface DepthLevel {
  level?: number;
  price?: number;
  volume?: number;
}

interface LiveItem extends Candidate {
  name?: string;
  last_price?: number;
  prev_close?: number;
  change_pct?: number;
  change_amount?: number;
  day_high?: number;
  day_low?: number;
  volume?: number;
  turnover?: number;
  latest_time?: string;
  trend_source?: string;
  trend_label?: string;
  trend_points?: number[];
  trend_change_pct?: number;
  depth_available?: boolean;
  depth_source?: string;
  spread?: number;
  order_imbalance?: number;
  bid_levels?: DepthLevel[];
  ask_levels?: DepthLevel[];
}

interface LiveResponse {
  generated_at?: string;
  records?: number;
  report_timestamp?: string;
  items?: LiveItem[];
  source_breakdown?: { intraday_1m?: number; intraday_5m?: number; daily?: number };
}

interface SymbolLiveResponse {
  item?: LiveItem;
}

interface Bundle {
  report: Report | null;
  live: LiveResponse | null;
}

interface CacheEntry {
  item: LiveItem;
  updatedAt: number;
}

function toneClass(tone: Tone): string {
  if (tone === 'accent') return 'border-[rgba(65,214,179,0.26)] bg-[rgba(65,214,179,0.10)]';
  if (tone === 'bad') return 'border-[rgba(255,123,123,0.26)] bg-[rgba(255,123,123,0.10)]';
  if (tone === 'good') return 'border-[rgba(77,223,126,0.26)] bg-[rgba(77,223,126,0.10)]';
  if (tone === 'warn') return 'border-[rgba(255,184,77,0.26)] bg-[rgba(255,184,77,0.10)]';
  return 'border-panelBorder bg-[rgba(12,33,48,0.52)]';
}

function actionClass(action: string | undefined): string {
  const normalized = (action ?? '').trim().toLowerCase();
  if (normalized === 'buy') return 'border-[rgba(255,123,123,0.28)] bg-[rgba(255,123,123,0.12)] text-bad';
  if (normalized === 'watch') return 'border-[rgba(65,214,179,0.28)] bg-[rgba(65,214,179,0.12)] text-accent';
  return 'border-panelBorder bg-[rgba(12,33,48,0.48)] text-muted';
}

function priceClass(changePct: number): string {
  if (changePct > 0) return 'text-bad';
  if (changePct < 0) return 'text-good';
  return 'text-ink';
}

function priceChipClass(changePct: number): string {
  if (changePct > 0) return 'border-[rgba(255,123,123,0.26)] bg-[rgba(255,123,123,0.12)] text-bad';
  if (changePct < 0) return 'border-[rgba(77,223,126,0.26)] bg-[rgba(77,223,126,0.12)] text-good';
  return 'border-panelBorder bg-[rgba(12,33,48,0.48)] text-muted';
}

function boardStageLabel(value: string | undefined): string {
  const normalized = (value ?? '').trim();
  if (!normalized) return '普通观察';
  if (normalized === 'first_board') return '首板';
  const matched = normalized.match(/^(\d+)_board$/);
  return matched ? `${matched[1]}板` : normalized;
}
function reasonLabel(reason: string | undefined): string {
  const normalized = (reason ?? '').trim();
  if (!normalized) return '-';
  if (normalized === 'liquidity_failed') return '流动性不足';
  if (normalized === 'cross_review') return '交叉复核未通过';
  if (normalized.startsWith('financial_penalty:low_roe')) return 'ROE 偏低';
  if (normalized.startsWith('financial_penalty:high_debt_ratio')) return '负债率偏高';
  if (normalized.startsWith('news_component:')) return `新闻因子 ${normalized.split(':', 2)[1] ?? '-'}`;
  if (normalized.startsWith('lgbm<')) return `LGBM 低于阈值 ${normalized.slice(5)}`;
  if (normalized.startsWith('xgb<')) return `XGB 低于阈值 ${normalized.slice(4)}`;
  if (normalized.startsWith('meta<')) return `Meta 低于阈值 ${normalized.slice(5)}`;
  if (normalized.startsWith('model_diff>')) return `模型分歧过大 ${normalized.slice(11)}`;
  return normalized;
}

function anomalyLabel(value: string | undefined): string {
  const normalized = (value ?? '').trim();
  if (normalized === 'gap') return '跳空异常';
  if (normalized === 'volume_spike') return '放量异常';
  return normalized || '-';
}

function compactAmount(value: unknown): string {
  const numeric = asNumber(value);
  if (Math.abs(numeric) >= 100000000) return `${formatNumber(numeric / 100000000, 2)}亿`;
  if (Math.abs(numeric) >= 10000) return `${formatNumber(numeric / 10000, 2)}万`;
  return formatNumber(numeric, 0);
}

function signedPercent(value: unknown, digits = 2): string {
  const numeric = asNumber(value);
  const formatted = formatPercent(Math.abs(numeric), digits);
  if (numeric > 0) return `+${formatted}`;
  if (numeric < 0) return `-${formatted}`;
  return formatted;
}

function signedNumber(value: unknown, digits = 2): string {
  const numeric = asNumber(value);
  const formatted = formatNumber(Math.abs(numeric), digits);
  if (numeric > 0) return `+${formatted}`;
  if (numeric < 0) return `-${formatted}`;
  return formatted;
}

function trendPoints(values: number[] | undefined): number[] {
  return (values ?? []).filter((value) => Number.isFinite(value));
}

function sparklinePath(points: number[]): string {
  if (points.length < 2) return '';
  const minValue = Math.min(...points);
  const maxValue = Math.max(...points);
  const range = maxValue - minValue || 1;
  return points
    .map((point, index) => {
      const x = (index / (points.length - 1)) * 100;
      const y = 100 - ((point - minValue) / range) * 100;
      return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(' ');
}

function DepthPanel(props: { side: 'ask' | 'bid'; title: string; levels: DepthLevel[] }) {
  const { side, title, levels } = props;
  const panelClass = side === 'bid'
    ? 'border-[rgba(255,123,123,0.24)] bg-[rgba(255,123,123,0.08)] text-bad'
    : 'border-[rgba(77,223,126,0.24)] bg-[rgba(77,223,126,0.08)] text-good';
  return (
    <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.84)] p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="font-semibold text-ink">{title}</div>
        <div className={`rounded-full border px-2 py-1 text-[11px] ${panelClass}`}>{side === 'bid' ? '买盘' : '卖盘'}</div>
      </div>
      <div className="space-y-2">
        {levels.length ? levels.map((level) => (
          <div key={`${side}-${level.level ?? 0}-${level.price ?? 0}`} className={`flex items-center justify-between rounded-xl border px-3 py-2 text-sm ${panelClass}`}>
            <div>{side === 'bid' ? '买' : '卖'}{level.level ?? '-'}</div>
            <div className="font-mono">{formatNumber(level.price, 2)}</div>
            <div className="font-mono">{compactAmount(level.volume)}</div>
          </div>
        )) : <div className="text-sm text-muted">盘口数据暂未返回。</div>}
      </div>
    </div>
  );
}

export default function ObservationPoolPage() {
  const { data, error, loading, lastUpdated, refresh } = useAutoRefresh<Bundle>(
    async () => {
      const [scanResponse, liveResponse] = await Promise.all([
        apiGet<ScanLatestResponse>('/week5/scan/latest'),
        apiGet<LiveResponse>(`/week5/signal-pool/live?limit=${LIVE_LIMIT}`),
      ]);
      return { report: scanResponse.report ?? null, live: liveResponse ?? null };
    },
    [],
    15000,
  );

  const [filterText, setFilterText] = useState('');
  const [selectedSymbol, setSelectedSymbol] = useState('');
  const [detailCache, setDetailCache] = useState<Record<string, CacheEntry>>({});
  const [detailLoadingSymbol, setDetailLoadingSymbol] = useState('');
  const [detailError, setDetailError] = useState('');
  const [desktopListHeight, setDesktopListHeight] = useState<number | null>(null);

  const detailCacheRef = useRef<Record<string, CacheEntry>>({});
  const inflightRef = useRef<Partial<Record<string, Promise<void>>>>({});
  const detailSectionRef = useRef<HTMLDivElement | null>(null);
  const detailPanelRef = useRef<HTMLDivElement | null>(null);
  const report = data?.report;
  const live = data?.live;
  const liveItems = useMemo(() => live?.items ?? [], [live?.items]);
  const anomalies = useMemo(() => report?.anomalies?.events ?? [], [report?.anomalies?.events]);
  const anomalyMap = useMemo(() => {
    const next = new Map<string, AnomalyEvent>();
    for (const item of anomalies) {
      const symbol = (item.symbol ?? '').trim();
      if (symbol) next.set(symbol, item);
    }
    return next;
  }, [anomalies]);

  const seedCache = useCallback((items: LiveItem[], updatedAt = Date.now()) => {
    if (!items.length) return;
    setDetailCache((previous) => {
      const next = { ...previous };
      let changed = false;
      for (const item of items) {
        const symbol = (item.symbol ?? '').trim();
        if (!symbol) continue;
        next[symbol] = { item: previous[symbol] ? { ...previous[symbol].item, ...item } : item, updatedAt };
        changed = true;
      }
      return changed ? next : previous;
    });
  }, []);

  useEffect(() => {
    detailCacheRef.current = detailCache;
  }, [detailCache]);

  useEffect(() => {
    if (liveItems.length) seedCache(liveItems, Date.now());
  }, [liveItems, seedCache]);

  const filteredPool = useMemo(() => {
    const keyword = filterText.trim().toLowerCase();
    if (!keyword) return liveItems;
    return liveItems.filter((item) => {
      const symbol = (item.symbol ?? '').toLowerCase();
      const name = cleanDisplayText(item.name).toLowerCase();
      const summary = (item.reason_summary ?? '').toLowerCase();
      return symbol.includes(keyword) || name.includes(keyword) || summary.includes(keyword);
    });
  }, [filterText, liveItems]);

  useEffect(() => {
    if (!filteredPool.length) {
      if (selectedSymbol) setSelectedSymbol('');
      return;
    }
    if (!filteredPool.some((item) => (item.symbol ?? '').trim() === selectedSymbol)) {
      const nextSymbol = (filteredPool[0]?.symbol ?? '').trim();
      if (nextSymbol) setSelectedSymbol(nextSymbol);
    }
  }, [filteredPool, selectedSymbol]);

  const loadSymbolDetail = useCallback(async (symbol: string, force = false, background = true) => {
    const normalized = symbol.trim();
    if (!normalized) return;
    const cached = detailCacheRef.current[normalized];
    const fresh = cached ? Date.now() - cached.updatedAt < CACHE_TTL_MS : false;
    if (fresh && !force) return;
    if (inflightRef.current[normalized]) {
      await inflightRef.current[normalized];
      return;
    }
    const showLoading = !background || normalized === selectedSymbol;
    const request = (async () => {
      if (showLoading) setDetailLoadingSymbol(normalized);
      try {
        const response = await apiGet<SymbolLiveResponse>(`/week5/signal-pool/symbol/live?symbol=${encodeURIComponent(normalized)}${force ? '&force_refresh=true' : ''}`);
        if (response.item) seedCache([response.item], Date.now());
        if (normalized === selectedSymbol) setDetailError('');
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : String(fetchError);
        if (normalized === selectedSymbol) setDetailError(message);
      } finally {
        delete inflightRef.current[normalized];
        if (showLoading) setDetailLoadingSymbol((current) => current === normalized ? '' : current);
      }
    })();
    inflightRef.current[normalized] = request;
    await request;
  }, [seedCache, selectedSymbol]);

  useEffect(() => {
    setDetailError('');
    if (selectedSymbol) void loadSymbolDetail(selectedSymbol, false, true);
  }, [loadSymbolDetail, selectedSymbol]);

  useEffect(() => {
    const symbols = liveItems.slice(0, PREFETCH_COUNT).flatMap((item) => {
      const symbol = (item.symbol ?? '').trim();
      return symbol ? [symbol] : [];
    });
    for (const symbol of symbols) void loadSymbolDetail(symbol, false, true);
  }, [liveItems, loadSymbolDetail]);

  const handleSelect = useCallback((symbol: string) => {
    const normalized = symbol.trim();
    if (!normalized) return;
    setSelectedSymbol(normalized);
    void loadSymbolDetail(normalized, false, true);
  }, [loadSymbolDetail]);

  const selectedFallback = useMemo(() => {
    const current = filteredPool.find((item) => (item.symbol ?? '').trim() === selectedSymbol);
    if (current) return current;
    const fromAll = liveItems.find((item) => (item.symbol ?? '').trim() === selectedSymbol);
    return fromAll ?? filteredPool[0] ?? liveItems[0] ?? null;
  }, [filteredPool, liveItems, selectedSymbol]);

  const selected = useMemo(() => {
    const cached = selectedSymbol ? detailCache[selectedSymbol]?.item : undefined;
    if (cached && selectedFallback) {
      return { ...selectedFallback, ...cached, reasons: cached.reasons?.length ? cached.reasons : selectedFallback.reasons } as LiveItem;
    }
    return cached ?? selectedFallback;
  }, [detailCache, selectedFallback, selectedSymbol]);

  const selectedAnomaly = useMemo(() => {
    const symbol = (selected?.symbol ?? '').trim();
    return symbol ? anomalyMap.get(symbol) ?? null : null;
  }, [anomalyMap, selected]);

  const watchlistSymbols = useMemo(() => {
    const fromReport = (report?.watchlist_sync?.symbols ?? []).flatMap((value) => {
      const symbol = (value ?? '').trim();
      return symbol ? [symbol] : [];
    });
    return fromReport.length ? fromReport : liveItems.flatMap((item) => {
      const symbol = (item.symbol ?? '').trim();
      return symbol ? [symbol] : [];
    });
  }, [liveItems, report?.watchlist_sync?.symbols]);

  const trend = trendPoints(selected?.trend_points);
  const trendPath = useMemo(() => sparklinePath(trend), [trend]);
  const changePct = asNumber(selected?.change_pct, 0);
  const TrendIcon = changePct >= 0 ? TrendingUp : TrendingDown;
  const summaryCards = [
    { title: '观察池数量', value: `${asNumber(report?.watchlist_size ?? report?.signal_pool?.candidate_count ?? liveItems.length, 0)}`, hint: `实时覆盖 ${asNumber(live?.records, 0)} 只`, tone: 'accent' as Tone },
    { title: '实时覆盖', value: `${asNumber(live?.records, 0)}`, hint: `1分 ${asNumber(live?.source_breakdown?.intraday_1m, 0)} / 5分 ${asNumber(live?.source_breakdown?.intraday_5m, 0)} / 日线 ${asNumber(live?.source_breakdown?.daily, 0)}`, tone: 'good' as Tone },
    { title: '首板候选', value: `${asNumber(report?.summary?.first_board_candidates, 0)}`, hint: `原始列表 ${(report?.first_board?.candidates ?? []).length} 只`, tone: 'warn' as Tone },
    { title: '龙头候选', value: `${asNumber(report?.summary?.leaders, 0)}`, hint: `原始列表 ${(report?.first_board?.leaders ?? []).length} 只`, tone: 'warn' as Tone },
    { title: '异常票', value: `${asNumber(report?.anomalies?.event_count ?? report?.summary?.anomalies ?? anomalies.length, 0)}`, hint: anomalies.length ? '建议人工复核' : '当前无明显异常', tone: anomalies.length ? 'bad' as Tone : 'good' as Tone },
    { title: '当前结论', value: selected?.action_label || (report?.empty_signal?.triggered ? '当前仅观察' : '等待盘中更新'), hint: selected?.symbol ? `${selected.symbol} ${cleanDisplayText(selected.name)}`.trim() : '尚未选择股票', tone: (selected?.action ?? '').toLowerCase() === 'buy' ? 'bad' as Tone : (selected?.action ?? '').toLowerCase() === 'watch' ? 'accent' as Tone : 'warn' as Tone },
  ];

  useLayoutEffect(() => {
    if (typeof window === 'undefined') return undefined;

    let frameId = 0;
    const updateDesktopListHeight = () => {
      frameId = 0;
      if (window.innerWidth < 1280) {
        setDesktopListHeight((current) => current === null ? current : null);
        return;
      }

      const section = detailSectionRef.current;
      const detailPanel = detailPanelRef.current;
      if (!section) return;

      const rect = section.getBoundingClientRect();
      const availableHeight = Math.floor(window.innerHeight - rect.top - 24);
      const detailHeight = detailPanel ? Math.ceil(detailPanel.getBoundingClientRect().height) : 0;
      const targetHeight = Math.max(360, availableHeight, detailHeight);
      const nextHeight = targetHeight > 0 ? targetHeight : null;
      setDesktopListHeight((current) => current === nextHeight ? current : nextHeight);
    };

    const scheduleUpdate = () => {
      if (frameId) window.cancelAnimationFrame(frameId);
      frameId = window.requestAnimationFrame(updateDesktopListHeight);
    };

    scheduleUpdate();
    window.addEventListener('resize', scheduleUpdate);
    return () => {
      if (frameId) window.cancelAnimationFrame(frameId);
      window.removeEventListener('resize', scheduleUpdate);
    };
  }, [filteredPool.length, loading, selectedSymbol, summaryCards.length]);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div>
          <h1 className="flex items-center gap-3 text-3xl font-bold tracking-wide text-ink"><Eye className="h-7 w-7 text-accent" /> 观察池与实时盯盘</h1>
          <p className="mt-2 text-sm text-muted">点击左侧股票时先用本地缓存秒切，后台再补刷最新盘口与分时。</p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted">
            <span className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1">扫描时间：{formatDateTime(report?.timestamp || live?.report_timestamp || lastUpdated)}</span>
            <span className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1">实时源：{report?.runtime_source?.provider || '未标记'} / {report?.runtime_source?.mode || '未标记'}</span>
            <span className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1">初筛窗口：{asNumber(report?.prefilter?.lookback_days, 0)} 日</span>
            <span className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1">扫描档位：{report?.scan_profile || 'default'}</span>
          </div>
        </div>
        <div className="flex w-full flex-col gap-3 xl:w-[420px]">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
            <input value={filterText} onChange={(event) => setFilterText(event.target.value)} placeholder="按代码、名称或理由过滤" className="input-field pl-10" />
          </div>
          <div className="flex items-center justify-between gap-3">
            <div className="text-xs text-muted">
              <div>最近刷新：{formatDateTime(lastUpdated || live?.generated_at || report?.timestamp)}</div>
              <div>当前命中：{filteredPool.length} / {liveItems.length}</div>
            </div>
            <button className="btn-outline" onClick={() => void (async () => { await refresh(); if (selectedSymbol) await loadSymbolDetail(selectedSymbol, true, false); })()}>
              <RefreshCw className="h-4 w-4" /> 刷新
            </button>
          </div>
        </div>
      </div>

      {error ? <div className="glass-panel border-warn p-4 text-sm text-warn">列表刷新失败：{error}</div> : null}
      {detailError ? <div className="glass-panel border-warn p-4 text-sm text-warn">单票补刷失败：{detailError}</div> : null}
      {loading && !data ? <div className="glass-panel p-4 text-sm text-muted">观察池数据初始化中…</div> : null}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
        {summaryCards.map((card) => (
          <div key={card.title} className={`rounded-2xl border p-4 shadow-[0_12px_28px_rgba(1,14,23,0.24)] ${toneClass(card.tone)}`}>
            <div className="text-xs tracking-[0.24em] text-muted uppercase">{card.title}</div>
            <div className="mt-3 text-2xl font-bold text-ink">{card.value}</div>
            <div className="mt-2 text-xs text-muted">{card.hint}</div>
          </div>
        ))}
      </div>

      <div ref={detailSectionRef} className="grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(360px,0.9fr)] 2xl:grid-cols-[minmax(0,1.22fr)_minmax(420px,0.9fr)]">
        <div className="glass-panel min-w-0 p-6 xl:flex xl:min-h-0 xl:flex-col" style={desktopListHeight ? { height: `${desktopListHeight}px` } : undefined}>
          <div className="mb-5 flex items-center justify-between gap-3">
            <div>
              <h2 className="text-xl font-bold text-ink">信号池股票</h2>
              <div className="mt-1 text-xs text-muted">点击行秒切右侧详情，悬停会提前预取数据。</div>
            </div>
            <div className="text-xs text-muted">共 {filteredPool.length} 只</div>
          </div>
          <div className="overflow-x-auto xl:min-h-0 xl:flex-1 xl:overflow-auto xl:overscroll-contain">
            <table className="data-table min-w-[920px]">
              <thead className="xl:sticky xl:top-0 xl:z-10 xl:backdrop-blur-sm">
                <tr>
                  <th>代码 / 名称</th><th>最新价</th><th>涨跌幅</th><th>评分</th><th>结论</th><th>仓位</th><th>异常</th><th>核心原因</th>
                </tr>
              </thead>
              <tbody>
                {filteredPool.length ? filteredPool.map((item) => {
                  const symbol = (item.symbol ?? '').trim();
                  const displayName = cleanDisplayText(item.name);
                  const itemChangePct = asNumber(item.change_pct, 0);
                  const anomaly = symbol ? anomalyMap.get(symbol) : undefined;
                  const selectedRow = symbol === selectedSymbol;
                  return (
                    <tr key={symbol || `${item.name ?? 'unknown'}-${item.score ?? 0}`} className={`${selectedRow ? 'bg-[rgba(65,214,179,0.08)]' : ''} cursor-pointer`} onClick={() => handleSelect(symbol)} onMouseEnter={() => void loadSymbolDetail(symbol, false, true)}>
                      <td><div className="flex items-start gap-3"><div><div className="font-semibold text-ink">{symbol || '-'}</div><div className="mt-1 text-xs text-muted">{displayName || '名称待返回'}</div></div>{selectedRow ? <span className="rounded-full border border-[rgba(65,214,179,0.28)] bg-[rgba(65,214,179,0.12)] px-2 py-1 text-[11px] text-accent">当前盯盘</span> : null}</div></td>
                      <td className={`font-mono ${priceClass(itemChangePct)}`}>{formatNumber(item.last_price, 2)}</td>
                      <td><span className={`rounded-full border px-2 py-1 text-xs ${priceChipClass(itemChangePct)}`}>{signedPercent(itemChangePct, 2)}</span></td>
                      <td className="font-mono">{formatNumber(item.score, 1)}</td>
                      <td><span className={`rounded-full border px-2 py-1 text-xs ${actionClass(item.action)}`}>{item.action_label || '待定'}</span></td>
                      <td>{formatPercent(item.suggested_position, 0)}</td>
                      <td>{anomaly ? <span className="rounded-full border border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.12)] px-2 py-1 text-xs text-warn">{anomaly.types?.map((type) => anomalyLabel(type)).join(' / ') || '异常'}</span> : <span className="text-muted">正常</span>}</td>
                      <td className="max-w-[320px] whitespace-normal text-sm text-muted">{(item.reason_summary || item.reasons?.map((reason) => reasonLabel(reason)).join('；') || '-').slice(0, 72)}</td>
                    </tr>
                  );
                }) : <tr><td colSpan={8} className="py-10 text-center text-sm text-muted">当前过滤条件下没有匹配股票。</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div ref={detailPanelRef} className="glass-panel p-6 xl:sticky xl:top-6 xl:self-start">
          {selected ? (
            <div className="space-y-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-2xl font-bold text-ink">{cleanDisplayText(selected.name) || '未命名'}</h2>
                    <span className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1 text-xs text-muted">{selected.symbol || '-'}</span>
                    <span className={`rounded-full border px-3 py-1 text-xs ${actionClass(selected.action)}`}>{selected.action_label || '待定'}</span>
                    <span className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1 text-xs text-muted">{boardStageLabel(selected.board_stage)}</span>
                    {selectedAnomaly ? <span className="rounded-full border border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.12)] px-3 py-1 text-xs text-warn">存在异常</span> : null}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs text-muted">
                    <span>更新时间：{formatDateTime(selected.latest_time || live?.generated_at || report?.timestamp)}</span>
                    <span>盘口：{selected.depth_available ? selected.depth_source || '已接入' : '未接入'}</span>
                  </div>
                </div>
                <div className="text-right">
                  <div className={`flex items-center justify-end gap-2 text-3xl font-bold ${priceClass(changePct)}`}><TrendIcon className="h-7 w-7" />{formatNumber(selected.last_price, 2)}</div>
                  <div className={`mt-1 text-sm ${priceClass(changePct)}`}>{signedNumber(selected.change_amount, 2)} / {signedPercent(changePct, 2)}</div>
                  <div className="mt-2 text-xs text-muted">{detailLoadingSymbol === selectedSymbol ? '后台补刷中…' : '已秒切展示，后台保持热更新'}</div>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4"><div className="text-xs uppercase tracking-[0.18em] text-muted">综合评分</div><div className="mt-2 text-xl font-bold text-ink">{formatNumber(selected.score, 1)}</div></div>
                <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4"><div className="text-xs uppercase tracking-[0.18em] text-muted">建议仓位</div><div className="mt-2 text-xl font-bold text-ink">{formatPercent(selected.suggested_position, 0)}</div></div>
                <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4"><div className="text-xs uppercase tracking-[0.18em] text-muted">成交量</div><div className="mt-2 text-xl font-bold text-ink">{compactAmount(selected.volume)}</div></div>
                <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4"><div className="text-xs uppercase tracking-[0.18em] text-muted">成交额</div><div className="mt-2 text-xl font-bold text-ink">{compactAmount(selected.turnover)}</div></div>
              </div>
              <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4">
                <div className="mb-4 flex items-center justify-between gap-3"><div className="flex items-center gap-2 text-ink"><Waves className="h-4 w-4 text-accent" /><span className="font-semibold">{selected.trend_label || '实时走势'}</span></div><div className="text-xs text-muted">来源：{selected.trend_source || 'unknown'}</div></div>
                {trendPath ? (
                  <div className="space-y-3">
                    <div className={`text-xs ${priceClass(asNumber(selected.trend_change_pct, changePct))}`}>{signedPercent(asNumber(selected.trend_change_pct, changePct), 2)}</div>
                    <div className="h-40 rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.88)] p-3">
                      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-full w-full overflow-visible">
                        <path d={trendPath} fill="none" className={changePct >= 0 ? 'stroke-bad' : 'stroke-good'} strokeWidth="2.2" strokeLinecap="round" />
                      </svg>
                    </div>
                  </div>
                ) : <div className="text-sm text-muted">暂无可展示的走势数据。</div>}
              </div>

              <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4">
                <div className="font-semibold text-ink">信号解读</div>
                <div className="mt-2 text-sm text-muted">{selected.reason_summary || '当前未返回详细摘要，请查看分项标签。'}</div>
                <div className="mt-4 flex flex-wrap gap-2">
                  {(selected.reasons ?? []).length ? selected.reasons?.map((reason) => (
                    <span key={`${selected.symbol ?? 'unknown'}-${reason}`} className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1 text-xs text-muted">{reasonLabel(reason)}</span>
                  )) : <span className="text-sm text-muted">暂无分项理由。</span>}
                </div>
                {selected.isolated ? <div className="mt-4 rounded-2xl border border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.1)] p-4 text-sm text-warn">该股票已进入隔离状态：{selected.isolation_reason || '未说明原因'}。</div> : null}
              </div>

              {selectedAnomaly ? (
                <div className="rounded-2xl border border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.1)] p-4">
                  <div className="flex items-center gap-2 font-semibold text-warn"><AlertTriangle className="h-4 w-4" /> 异常提醒</div>
                  <div className="mt-2 text-sm text-muted">{selectedAnomaly.types?.map((type) => anomalyLabel(type)).join(' / ') || '未知异常'}</div>
                  <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
                    <div className="rounded-xl border border-[rgba(255,184,77,0.22)] bg-[rgba(12,33,48,0.42)] p-3">跳空幅度：{formatPercent(selectedAnomaly.gap_pct, 2)}</div>
                    <div className="rounded-xl border border-[rgba(255,184,77,0.22)] bg-[rgba(12,33,48,0.42)] p-3">5日量比：{formatNumber(selectedAnomaly.volume_ratio_5d, 2)}</div>
                  </div>
                </div>
              ) : <div className="rounded-2xl border border-[rgba(77,223,126,0.24)] bg-[rgba(77,223,126,0.08)] p-4 text-sm text-good">当前盯盘股票暂无异常标签，可以继续观察。</div>}

              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <DepthPanel side="bid" title="五档买盘" levels={selected.bid_levels ?? []} />
                <DepthPanel side="ask" title="五档卖盘" levels={selected.ask_levels ?? []} />
              </div>
            </div>
          ) : <div className="text-sm text-muted">当前没有可展示的股票，请先刷新或检查观察池是否已生成。</div>}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="glass-panel p-5">
          <div className="mb-4 flex items-center justify-between gap-3"><h2 className="text-lg font-bold text-ink">异常票清单</h2><div className="text-xs text-muted">{anomalies.length} 只</div></div>
          {anomalies.length ? <div className="space-y-3">{anomalies.map((item) => { const symbol = (item.symbol ?? '').trim(); return <button key={`anomaly-${symbol}`} type="button" className="w-full rounded-2xl border border-[rgba(255,184,77,0.24)] bg-[rgba(12,33,48,0.55)] p-4 text-left transition hover:bg-[rgba(16,42,61,0.72)]" onClick={() => handleSelect(symbol)}><div className="flex items-start justify-between gap-3"><div><div className="font-semibold text-ink">{symbol || '-'}</div><div className="mt-1 text-xs text-warn">{item.types?.map((type) => anomalyLabel(type)).join(' / ') || '异常'}</div></div><div className="text-right text-xs text-muted"><div>跳空 {formatPercent(item.gap_pct, 2)}</div><div>量比 {formatNumber(item.volume_ratio_5d, 2)}</div></div></div></button>; })}</div> : <div className="text-sm text-muted">当前无异常票。</div>}
        </div>

        <div className="glass-panel p-5">
          <div className="mb-4 flex items-center justify-between gap-3"><h2 className="text-lg font-bold text-ink">首板候选</h2><div className="text-xs text-muted">{(report?.first_board?.candidates ?? []).length} 只</div></div>
          {(report?.first_board?.candidates ?? []).length ? <div className="space-y-3">{(report?.first_board?.candidates ?? []).map((item) => { const symbol = (item.symbol ?? '').trim(); return <button key={`candidate-${symbol}`} type="button" className="w-full rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-left transition hover:bg-[rgba(16,42,61,0.72)]" onClick={() => handleSelect(symbol)}><div className="flex items-start justify-between gap-3"><div><div className="font-semibold text-ink">{symbol || '-'}</div><div className="mt-1 text-xs text-muted">阶段：{boardStageLabel(item.board_stage)}</div></div><div className="text-right text-sm text-ink"><div>评分 {formatNumber(item.score, 1)}</div><div className="mt-1 text-xs text-muted">龙头分 {formatNumber(item.leader_score, 1)}</div></div></div></button>; })}</div> : <div className="text-sm text-muted">当前扫描未发现首板候选。</div>}
        </div>

        <div className="glass-panel p-5">
          <div className="mb-4 flex items-center justify-between gap-3"><h2 className="text-lg font-bold text-ink">龙头候选</h2><div className="text-xs text-muted">{(report?.first_board?.leaders ?? []).length} 只</div></div>
          {(report?.first_board?.leaders ?? []).length ? <div className="space-y-3">{(report?.first_board?.leaders ?? []).map((item) => { const symbol = (item.symbol ?? '').trim(); return <button key={`leader-${symbol}`} type="button" className="w-full rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-left transition hover:bg-[rgba(16,42,61,0.72)]" onClick={() => handleSelect(symbol)}><div className="flex items-start justify-between gap-3"><div><div className="font-semibold text-ink">{symbol || '-'}</div><div className="mt-1 text-xs text-muted">阶段：{boardStageLabel(item.board_stage)}</div></div><div className="text-right text-sm text-ink"><div>评分 {formatNumber(item.score, 1)}</div><div className="mt-1 text-xs text-muted">龙头分 {formatNumber(item.leader_score, 1)}</div></div></div></button>; })}</div> : <div className="text-sm text-muted">当前扫描未发现龙头候选。</div>}
        </div>
      </div>

      <div className="glass-panel p-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between"><div><h2 className="text-lg font-bold text-ink">当前观察池代码</h2><div className="mt-1 text-sm text-muted">当前同步状态：{report?.watchlist_sync?.updated ? '本轮已同步' : report?.watchlist_sync?.enabled ? '本轮未改动，沿用现有观察池' : '自动同步未开启'}</div></div><div className="text-xs text-muted">初筛 {asNumber(report?.prefilter?.top_k, 0)} 只 / 入池 {asNumber(report?.watchlist_size ?? report?.signal_pool?.candidate_count ?? liveItems.length, 0)} 只 / 空信号连续 {asNumber(report?.empty_signal?.no_buy_streak, 0)} 次</div></div>
        <div className="mt-4 flex flex-wrap gap-2">{watchlistSymbols.length ? watchlistSymbols.map((symbol) => <button key={`watch-${symbol}`} type="button" className={`rounded-full border px-3 py-1.5 text-sm transition ${symbol === selectedSymbol ? 'border-[rgba(65,214,179,0.32)] bg-[rgba(65,214,179,0.12)] text-accent' : 'border-panelBorder bg-[rgba(12,33,48,0.55)] text-muted hover:text-ink'}`} onClick={() => handleSelect(symbol)}>{symbol}</button>) : <div className="text-sm text-muted">当前没有已同步的观察池代码。</div>}</div>
      </div>
    </div>
  );
}
