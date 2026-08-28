import { AlertTriangle, CalendarClock, Loader2, RefreshCw, TrendingDown, TrendingUp } from 'lucide-react';
import { useMemo, useState } from 'react';

import { apiGet, apiPost } from '../lib/api';
import { formatDateTime, formatNumber, formatPercent } from '../lib/format';

// ---- 后端响应结构（对齐 api/backtest.py + backtest/asof_scan.py + backtest/holding_curve.py） ----

interface PipelineSignalPayload {
  symbol?: string;
  strategy?: string;
  score?: number;
  grade?: string;
  action?: string;
  target_position?: number;
  reasons?: string[];
}

interface HoldingDayReturnPayload {
  offset?: number;
  trade_date?: string;
  close?: number;
  return_pct?: number;
  high_return_pct?: number;
  low_return_pct?: number;
}

interface SymbolHoldingResultPayload {
  symbol?: string;
  entry_date?: string;
  entry_price?: number;
  status?: string;
  error?: string;
  horizon_days?: number;
  available_trading_days?: number;
  daily_returns?: HoldingDayReturnPayload[];
  best_exit_offset?: number;
  best_exit_return_pct?: number;
  max_drawdown_pct?: number;
  take_profit_triggered?: boolean;
  stop_loss_triggered?: boolean;
  matched_net_return_pct?: number;
}

interface HoldingCurveSummaryPayload {
  symbol_count?: number;
  ok_count?: number;
  win_count?: number;
  loss_count?: number;
  win_rate?: number;
  avg_best_exit_offset?: number;
  profit_loss_ratio?: number;
  avg_return_by_offset?: Record<string, number>;
}

interface HoldingCurveReportPayload {
  entry_date?: string;
  horizon_days?: number;
  results?: SymbolHoldingResultPayload[];
  summary?: HoldingCurveSummaryPayload;
}

interface AsofDateEntryPayload {
  as_of?: string;
  candidates?: PipelineSignalPayload[];
  candidate_count?: number;
  errors?: unknown[];
  holding_curve?: HoldingCurveReportPayload | null;
}

interface CaveatsPayload {
  lookahead_bias?: boolean;
  model_trained_at?: string;
  news_neutralized?: boolean;
  intraday_degraded?: boolean;
  intraday_coverage_until?: string;
  candidate_pool_source?: string;
  candidate_pool_bias?: boolean;
  worker_count?: number;
  symbols_scanned?: number;
  dates_scanned?: string[];
}

interface AsofBacktestResultPayload {
  generated_at?: string;
  start_date?: string;
  end_date?: string;
  horizon_days?: number;
  dates?: Record<string, AsofDateEntryPayload>;
  caveats?: CaveatsPayload;
}

interface TaskStatusPayload {
  task_id?: string;
  status?: string;
  result?: AsofBacktestResultPayload;
  error?: string;
}

// ---- SVG 折线：复用 ObservationPoolPage.tsx 的 sparklinePath 实现（0-100 归一化坐标）----

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

function returnTone(value: number | undefined): string {
  const v = value ?? 0;
  if (v > 0) return 'text-good';
  if (v < 0) return 'text-bad';
  return 'text-muted';
}

async function pollTask(taskId: string, onTick?: () => void): Promise<TaskStatusPayload> {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    const payload = await apiGet<TaskStatusPayload>(`/tasks/${taskId}`);
    if (payload.status === 'succeeded' || payload.status === 'failed') {
      return payload;
    }
    onTick?.();
    await new Promise((resolve) => window.setTimeout(resolve, 800));
  }
  throw new Error('任务轮询超时，请稍后在“最近结果”中查看是否已完成。');
}

/** 提交前的前端校验，返回空串表示通过。
 *
 * 后端虽然也有 Query 约束，但让用户点完才收到一条英文 422 体验很差；更麻烦的是
 * 原生 number 控件在填 0 时会静默变成 1、填负数时仍允许提交（UI 验证实测到），
 * 用户根本不知道自己的输入被改写过。所以这里显式挡住并给出中文原因。
 */
function validateBacktestInputs(params: {
  mode: 'single' | 'range';
  singleDate: string;
  startDate: string;
  endDate: string;
  topN: number;
  horizonDays: number;
}): string {
  const { mode, singleDate, startDate, endDate, topN, horizonDays } = params;
  if (mode === 'single') {
    if (!singleDate) return '请选择回测日期。';
  } else {
    if (!startDate || !endDate) return '请选择完整的日期区间（开始与结束日期）。';
    if (startDate > endDate) return '日期区间无效：开始日期不能晚于结束日期。';
  }
  if (!Number.isInteger(topN) || topN < 1) return 'Top N 必须是不小于 1 的整数。';
  // 上限与后端 AsofBacktestRequest 的 Field(ge=1, le=500) 保持一致，避免前端放过去
  // 再被后端 422 拒掉
  if (topN > 500) return 'Top N 最大支持 500。';
  if (!Number.isInteger(horizonDays) || horizonDays < 1) {
    return '持有交易日数必须是不小于 1 的整数。';
  }
  if (horizonDays > 60) return '持有交易日数最大支持 60（与后端约束一致）。';
  return '';
}

/** 把接口层的技术错误转成用户能理解的中文说明。
 *
 * 原先直接把 `GET /tasks/xxx failed: 401` 这类信息抛到界面上，用户既看不懂也不
 * 知道该做什么（UI 验证反馈）。这里保留原文附在括号里，便于排查时对照日志。
 */
function toFriendlyError(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  if (raw.includes(' 401')) {
    return `鉴权失败：当前页面没有取得有效的 API token。请硬刷新页面（Ctrl+Shift+R）重试；若仍失败，说明前端产物与后端不同步，需要联系运维重新发布前端。（${raw}）`;
  }
  if (raw.includes(' 403')) {
    return `鉴权被拒绝：token 无效或已轮换。请硬刷新页面重新获取。（${raw}）`;
  }
  if (raw.includes(' 404')) {
    return `请求的资源不存在，可能是任务记录已过期或服务刚重启过。（${raw}）`;
  }
  if (raw.includes(' 422')) {
    return `参数校验未通过，请检查日期与数值输入。（${raw}）`;
  }
  if (raw.includes(' 5')) {
    return `服务端处理失败，请稍后重试或查看服务日志。（${raw}）`;
  }
  return raw;
}

/** 提交前就展示的静态口径提示。
 *
 * CaveatsBanner 依赖接口返回的 caveats，所以只有拿到结果后才会出现；但"这个功能
 * 的结果偏乐观、不能当真实历史表现看"这件事，用户在**提交之前**就该知道，否则等
 * 看到结果时已经先形成了错误预期（UI 验证反馈）。这里给不依赖接口数据的通用版本，
 * 具体日期与模型训练时间仍由结果区的 CaveatsBanner 给出精确值。
 */
function StaticCaveatsNotice() {
  return (
    <div className="glass-panel flex flex-col gap-2 border-warn p-4 text-sm text-warn">
      <div className="flex items-center gap-2 font-bold">
        <AlertTriangle className="h-5 w-5" /> 请先了解本功能的结果口径
      </div>
      <ul className="ml-5 list-disc space-y-1">
        <li>
          回测使用<strong>当前</strong>模型回溯更早的日期，模型已"见过"回测日之后的市场，
          存在未来函数，<strong>结果系统性偏乐观</strong>，不等于当时真实能取得的表现。
        </li>
        <li>历史新闻无法追溯，新闻情绪一律按中性处理。</li>
        <li>分钟级特征受数据覆盖限制会降级为缺失，判断依据弱于实盘。</li>
        <li>候选池默认取当前关注池，并非当日全市场，存在选择偏差。</li>
      </ul>
      <div className="text-xs opacity-80">提交后结果区会给出精确的模型训练时间与数据覆盖截止日期。</div>
    </div>
  );
}

function CaveatsBanner(props: { caveats: CaveatsPayload | undefined }) {
  const { caveats } = props;
  if (!caveats) return null;
  return (
    <div className="glass-panel flex flex-col gap-2 border-warn p-4 text-sm text-warn">
      <div className="flex items-center gap-2 font-bold">
        <AlertTriangle className="h-5 w-5" /> 口径说明（务必阅读）
      </div>
      <ul className="list-disc space-y-1 pl-6 text-warn/90">
        {caveats.lookahead_bias ? (
          <li>
            存在未来函数偏差：当前模型训练于{' '}
            {caveats.model_trained_at ? formatDateTime(caveats.model_trained_at) : '未知时间'}
            ，回测早于该时间的日期时，结果会比真实历史表现更乐观。
          </li>
        ) : null}
        {caveats.news_neutralized ? (
          <li>新闻情绪已中性化处理（固定 0.50 分）：历史新闻数据无法追回，本结果未计入真实新闻面影响。</li>
        ) : null}
        {caveats.intraday_degraded ? (
          <li>
            分钟级（intraday）特征已降级为缺失（约 40 个特征列为空）：分钟数据仅覆盖到{' '}
            {caveats.intraday_coverage_until || '未知日期'} 之前，之后的回测日期缺少这部分特征。
          </li>
        ) : null}
        {caveats.candidate_pool_bias ? (
          <li>
            候选池选择偏差：本次候选标的来自<b>当前关注池</b>，其构成会随时间推移持续调整，
            用当前关注池去回测更早的历史日期，等价于用回测日<b>之后</b>才知道的关注结果去筛选候选池，
            不等同于当日真实全市场选股范围。若需规避此偏差，可在提交请求时显式指定标的清单。
          </li>
        ) : null}
      </ul>
    </div>
  );
}

function HoldingCurveSection(props: { holdingCurve: HoldingCurveReportPayload | null | undefined }) {
  const { holdingCurve } = props;
  if (!holdingCurve || !holdingCurve.results?.length) {
    return <div className="text-sm text-muted">该日期暂无候选标的的持有期走势数据。</div>;
  }
  const summary = holdingCurve.summary ?? {};
  const offsetEntries = Object.entries(summary.avg_return_by_offset ?? {})
    .map(([offset, value]) => ({ offset: Number(offset), value }))
    .sort((a, b) => a.offset - b.offset);
  const bestOffsetEntry = offsetEntries.reduce<{ offset: number; value: number } | null>(
    (best, current) => (best === null || current.value > best.value ? current : best),
    null,
  );

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-3">
          <div className="text-xs text-muted">胜率</div>
          <div className="mt-1 font-mono text-xl font-bold text-good">{formatPercent(summary.win_rate, 1)}</div>
        </div>
        <div className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-3">
          <div className="text-xs text-muted">盈亏比</div>
          <div className="mt-1 font-mono text-xl font-bold">{formatNumber(summary.profit_loss_ratio, 2)}</div>
        </div>
        <div className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-3">
          <div className="text-xs text-muted">平均最优持有天数</div>
          <div className="mt-1 font-mono text-xl font-bold">{formatNumber(summary.avg_best_exit_offset, 1)} 天</div>
        </div>
        <div className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-3">
          <div className="text-xs text-muted">第几天卖最赚</div>
          <div className="mt-1 font-mono text-xl font-bold text-accent">
            {bestOffsetEntry ? `T+${bestOffsetEntry.offset}（${formatPercent(bestOffsetEntry.value, 2)}）` : '-'}
          </div>
        </div>
      </div>

      <div className="space-y-4">
        {holdingCurve.results.map((item) => {
          const points = (item.daily_returns ?? []).map((day) => day.return_pct ?? 0);
          const path = sparklinePath(points);
          const lastReturn = points.length ? points[points.length - 1] : 0;
          return (
            <div key={`${item.symbol}-${item.entry_date}`} className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4">
              <div className="flex items-center justify-between gap-3">
                <div className="font-mono text-lg font-bold">{item.symbol}</div>
                <div className="text-xs text-muted">
                  {item.status === 'ok'
                    ? `入场价 ${formatNumber(item.entry_price, 2)}（${item.entry_date}）`
                    : `${item.status === 'insufficient_data' ? '数据不足' : '分析异常'}${item.error ? `：${item.error}` : ''}`}
                </div>
              </div>
              {item.status === 'ok' ? (
                <>
                  <div className="mt-3 grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
                    <div>
                      最优退出：T+{item.best_exit_offset ?? 0}（
                      <span className={returnTone(item.best_exit_return_pct)}>{formatPercent(item.best_exit_return_pct, 2)}</span>）
                    </div>
                    <div>
                      最大回撤：<span className="text-bad">{formatPercent(item.max_drawdown_pct, 2)}</span>
                    </div>
                    <div>
                      真实成交净收益：
                      <span className={returnTone(item.matched_net_return_pct)}>{formatPercent(item.matched_net_return_pct, 2)}</span>
                    </div>
                    <div>止盈触发：{item.take_profit_triggered ? '是' : '否'}</div>
                    <div>止损触发：{item.stop_loss_triggered ? '是' : '否'}</div>
                  </div>
                  {path ? (
                    <div className="mt-3 h-28 rounded-xl border border-panelBorder bg-[rgba(8,25,39,0.88)] p-3">
                      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-full w-full overflow-visible">
                        <path
                          d={path}
                          fill="none"
                          className={lastReturn >= 0 ? 'stroke-good' : 'stroke-bad'}
                          strokeWidth="2.2"
                          strokeLinecap="round"
                        />
                      </svg>
                    </div>
                  ) : null}
                </>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function HistoricalBacktestPage() {
  const [mode, setMode] = useState<'single' | 'range'>('single');
  const [singleDate, setSingleDate] = useState('2026-07-31');
  const [startDate, setStartDate] = useState('2026-07-27');
  const [endDate, setEndDate] = useState('2026-07-31');
  const [symbolsText, setSymbolsText] = useState('');
  const [topN, setTopN] = useState(50);
  const [horizonDays, setHorizonDays] = useState(10);

  const [submitting, setSubmitting] = useState(false);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<AsofBacktestResultPayload | null>(null);
  const [selectedDate, setSelectedDate] = useState<string>('');

  const dateKeys = useMemo(() => Object.keys(result?.dates ?? {}).sort(), [result]);
  const activeDateKey = selectedDate && dateKeys.includes(selectedDate) ? selectedDate : dateKeys[0] ?? '';
  const activeEntry = activeDateKey ? result?.dates?.[activeDateKey] : undefined;

  const handleSubmit = async () => {
    const validationError = validateBacktestInputs({
      mode,
      singleDate,
      startDate,
      endDate,
      topN,
      horizonDays,
    });
    if (validationError) {
      setError(validationError);
      return;
    }
    setSubmitting(true);
    setError('');
    setResult(null);
    setSelectedDate('');
    try {
      const symbols = symbolsText
        .split(/[,\s]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      const body =
        mode === 'single'
          ? { date: singleDate, symbols, top_n: topN, horizon_days: horizonDays }
          : { start_date: startDate, end_date: endDate, symbols, top_n: topN, horizon_days: horizonDays };
      const submitted = await apiPost<{ task_id: string }>('/backtest/asof-scan', body);
      setSubmitting(false);
      setPolling(true);
      const finalStatus = await pollTask(submitted.task_id);
      if (finalStatus.status === 'failed') {
        setError(finalStatus.error || '回测任务执行失败。');
      } else if (finalStatus.result) {
        setResult(finalStatus.result);
        const firstDate = Object.keys(finalStatus.result.dates ?? {}).sort()[0];
        if (firstDate) setSelectedDate(firstDate);
      }
    } catch (err) {
      setError(toFriendlyError(err));
    } finally {
      setSubmitting(false);
      setPolling(false);
    }
  };

  const handleLoadLatest = async () => {
    setError('');
    try {
      const latest = await apiGet<{ status?: string; report?: AsofBacktestResultPayload }>(
        '/backtest/asof-scan/latest',
      );
      if (latest.report) {
        setResult(latest.report);
        const firstDate = Object.keys(latest.report.dates ?? {}).sort()[0];
        if (firstDate) setSelectedDate(firstDate);
      } else {
        setError('尚无历史回测结果，请先提交一次回测。');
      }
    } catch (err) {
      setError(toFriendlyError(err));
    }
  };

  const busy = submitting || polling;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
            <CalendarClock className="h-7 w-7 text-accent" /> 历史回测
          </h1>
          <p className="mt-2 text-muted">
            选择历史日期，用当前代码与模型回溯计算当天会选出哪些股票，并查看之后的走势与收益统计。
          </p>
        </div>
        <button className="btn-outline" onClick={() => void handleLoadLatest()} disabled={busy}>
          <RefreshCw className="h-4 w-4" /> 加载最近一次结果
        </button>
      </div>

      <div className="glass-panel space-y-4 p-6">
        <div className="flex items-center gap-2">
          <button
            type="button"
            className={`rounded-lg border px-4 py-2 text-sm ${mode === 'single' ? 'border-accent bg-[rgba(65,214,179,0.12)] text-accent' : 'border-panelBorder text-muted'}`}
            onClick={() => setMode('single')}
          >
            单日回测
          </button>
          <button
            type="button"
            className={`rounded-lg border px-4 py-2 text-sm ${mode === 'range' ? 'border-accent bg-[rgba(65,214,179,0.12)] text-accent' : 'border-panelBorder text-muted'}`}
            onClick={() => setMode('range')}
          >
            日期区间
          </button>
        </div>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          {mode === 'single' ? (
            <label className="flex flex-col gap-1 text-sm text-muted">
              历史日期
              <input
                type="date"
                value={singleDate}
                onChange={(event) => setSingleDate(event.target.value)}
                className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
              />
            </label>
          ) : (
            <>
              <label className="flex flex-col gap-1 text-sm text-muted">
                起始日期
                <input
                  type="date"
                  value={startDate}
                  onChange={(event) => setStartDate(event.target.value)}
                  className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
                />
              </label>
              <label className="flex flex-col gap-1 text-sm text-muted">
                结束日期
                <input
                  type="date"
                  value={endDate}
                  onChange={(event) => setEndDate(event.target.value)}
                  className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
                />
              </label>
            </>
          )}
          <label className="flex flex-col gap-1 text-sm text-muted">
            标的清单（留空用默认关注池，逗号或空格分隔）
            <input
              type="text"
              value={symbolsText}
              onChange={(event) => setSymbolsText(event.target.value)}
              placeholder="600000, 000001"
              className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-muted">
            候选上限 Top N
            <input
              type="number"
              min={1}
              max={500}
              // 不再用 `Number(v) || 1` 兜底：那会把用户填的 0 静默改写成 1，界面上看不出
              // 输入被改过（UI 验证实测到）。这里保留原值（空串记为 NaN），交给
              // validateBacktestInputs 在提交时给出明确的中文错误。
              value={Number.isNaN(topN) ? '' : topN}
              onChange={(event) =>
                setTopN(event.target.value === '' ? Number.NaN : Number(event.target.value))
              }
              className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-muted">
            持有交易日数
            <input
              type="number"
              min={1}
              max={60}
              value={Number.isNaN(horizonDays) ? '' : horizonDays}
              onChange={(event) =>
                setHorizonDays(event.target.value === '' ? Number.NaN : Number(event.target.value))
              }
              className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
            />
          </label>
        </div>

        <button className="btn-primary" onClick={() => void handleSubmit()} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <TrendingUp className="h-4 w-4" />}
          {submitting ? '提交中…' : polling ? '回测运行中，请稍候…' : '开始回测'}
        </button>
      </div>

      {error ? <div className="glass-panel border-bad p-4 text-bad">{error}</div> : null}

      {/* 还没有结果时展示静态口径提示：让用户在**提交前**就知道结果偏乐观，
          而不是等看到数字之后才被告知（UI 验证反馈）。拿到结果后由下面的
          CaveatsBanner 接管，给出模型训练时间等精确值，避免两块重复。 */}
      {result ? null : <StaticCaveatsNotice />}

      {result ? (
        <>
          <CaveatsBanner caveats={result.caveats} />

          <div className="glass-panel p-6">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm text-muted">
                生成时间：{formatDateTime(result.generated_at)} ｜ 区间：{result.start_date} ~ {result.end_date} ｜ 持有
                {result.horizon_days} 个交易日
              </div>
              <div className="flex flex-wrap gap-2">
                {dateKeys.length ? (
                  dateKeys.map((key) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setSelectedDate(key)}
                      className={`rounded-full border px-3 py-1 text-xs ${
                        key === activeDateKey
                          ? 'border-accent bg-[rgba(65,214,179,0.12)] text-accent'
                          : 'border-panelBorder text-muted'
                      }`}
                    >
                      {key}（{result.dates?.[key]?.candidate_count ?? 0} 只）
                    </button>
                  ))
                ) : (
                  <span className="text-xs text-muted">该区间内没有交易日（可能选中了纯周末/节假日区间）。</span>
                )}
              </div>
            </div>

            {activeEntry ? (
              <div className="space-y-6">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-panelBorder text-left text-muted">
                        <th className="px-3 py-2">代码</th>
                        <th className="px-3 py-2">策略</th>
                        <th className="px-3 py-2">评分</th>
                        <th className="px-3 py-2">等级</th>
                        <th className="px-3 py-2">动作</th>
                        <th className="px-3 py-2">建议仓位</th>
                        <th className="px-3 py-2">理由</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeEntry.candidates?.length ? (
                        activeEntry.candidates.map((item) => (
                          <tr key={item.symbol} className="border-b border-panelBorder/70">
                            <td className="px-3 py-3 font-mono font-bold">{item.symbol}</td>
                            <td className="px-3 py-3">{item.strategy || '-'}</td>
                            <td className="px-3 py-3 font-mono">{formatNumber(item.score, 1)}</td>
                            <td className="px-3 py-3">{item.grade || '-'}</td>
                            <td className="px-3 py-3">
                              <span
                                className={`rounded-full border px-2 py-1 text-xs ${
                                  item.action === 'buy'
                                    ? 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good'
                                    : 'border-panelBorder text-muted'
                                }`}
                              >
                                {item.action || '-'}
                              </span>
                            </td>
                            <td className="px-3 py-3">{formatPercent(item.target_position, 0)}</td>
                            <td className="px-3 py-3 text-muted">{(item.reasons ?? []).slice(0, 3).join('、') || '-'}</td>
                          </tr>
                        ))
                      ) : (
                        <tr>
                          <td className="px-3 py-8 text-center text-muted" colSpan={7}>
                            该日期没有产生 buy 候选信号。
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>

                {(activeEntry.errors?.length ?? 0) > 0 ? (
                  <div className="flex items-center gap-2 rounded-xl border border-warn/40 bg-[rgba(255,184,77,0.08)] p-3 text-sm text-warn">
                    <TrendingDown className="h-4 w-4" /> 有 {activeEntry.errors?.length} 个标的分析出错，已跳过（不影响其它标的结果）。
                  </div>
                ) : null}

                <div>
                  <h2 className="mb-3 text-lg font-bold">持有期走势与收益统计</h2>
                  <HoldingCurveSection holdingCurve={activeEntry.holding_curve} />
                </div>
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
