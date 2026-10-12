import { AlertTriangle, Clock3, Layers, RefreshCw, ShieldAlert, Sparkles } from 'lucide-react';

import { apiGet } from '../lib/api';
import { formatDateTime, formatNumber, formatPercent } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface TailMeta {
  trade_date?: string;
  probability_field?: string;
  probability_meaning?: string;
  strategy?: string;
  reference_notional_cny?: number;
  contract_version?: string;
  contract_digest?: string;
  entry_window?: string[];
  min_net_profit_probability?: number;
  max_recommendations?: number;
  holding_days?: number;
  take_profit_pct?: number;
  stop_loss_pct?: number;
  data_as_of?: string;
}

interface TailFill {
  filled?: boolean;
  fill_time?: string;
  quantity?: number;
  entry_amount?: number;
  buy_cost?: number;
}

interface TailRow {
  symbol?: string;
  rank?: number;
  probability?: number;
  data_as_of?: string;
  fill?: TailFill;
  caveats?: string[];
  model_identity?: { model_id?: string; identity_recorded?: boolean };
}

interface TailShadowView {
  status?: string;
  mode?: string;
  meta?: TailMeta;
  candidates?: string[];
  final_recommendations?: TailRow[];
  fills?: Record<string, TailFill>;
  rejection_reasons?: {
    final_ranking?: Record<string, string[]>;
    pre_confirmation?: Record<string, string[]>;
  };
  blocking_reason?: string;
  caveats?: string[];
}

interface TailShadowHistory {
  days?: {
    trade_date?: string;
    final_symbols?: string[];
    candidate_count?: number;
    blocking_reason?: string;
  }[];
  count?: number;
}

// 这些原因是"数据还没到"，不是"今天没有机会"：措辞必须分开，
// 否则页面会把采集缺口读成空仓信号。
const DATA_GAP_REASONS = new Set([
  'no_tail_probability_available',
  'minute_bars_unavailable',
  'no_completed_minute_bar',
  'serving_manifest_missing',
  'no_tail_shadow_report',
]);

function reasonText(reason: string): string {
  const mapping: Record<string, string> = {
    no_tail_probability_available: '还没有 p_net_profit_5d_tail 的模型输出，链路不出推荐',
    minute_bars_unavailable: '缺尾盘分钟行情，确认窗口无法判定',
    no_completed_minute_bar: '确认时点还没有已完成的分钟 bar',
    serving_manifest_missing: '缺少在服模型清单，身份无法校验',
    no_tail_shadow_report: '这一天还没有跑过尾盘影子任务',
    below_threshold: '概率低于准入阈值',
    capital_budget_cap: '超出当日资金可支持只数',
    stale_quote: '行情陈旧度不足',
    limit_up_locked: '涨停封板买不进',
    suspended: '停牌',
    below_lot_size: '不足一手申报数量',
    no_valid_price_data: '无有效价格数据',
    no_completed_bar_after_confirmation: '确认后没有下一分钟价格',
    risk_state_blocked: '风险状态不允许',
    model_identity_missing: '模型身份无法验证',
    feature_snapshot_missing: '最终推荐缺特征快照，留档不完整',
    fill_status_missing: '成交状态未记录',
  };
  return mapping[reason] || reason;
}

function ReasonGroup(props: { title: string; groups: Record<string, string[]> }) {
  const entries = Object.entries(props.groups);
  if (!entries.length) {
    return null;
  }
  return (
    <div className="space-y-2">
      <div className="text-xs font-bold tracking-wider text-muted">{props.title}</div>
      {entries.map(([reason, symbols]) => (
        <div key={reason} className="flex items-start justify-between gap-3 text-sm">
          <span className="text-warn">{reasonText(reason)}</span>
          <span className="font-mono text-muted">{(symbols ?? []).join('、')}</span>
        </div>
      ))}
    </div>
  );
}

export default function TailShadowPage() {
  const latest = useAutoRefresh<TailShadowView>(
    () => apiGet<TailShadowView>('/week5/tail-shadow/latest'),
    20000,
  );
  const history = useAutoRefresh<TailShadowHistory>(
    () => apiGet<TailShadowHistory>('/week5/tail-shadow/history?limit=20'),
    60000,
  );

  const view = latest.data ?? {};
  const meta = view.meta ?? {};
  const rows = view.final_recommendations ?? [];
  const candidates = view.candidates ?? [];
  const fills = view.fills ?? {};
  const finalRejects = view.rejection_reasons?.final_ranking ?? {};
  const preRejects = view.rejection_reasons?.pre_confirmation ?? {};
  const blocking = view.blocking_reason ?? '';
  const isDataGap = DATA_GAP_REASONS.has(blocking);
  const rejectedSymbols = new Set(
    Object.values(finalRejects).flat().concat(Object.values(preRejects).flat()),
  );

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
            <Clock3 className="h-7 w-7 text-accent" /> 尾盘确认与最终推荐
          </h1>
          <p className="mt-2 text-muted">
            夜间观察池 → 次日 {meta.entry_window?.[0] || '--:--'}–{meta.entry_window?.[1] || '--:--'}
            {' '}尾盘确认 → 最多 {meta.max_recommendations ?? '-'} 只最终推荐。当前为影子状态，不接管旧推荐链路。
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right text-xs text-muted">
            <div>最近刷新：{latest.lastUpdated ? formatDateTime(latest.lastUpdated) : '-'}</div>
            <div>状态：{latest.loading ? '更新中' : '已同步'}</div>
          </div>
          <button className="btn-outline" onClick={() => { void latest.refresh(); void history.refresh(); }}>
            <RefreshCw className="h-4 w-4" /> 刷新
          </button>
        </div>
      </div>

      {latest.error ? <div className="glass-panel border-warn p-4 text-warn">加载失败：{latest.error}</div> : null}

      <div className="glass-panel p-6">
        <div className="mb-3 flex items-center gap-2 text-sm font-bold tracking-wider text-muted">
          <Sparkles className="h-4 w-4 text-accent" /> 这个概率是什么口径
        </div>
        <div className="grid grid-cols-1 gap-3 text-sm md:grid-cols-3">
          <div>
            <span className="text-muted">概率字段：</span>
            <span className="font-mono">{meta.probability_field || '-'}</span>
          </div>
          <div>
            <span className="text-muted">所属策略：</span>
            {meta.strategy || 'trend'} / {meta.contract_version || '-'}
          </div>
          <div>
            <span className="text-muted">参考金额：</span>
            {meta.reference_notional_cny
              ? `¥${formatNumber(meta.reference_notional_cny, 0)} / 只`
              : '-'}
          </div>
          <div>
            <span className="text-muted">准入阈值：</span>
            {typeof meta.min_net_profit_probability === 'number'
              ? formatPercent(meta.min_net_profit_probability, 0)
              : '-'}
          </div>
          <div>
            <span className="text-muted">数据日期：</span>
            {meta.trade_date || '-'}（数据截至 {meta.data_as_of ? formatDateTime(meta.data_as_of) : '-'}）
          </div>
          <div>
            <span className="text-muted">规则摘要：</span>
            {meta.contract_digest || '-'}
          </div>
        </div>
        <p className="mt-4 text-sm leading-relaxed text-ink/80">
          {meta.probability_meaning || '-'}
        </p>
        <p className="mt-2 text-xs text-muted">
          阈值是初始选股规则，不代表已证明实际命中率达到该数值；命中证据见训练总览与影子验证记录。
        </p>
      </div>

      {blocking ? (
        <div className={`glass-panel flex items-start gap-3 p-4 ${isDataGap ? 'border-warn' : 'border-bad'}`}>
          {isDataGap ? <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-warn" /> : <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-bad" />}
          <div>
            <div className={`font-bold ${isDataGap ? 'text-warn' : 'text-bad'}`}>
              今日最终推荐 0 只：{reasonText(blocking)}
            </div>
            <div className="mt-1 text-xs text-muted">
              {isDataGap
                ? '这是数据/模型可用性缺口，不是"没有达标的股票"；空仓结论不成立。'
                : '硬门或身份校验未通过，链路按 fail-closed 输出 0 只，不补名额。'}
            </div>
          </div>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="glass-panel p-6">
          <h2 className="mb-4 flex items-center gap-2 text-xl font-bold">
            <Layers className="h-5 w-5 text-accent" /> 候选（观察池）
          </h2>
          <div className="space-y-2">
            {candidates.length ? candidates.map((symbol) => {
              const row = rows.find((item) => item.symbol === symbol);
              return (
                <div key={symbol} className="flex items-center justify-between rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] px-4 py-3">
                  <div className="font-mono font-bold">{symbol}</div>
                  <div className="flex items-center gap-3 text-sm">
                    <span className="text-muted">{row ? `概率 ${formatPercent(row.probability, 1)}` : '未入最终排序'}</span>
                    {rejectedSymbols.has(symbol) ? (
                      <span className="rounded-full border border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.10)] px-2 py-1 text-xs text-warn">已拒绝</span>
                    ) : (
                      <span className="rounded-full border border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] px-2 py-1 text-xs text-good">入围</span>
                    )}
                  </div>
                </div>
              );
            }) : <div className="text-muted">今天没有候选，或影子任务尚未产出报告。</div>}
          </div>
        </div>

        <div className="glass-panel p-6">
          <h2 className="mb-4 flex items-center gap-2 text-xl font-bold">
            <Sparkles className="h-5 w-5 text-accent" /> 最终推荐
          </h2>
          <div className="space-y-3">
            {rows.length ? rows.map((row) => (
              <div key={`${row.symbol}-${row.rank}`} className="rounded-xl border border-[rgba(65,214,179,0.28)] bg-[rgba(12,33,48,0.55)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-mono text-lg font-bold">{row.symbol || '-'}</div>
                  <div className="font-mono text-lg font-bold text-accent">{formatPercent(row.probability, 1)}</div>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-sm text-muted">
                  <div>排名：#{row.rank ?? '-'}</div>
                  <div>参考金额：¥{formatNumber(row.fill?.entry_amount ?? meta.reference_notional_cny, 0)}</div>
                  <div>模型：{row.model_identity?.model_id || '-'}</div>
                  <div>身份已记录：{row.model_identity?.identity_recorded ? '是' : '否'}</div>
                </div>
                {(row.caveats ?? []).length ? (
                  <div className="mt-3 text-xs text-warn">留档提示：{(row.caveats ?? []).map(reasonText).join(' / ')}</div>
                ) : null}
              </div>
            )) : <div className="text-muted">今天没有最终推荐，空仓是允许的结果。</div>}
          </div>

          {Object.keys(finalRejects).length || Object.keys(preRejects).length ? (
            <div className="mt-5 space-y-4 border-t border-panelBorder pt-4">
              <div className="text-sm font-bold tracking-wider text-muted">拒绝原因（分层）</div>
              <ReasonGroup title="尾盘确认 / 交易资格" groups={preRejects} />
              <ReasonGroup title="排序与名额" groups={finalRejects} />
            </div>
          ) : null}
        </div>

        <div className="glass-panel p-6">
          <h2 className="mb-4 flex items-center gap-2 text-xl font-bold">
            <ShieldAlert className="h-5 w-5 text-accent" /> 成交状态
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-panelBorder text-left text-muted">
                  <th className="px-2 py-2">代码</th>
                  <th className="px-2 py-2">成交</th>
                  <th className="px-2 py-2">时间</th>
                  <th className="px-2 py-2">数量</th>
                  <th className="px-2 py-2">买入成本</th>
                </tr>
              </thead>
              <tbody>
                {rows.length ? rows.map((row) => {
                  const fill = fills[row.symbol || ''] ?? row.fill ?? {};
                  return (
                    <tr key={`fill-${row.symbol}`} className="border-b border-panelBorder/70">
                      <td className="px-2 py-3 font-mono font-bold">{row.symbol || '-'}</td>
                      <td className="px-2 py-3">
                        <span className={`rounded-full border px-2 py-1 text-xs ${
                          fill.filled
                            ? 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good'
                            : 'border-[rgba(255,123,123,0.28)] bg-[rgba(255,123,123,0.10)] text-bad'
                        }`}
                        >
                          {fill.filled ? '已成交' : '未成交'}
                        </span>
                      </td>
                      <td className="px-2 py-3 text-muted">{fill.fill_time ? formatDateTime(fill.fill_time) : '-'}</td>
                      <td className="px-2 py-3">{fill.quantity ?? '-'}</td>
                      <td className="px-2 py-3">{formatNumber(fill.buy_cost, 2)}</td>
                    </tr>
                  );
                }) : (
                  <tr>
                    <td className="px-2 py-8 text-center text-muted" colSpan={5}>
                      没有成交记录。未成交不计盈亏，成交率单独统计。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-muted">
            成本含佣金、最低佣金、过户费、印花税与滑点；T+1 才可卖出
            {typeof meta.take_profit_pct === 'number'
              ? `，止盈 +${formatPercent(meta.take_profit_pct, 0)}`
              : ''}
            {typeof meta.stop_loss_pct === 'number'
              ? ` / 止损 -${formatPercent(meta.stop_loss_pct, 0)}`
              : ''}
            {meta.holding_days ? `，持有至多 ${meta.holding_days} 个交易日` : ''}。
          </p>
        </div>
      </div>

      <div className="glass-panel p-6">
        <h2 className="mb-4 text-xl font-bold">影子运行记录</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-panelBorder text-left text-muted">
                <th className="px-3 py-2">交易日</th>
                <th className="px-3 py-2">候选数</th>
                <th className="px-3 py-2">最终推荐</th>
                <th className="px-3 py-2">说明</th>
              </tr>
            </thead>
            <tbody>
              {(history.data?.days ?? []).length ? (history.data?.days ?? []).slice().reverse().map((day) => (
                <tr key={day.trade_date} className="border-b border-panelBorder/70">
                  <td className="px-3 py-3 font-mono">{day.trade_date || '-'}</td>
                  <td className="px-3 py-3">{day.candidate_count ?? 0}</td>
                  <td className="px-3 py-3 font-mono">{(day.final_symbols ?? []).join('、') || '空仓'}</td>
                  <td className="px-3 py-3 text-muted">{day.blocking_reason ? reasonText(day.blocking_reason) : '正常出单'}</td>
                </tr>
              )) : (
                <tr>
                  <td className="px-3 py-8 text-center text-muted" colSpan={4}>
                    暂无影子运行记录；影子验证需要连续 60 个完整交易日的留档。
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
