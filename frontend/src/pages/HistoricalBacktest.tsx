import { AlertTriangle, CalendarClock, Loader2, RefreshCw, TrendingDown, TrendingUp } from 'lucide-react';
import { useMemo, useState } from 'react';

import { apiGet, apiPost } from '../lib/api';
import { formatDateTime, formatNumber, formatPercent } from '../lib/format';

// ---- 后端响应结构（对齐 api/backtest.py + backtest/asof_scan.py + backtest/holding_curve.py） ----

type AlgorithmMode = 'week5_daily' | 'legacy_trend';

interface PipelineSignalPayload {
  symbol?: string;
  strategy?: string;
  score?: number;
  grade?: string;
  action?: string;
  target_position?: number;
  reasons?: string[];
}

/** Week5 模式：signal pool 里的原始候选（未过 final gate）。 */
interface Week5PoolCandidatePayload {
  symbol?: string;
  action?: string;
  score?: number;
  shortlist_score?: number;
  shortlist_rank?: number;
  grade?: string;
  reject_new_buy?: boolean;
  overextension?: { level?: string; reject_new_buy?: boolean; reasons?: string[] };
  board_risk?: { consecutive_limit_up?: number; reject_new_buy?: boolean; reasons?: string[] };
  shortlist_reasons?: string[];
}

/** Week5 模式：final selector 的入选/拒绝条目。 */
interface Week5FinalItemPayload {
  symbol?: string;
  score?: number;
  action?: string;
  final_signal_reasons?: string[];
  reject_reasons?: string[];
}

interface Week5FunnelPayload {
  policy?: string;
  mode?: string;
  universe_count?: number;
  quality_count?: number;
  light_count?: number;
  deep_count?: number;
  final_count?: number;
  deep_empty_reason?: string;
  final_signal_cap?: number;
}

interface Week5HistoricalContextPayload {
  as_of?: string;
  decision_time?: string;
  news_neutralized?: boolean;
  intraday_degraded?: boolean;
  intraday_coverage_ratio?: number;
  market_breadth_recomputed?: boolean;
  realtime_data_allowed?: boolean;
  account?: { neutral?: boolean; current_equity?: number; pause_new_buy?: boolean; no_buy_streak?: number };
  model?: { model_id?: string; trained_at?: string; code_commit?: string; config_hash?: string };
}

interface Week5DateEntryPayload {
  as_of?: string;
  run_mode?: string;
  status?: string;
  funnel?: Week5FunnelPayload;
  universe?: { provider_index_count?: number; as_of_valid_count?: number; selected_count?: number; max_staleness_days?: number; batch_error?: string };
  signal_pool?: {
    candidate_count?: number;
    action_counts?: Record<string, number>;
    candidates?: Week5PoolCandidatePayload[];
  };
  final_selection?: {
    min_threshold?: number;
    selected_count?: number;
    rejected_count?: number;
    final_signals?: Week5FinalItemPayload[];
    rejected?: Week5FinalItemPayload[];
    news_risk_gate?: Record<string, unknown>;
  };
  rejection_reasons?: Record<string, number>;
  empty_state?: string;
  stage_timings?: Record<string, unknown>;
  candidates?: Week5FinalItemPayload[];
  candidate_count?: number;
  historical_context?: Week5HistoricalContextPayload;
  anomalies_count?: number;
  holding_curve?: HoldingCurveReportPayload | null;
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
  algorithm?: string;
  neutral_account?: boolean;
  market_breadth_recomputed?: boolean;
  dates_truncated?: boolean;
  model_id?: string;
}

interface AsofBacktestResultPayload {
  generated_at?: string;
  start_date?: string;
  end_date?: string;
  horizon_days?: number;
  algorithm?: string;
  dates?: Record<string, AsofDateEntryPayload & Partial<Week5DateEntryPayload>>;
  caveats?: CaveatsPayload;
}

interface TaskProgressPayload {
  algorithm?: string;
  date?: string;
  date_index?: number;
  date_total?: number;
  stage?: string;
  completed?: number;
  total?: number;
  current_symbol?: string;
}

interface TaskStatusPayload {
  task_id?: string;
  status?: string;
  result?: AsofBacktestResultPayload;
  error?: string;
  progress?: TaskProgressPayload;
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

function actionBadge(action: string | undefined): string {
  switch ((action ?? '').toLowerCase()) {
    case 'buy':
      return 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good';
    case 'watch':
      return 'border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.10)] text-warn';
    case 'hold':
      return 'border-panelBorder text-muted';
    case 'sell':
      return 'border-bad/40 bg-[rgba(239,68,68,0.10)] text-bad';
    default:
      return 'border-panelBorder text-muted';
  }
}

/** Week5 空态分类的中文说明（后端 empty_state）。 */
function emptyStateText(emptyState: string | undefined): { title: string; detail: string } {
  switch (emptyState) {
    case 'data_gate_blocked':
      return {
        title: '数据门阻断',
        detail: '当日数据快照缺失/过期或分钟新鲜度不足，系统按 fail-closed 拦截了整轮扫描，这不是选股结论。',
      };
    case 'no_eligible':
      return {
        title: '无合格股票',
        detail: '质量筛选/Light/Deep 漏斗后没有可用候选（可能是当日历史数据覆盖不足）。',
      };
    case 'no_raw_buy':
      return {
        title: '无原始 buy',
        detail: '漏斗产出了候选，但没有一只触发 buy 信号（观望/持有为主）。',
      };
    case 'final_gate_rejected_all':
      return {
        title: '有 buy 但全部被 final gate 拒绝',
        detail: '原始 buy 信号存在，但全部被数据门/风控门/过热/连板/新闻门/最低分阈值拒绝，请在拒绝原因中查看明细。',
      };
    default:
      return { title: '', detail: '' };
  }
}

const WEEK5_STAGE_LABELS: Record<string, string> = {
  universe: '历史股票池 + 质量筛选',
  quality: '质量筛选',
  snapshot: 'Feature Snapshot',
  light: 'Light 漏斗',
  deep: 'Deep 排序',
  final: 'Final selection',
  holding: '持有期分析',
};

async function pollTask(taskId: string, onTick?: (progress?: TaskProgressPayload) => void): Promise<TaskStatusPayload> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const payload = await apiGet<TaskStatusPayload>(`/tasks/${taskId}`);
    if (payload.status === 'succeeded' || payload.status === 'failed') {
      return payload;
    }
    onTick?.(payload.progress);
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
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
  algorithm: AlgorithmMode;
  topN: number;
  holdingTopN: number;
  horizonDays: number;
}): string {
  const { mode, singleDate, startDate, endDate, algorithm, topN, holdingTopN, horizonDays } = params;
  if (mode === 'single') {
    if (!singleDate) return '请选择回测日期。';
  } else {
    if (!startDate || !endDate) return '请选择完整的日期区间（开始与结束日期）。';
    if (startDate > endDate) return '日期区间无效：开始日期不能晚于结束日期。';
  }
  if (algorithm === 'legacy_trend') {
    if (!Number.isInteger(topN) || topN < 1) return 'Top N 必须是不小于 1 的整数。';
    // 上限与后端 AsofBacktestRequest 的 Field(ge=1, le=500) 保持一致，避免前端放过去
    // 再被后端 422 拒掉
    if (topN > 500) return 'Top N 最大支持 500。';
  } else if (Number.isInteger(holdingTopN) && (holdingTopN < 1 || holdingTopN > 100)) {
    return '持有分析数量必须是 1~100 之间的整数（留空则统计全部最终入选）。';
  }
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
  if (raw.includes(' 409') && raw.includes('week5_backtest_busy')) {
    return '已有一个 Week5 完整链路回测任务在运行（同一时间只允许一个），请等它完成后再提交。';
  }
  if (raw.includes(' 409') && raw.includes('week5_backtest_disabled')) {
    return 'Week5 完整链路回测当前被功能开关关闭，请联系运维开启（asof_backtest.week5_daily_enabled）。';
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
function StaticCaveatsNotice({ algorithm }: { algorithm: AlgorithmMode }) {
  return (
    <div className="glass-panel flex flex-col gap-2 border-warn p-4 text-sm text-warn">
      <div className="flex items-center gap-2 font-bold">
        <AlertTriangle className="h-5 w-5" /> 请先了解本功能的结果口径
      </div>
      <ul className="ml-5 list-disc space-y-1">
        <li>
          回测使用<strong>当前</strong>模型回溯更早的日期，模型已“见过”回测日之后的市场，
          存在未来函数，<strong>结果系统性偏乐观</strong>，不等于当时真实能取得的表现。
        </li>
        <li>历史新闻无法追溯，新闻情绪一律按中性处理。</li>
        <li>分钟级特征受数据覆盖限制会降级为缺失，判断依据弱于实盘。</li>
        {algorithm === 'week5_daily' ? (
          <>
            <li>Week5 模式复现“当日收盘后”的完整主选股链路（全市场 → 质量 → Light → Deep → Final）。</li>
            <li>
              历史账户状态采用<strong>中性假设</strong>（空仓、无暂停开仓、无连败），实际当日账户状态无法复原。
            </li>
            <li>
              Week5 全市场回测为重计算任务，<strong>最多 5 个交易日/次</strong>且逐日执行，耗时可能以小时计。
            </li>
          </>
        ) : (
          <li>旧版快速扫描的候选池默认取当前关注池，并非当日全市场，存在选择偏差。</li>
        )}
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
            存在未来函数偏差：当前模型
            {caveats.model_id ? <span className="font-mono"> {caveats.model_id} </span> : null}
            训练于{' '}
            {caveats.model_trained_at ? formatDateTime(caveats.model_trained_at) : '未知时间'}
            ，回测早于该时间的日期时，结果会比真实历史表现更乐观。
          </li>
        ) : null}
        {caveats.news_neutralized ? (
          <li>新闻情绪已中性化处理：历史新闻数据无法追回，本结果未计入真实新闻面影响。</li>
        ) : null}
        {caveats.intraday_degraded ? (
          <li>
            分钟级（intraday）特征在部分回测日降级为缺失：分钟数据仅覆盖到{' '}
            {caveats.intraday_coverage_until || '未知日期'} 之前，之后的回测日期缺少这部分特征。
          </li>
        ) : null}
        {caveats.neutral_account ? (
          <li>
            历史账户状态采用<b>中性假设</b>：假设空仓、无暂停开仓、无今日持仓与连败，仅保留股票级风险、模型门控与 final selection。
          </li>
        ) : null}
        {caveats.market_breadth_recomputed ? (
          <li>市场广度按回测日历史日线现算，不读取当前市场广度快照。</li>
        ) : null}
        {caveats.algorithm === 'week5_daily' ? null : caveats.candidate_pool_bias ? (
          <li>
            候选池选择偏差：本次候选标的来自<b>当前关注池</b>，其构成会随时间推移持续调整，
            用当前关注池去回测更早的历史日期，等价于用回测日<b>之后</b>才知道的关注结果去筛选候选池，
            不等同于当日真实全市场选股范围。若需规避此偏差，可在提交请求时显式指定标的清单。
          </li>
        ) : null}
        {caveats.dates_truncated ? (
          <li>请求区间超过单次上限，仅回测了区间内前若干个交易日（week5_max_dates_per_run）。</li>
        ) : null}
      </ul>
    </div>
  );
}

function HoldingCurveSection(props: { holdingCurve: HoldingCurveReportPayload | null | undefined }) {
  const { holdingCurve } = props;
  if (!holdingCurve || !holdingCurve.results?.length) {
    return <div className="text-sm text-muted">该日期暂无最终入选股票的持有期走势数据。</div>;
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

/** Week5 漏斗条：全市场 → 质量 → Light → Deep → Final。 */
function Week5FunnelBar(props: { funnel: Week5FunnelPayload | undefined; universe?: Week5DateEntryPayload['universe'] }) {
  const { funnel, universe } = props;
  const stages = [
    { label: '全市场索引', value: universe?.provider_index_count },
    { label: 'as-of 有效', value: universe?.as_of_valid_count },
    { label: '质量筛选', value: funnel?.quality_count ?? funnel?.universe_count },
    { label: 'Light', value: funnel?.light_count },
    { label: 'Deep', value: funnel?.deep_count },
    { label: 'Final', value: funnel?.final_count },
  ];
  return (
    <div className="flex flex-wrap items-center gap-2">
      {stages.map((stage, index) => (
        <div key={stage.label} className="flex items-center gap-2">
          {index > 0 ? <span className="text-muted">→</span> : null}
          <div className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-2 text-center">
            <div className="text-[11px] text-muted">{stage.label}</div>
            <div className="font-mono text-lg font-bold">{stage.value ?? '-'}</div>
          </div>
        </div>
      ))}
      <span className="ml-2 text-xs text-muted">漏斗策略：{funnel?.policy || '-'}（deep 空结果归因：{funnel?.deep_empty_reason || '无'}）</span>
    </div>
  );
}

/** Week5 单日结果区：漏斗 + 原始池/最终入选/最终拒绝 + 空态 + 持有期。 */
function Week5DateSection(props: { entry: Week5DateEntryPayload }) {
  const { entry } = props;
  const emptyState = emptyStateText(entry.empty_state);
  const finalSignals = entry.final_selection?.final_signals ?? entry.candidates ?? [];
  const rejectedItems = entry.final_selection?.rejected ?? [];
  const rejectionReasons = Object.entries(entry.rejection_reasons ?? {}).sort((a, b) => b[1] - a[1]);
  const poolCandidates = entry.signal_pool?.candidates ?? [];
  const actionCounts = entry.signal_pool?.action_counts ?? {};

  return (
    <div className="space-y-6">
      <Week5FunnelBar funnel={entry.funnel} universe={entry.universe} />

      {emptyState.title ? (
        <div className="flex items-start gap-2 rounded-xl border border-warn/40 bg-[rgba(255,184,77,0.08)] p-4 text-sm text-warn">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <div className="font-bold">{emptyState.title}</div>
            <div className="mt-1 opacity-90">{emptyState.detail}</div>
          </div>
        </div>
      ) : null}

      {entry.historical_context ? (
        <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.4)] p-3 text-xs text-muted">
          <span>决策时点：{entry.historical_context.decision_time ?? '-'}</span>
          <span>新闻：{entry.historical_context.news_neutralized ? '中性化' : '-'}</span>
          <span>
            分钟特征：
            {entry.historical_context.intraday_degraded
              ? `降级（覆盖率 ${formatPercent(entry.historical_context.intraday_coverage_ratio, 0)}）`
              : '正常'}
          </span>
          <span>账户：中性假设（空仓）</span>
          <span>广度：按 {entry.historical_context.as_of} 历史日线现算</span>
          <span>
            模型：{entry.historical_context.model?.model_id || '未知'}
            {entry.historical_context.model?.trained_at ? `（${entry.historical_context.model.trained_at}）` : ''}
          </span>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-bold">原始信号池（buy/watch/hold）</h3>
            <span className="text-xs text-muted">共 {entry.signal_pool?.candidate_count ?? 0} 只</span>
          </div>
          <div className="mb-3 flex flex-wrap gap-2 text-xs">
            {Object.entries(actionCounts).map(([action, count]) => (
              <span key={action} className={`rounded-full border px-2 py-1 ${actionBadge(action)}`}>
                {action}: {count}
              </span>
            ))}
            {!Object.keys(actionCounts).length ? <span className="text-muted">无候选</span> : null}
          </div>
          <div className="max-h-72 space-y-1 overflow-y-auto text-sm">
            {poolCandidates.slice(0, 50).map((item) => (
              <div key={item.symbol} className="flex items-center justify-between gap-2 rounded-lg px-2 py-1 hover:bg-[rgba(65,214,179,0.05)]">
                <div className="flex items-center gap-2">
                  <span className={`rounded-full border px-2 py-0.5 text-[11px] ${actionBadge(item.action)}`}>{item.action || '-'}</span>
                  <span className="font-mono">{item.symbol}</span>
                </div>
                <span className="font-mono text-xs text-muted">{formatNumber(item.shortlist_score ?? item.score, 1)}</span>
              </div>
            ))}
            {!poolCandidates.length ? <div className="text-sm text-muted">当日无原始候选。</div> : null}
          </div>
        </div>

        <div className="rounded-2xl border border-[rgba(77,223,126,0.3)] bg-[rgba(8,25,39,0.86)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-bold text-good">最终入选（final signals）</h3>
            <span className="text-xs text-muted">
              共 {finalSignals.length} 只（阈值 {formatNumber(entry.final_selection?.min_threshold, 0)}）
            </span>
          </div>
          <div className="max-h-72 space-y-2 overflow-y-auto text-sm">
            {finalSignals.map((item) => (
              <div key={item.symbol} className="rounded-lg border border-panelBorder/70 px-3 py-2">
                <div className="flex items-center justify-between">
                  <span className="font-mono font-bold">{item.symbol}</span>
                  <span className="font-mono text-good">{formatNumber(item.score, 1)}</span>
                </div>
                <div className="mt-1 text-xs text-muted">{(item.final_signal_reasons ?? []).join('、') || '通过全部 final gate'}</div>
              </div>
            ))}
            {!finalSignals.length ? <div className="text-sm text-muted">当日无最终入选。</div> : null}
          </div>
        </div>

        <div className="rounded-2xl border border-panelBorder bg-[rgba(8,25,39,0.86)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-bold">最终拒绝与原因</h3>
            <span className="text-xs text-muted">共 {rejectedItems.length} 只被拒</span>
          </div>
          {rejectionReasons.length ? (
            <div className="mb-3 flex flex-wrap gap-2 text-xs">
              {rejectionReasons.map(([reason, count]) => (
                <span key={reason} className="rounded-full border border-bad/30 bg-[rgba(239,68,68,0.08)] px-2 py-1 text-bad">
                  {reason}: {count}
                </span>
              ))}
            </div>
          ) : null}
          <div className="max-h-72 space-y-1 overflow-y-auto text-sm">
            {rejectedItems.slice(0, 50).map((item) => (
              <div key={item.symbol} className="rounded-lg px-2 py-1 hover:bg-[rgba(239,68,68,0.05)]">
                <div className="flex items-center justify-between">
                  <span className="font-mono">{item.symbol}</span>
                  <span className="text-xs text-bad">{(item.reject_reasons ?? []).join('、')}</span>
                </div>
              </div>
            ))}
            {!rejectedItems.length ? <div className="text-sm text-muted">无被拒条目。</div> : null}
          </div>
        </div>
      </div>

      <div>
        <h2 className="mb-3 text-lg font-bold">持有期走势与收益统计（仅最终入选）</h2>
        <HoldingCurveSection holdingCurve={entry.holding_curve} />
      </div>
    </div>
  );
}

export default function HistoricalBacktestPage() {
  const [algorithm, setAlgorithm] = useState<AlgorithmMode>('week5_daily');
  const [mode, setMode] = useState<'single' | 'range'>('single');
  const [singleDate, setSingleDate] = useState('2026-07-31');
  const [startDate, setStartDate] = useState('2026-07-27');
  const [endDate, setEndDate] = useState('2026-07-31');
  const [symbolsText, setSymbolsText] = useState('');
  const [topN, setTopN] = useState(50);
  const [holdingTopN, setHoldingTopN] = useState<number | typeof NaN>(20);
  const [horizonDays, setHorizonDays] = useState(10);

  const [submitting, setSubmitting] = useState(false);
  const [polling, setPolling] = useState(false);
  const [progress, setProgress] = useState<TaskProgressPayload | undefined>(undefined);
  const [error, setError] = useState('');
  const [result, setResult] = useState<AsofBacktestResultPayload | null>(null);
  const [selectedDate, setSelectedDate] = useState<string>('');

  const dateKeys = useMemo(() => Object.keys(result?.dates ?? {}).sort(), [result]);
  const activeDateKey = selectedDate && dateKeys.includes(selectedDate) ? selectedDate : dateKeys[0] ?? '';
  const activeEntry = activeDateKey ? result?.dates?.[activeDateKey] : undefined;
  const isWeek5 = (result?.algorithm ?? '') === 'week5_daily' || (activeEntry as Week5DateEntryPayload | undefined)?.run_mode === 'historical';

  const handleSubmit = async () => {
    const validationError = validateBacktestInputs({
      mode,
      singleDate,
      startDate,
      endDate,
      algorithm,
      topN,
      holdingTopN,
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
    setProgress(undefined);
    try {
      const symbols = symbolsText
        .split(/[,\s]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      const base = {
        algorithm,
        horizon_days: horizonDays,
        holding_top_n: Number.isInteger(holdingTopN) ? holdingTopN : undefined,
      };
      const body =
        algorithm === 'week5_daily'
          ? mode === 'single'
            ? { date: singleDate, symbols, ...base }
            : { start_date: startDate, end_date: endDate, symbols, ...base }
          : mode === 'single'
            ? { date: singleDate, symbols, top_n: topN, horizon_days: horizonDays }
            : { start_date: startDate, end_date: endDate, symbols, top_n: topN, horizon_days: horizonDays };
      const submitted = await apiPost<{ task_id: string }>('/backtest/asof-scan', body);
      setSubmitting(false);
      setPolling(true);
      const finalStatus = await pollTask(submitted.task_id, setProgress);
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
  const progressLabel = progress
    ? `${WEEK5_STAGE_LABELS[progress.stage ?? ''] ?? progress.stage ?? ''}${
        progress.date ? ` ｜ ${progress.date}（${(progress.date_index ?? 0) + 1}/${progress.date_total ?? '?'}）` : ''
      }${progress.current_symbol ? ` ｜ ${progress.current_symbol}` : ''}`
    : '';

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
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className={`rounded-lg border px-4 py-2 text-sm ${algorithm === 'week5_daily' ? 'border-accent bg-[rgba(65,214,179,0.12)] text-accent' : 'border-panelBorder text-muted'}`}
            onClick={() => setAlgorithm('week5_daily')}
          >
            Week5 完整链路（每日主选股）
          </button>
          <button
            type="button"
            className={`rounded-lg border px-4 py-2 text-sm ${algorithm === 'legacy_trend' ? 'border-accent bg-[rgba(65,214,179,0.12)] text-accent' : 'border-panelBorder text-muted'}`}
            onClick={() => setAlgorithm('legacy_trend')}
          >
            旧版快速扫描（诊断）
          </button>
          <div className="mx-2 h-6 w-px bg-panelBorder" />
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
            日期区间（Week5 最多 5 个交易日）
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
            标的清单（{algorithm === 'week5_daily' ? '留空 = 历史全市场' : '留空用默认关注池'}，逗号或空格分隔）
            <input
              type="text"
              value={symbolsText}
              onChange={(event) => setSymbolsText(event.target.value)}
              placeholder="600000, 000001"
              className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
            />
          </label>
          {algorithm === 'legacy_trend' ? (
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
          ) : (
            <label className="flex flex-col gap-1 text-sm text-muted">
              持有分析数量上限（仅统计最终入选）
              <input
                type="number"
                min={1}
                max={100}
                placeholder="留空 = 全部最终入选"
                value={Number.isNaN(holdingTopN) ? '' : holdingTopN}
                onChange={(event) =>
                  setHoldingTopN(event.target.value === '' ? Number.NaN : Number(event.target.value))
                }
                className="rounded-lg border border-panelBorder bg-[rgba(8,25,39,0.7)] px-3 py-2 text-ink"
              />
            </label>
          )}
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

        {algorithm === 'week5_daily' ? (
          <div className="text-xs text-muted">
            Week5 模式使用自身配置的质量/Light/Deep/Final 上限（全市场 → 质量 → Light → Deep → Final），
            Top N 不参与漏斗；持有期走势只分析最终入选股票。区间最多 5 个交易日，逐日执行、禁止并发。
          </div>
        ) : null}

        <button className="btn-primary" onClick={() => void handleSubmit()} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <TrendingUp className="h-4 w-4" />}
          {submitting ? '提交中…' : polling ? '回测运行中，请稍候…' : '开始回测'}
        </button>

        {polling && progressLabel ? (
          <div className="flex items-center gap-2 text-sm text-accent">
            <Loader2 className="h-4 w-4 animate-spin" /> 当前阶段：{progressLabel}
          </div>
        ) : null}
      </div>

      {error ? <div className="glass-panel border-bad p-4 text-bad">{error}</div> : null}

      {/* 还没有结果时展示静态口径提示：让用户在**提交前**就知道结果偏乐观，
          而不是等看到数字之后才被告知（UI 验证反馈）。拿到结果后由下面的
          CaveatsBanner 接管，给出模型训练时间等精确值，避免两块重复。 */}
      {result ? null : <StaticCaveatsNotice algorithm={algorithm} />}

      {result ? (
        <>
          <CaveatsBanner caveats={result.caveats} />

          <div className="glass-panel p-6">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <div className="text-sm text-muted">
                {isWeek5 ? 'Week5 完整链路 ｜ ' : ''}
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
              isWeek5 ? (
                <Week5DateSection entry={activeEntry as Week5DateEntryPayload} />
              ) : (
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
              )
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
