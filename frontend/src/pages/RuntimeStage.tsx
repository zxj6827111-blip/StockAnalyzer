import { useState } from 'react';
import { Activity, Clock3, RefreshCw, Server, ShieldAlert } from 'lucide-react';
import { Link } from 'react-router-dom';

import { apiGet, apiPost } from '../lib/api';
import { asNumber, formatDateTime, formatPercent } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface RuntimePhase {
  code?: string;
  label?: string;
  detail?: string;
  hhmm?: string;
}

interface SystemStage {
  code?: string;
  label?: string;
  detail?: string;
}

interface RuntimeHealth {
  code?: string;
  label?: string;
  detail?: string;
  provider_degraded?: boolean;
  pause_new_buy?: boolean;
  week5_intraday_preserved?: boolean;
  week5_empty_signal_triggered?: boolean;
  acceptance_failed?: boolean;
  week5_last_scan_timestamp?: string;
  latest_task_label?: string;
  latest_task_detail?: string;
  latest_task_timestamp?: string;
}

interface PendingTask {
  name?: string;
  label?: string;
  scheduled_time?: string;
  interval_minutes?: number;
}

interface RuntimeSummary {
  mode?: string;
  counts?: Record<string, number>;
  tasks_total?: number;
  pending_next?: PendingTask | null;
}

interface StageTask {
  name?: string;
  label?: string;
  type?: string;
  category?: string;
  status?: string;
  status_label?: string;
  scheduled_time?: string;
  latest_time?: string;
  interval_minutes?: number;
  report_timestamp?: string;
  report_status?: string;
  detail?: string;
  current_hhmm?: string;
  last_run_day?: string;
  last_slot_date?: string;
  last_slot_value?: number;
}

interface MarketWarehouseProgress {
  status?: string;
  phase?: string;
  current_symbol?: string;
  symbols_completed?: number;
  symbols_total?: number;
  progress_ratio?: number;
  updated_at?: string;
}

interface FollowupState {
  updated_at?: string;
  stage?: string;
  status?: string;
  payload?: Record<string, unknown>;
}

interface FollowupResult {
  started_at?: string;
  finished_at?: string;
  ok?: boolean;
  skipped?: boolean;
  reason?: string;
  trigger?: string;
  effective_market_warehouse_trace_id?: string;
  market_warehouse_trace_id?: string;
}

interface ResumeAction {
  available?: boolean;
  reason?: string;
  retry_report_trace_id?: string;
  latest_status?: string;
  latest_timestamp?: string;
  target_trade_date?: string;
  failed_symbols_total?: number;
  failed_symbols_complete?: boolean;
  failed_symbols_sample?: string[];
}

interface WarehouseSyncRunResponse {
  status?: string;
  reason?: string;
  trace_id?: string;
  failed_symbols_total?: number;
}

interface IdleQueueSummary {
  enabled?: boolean;
  auto_run?: boolean;
  blocked_tasks?: number;
  pending_manual_ack?: number;
}

interface SchedulerState {
  last_run?: Record<string, string>;
  last_interval_slot?: Record<string, { date?: string; slot?: number }>;
}

interface RuntimeStageResponse {
  as_of?: string;
  today?: string;
  runtime_phase?: RuntimePhase;
  system_stage?: SystemStage;
  health?: RuntimeHealth;
  summary?: RuntimeSummary;
  tasks?: StageTask[];
  latest_activity?: ActivitySummary | null;
  market_warehouse_progress?: MarketWarehouseProgress | null;
  market_warehouse_followup_state?: FollowupState | null;
  market_warehouse_followup_result?: FollowupResult | null;
  market_warehouse_resume_action?: ResumeAction | null;
  idle_queue?: IdleQueueSummary;
  scheduler_state?: SchedulerState;
}

interface ActivitySummary {
  label: string;
  detail: string;
  timestamp: string;
}

function statusToneClass(status: string | undefined): string {
  switch ((status || '').toLowerCase()) {
    case 'done':
    case 'active':
      return 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good';
    case 'running':
      return 'border-[rgba(65,214,179,0.28)] bg-[rgba(65,214,179,0.10)] text-accent';
    case 'partial':
    case 'pending':
    case 'due':
    case 'expired':
    case 'skipped':
      return 'border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.10)] text-warn';
    case 'failed':
    case 'disabled':
      return 'border-[rgba(255,123,123,0.28)] bg-[rgba(255,123,123,0.10)] text-bad';
    default:
      return 'border-panelBorder bg-[rgba(12,33,48,0.45)] text-muted';
  }
}

function topBannerClass(phaseCode: string | undefined, stageCode: string | undefined): string {
  if ((stageCode || '').toLowerCase() === 'degraded') {
    return 'border-[rgba(255,123,123,0.30)] bg-[rgba(86,25,25,0.20)]';
  }
  if ((stageCode || '').toLowerCase() === 'warn') {
    return 'border-[rgba(255,184,77,0.30)] bg-[rgba(97,60,11,0.20)]';
  }
  if ((phaseCode || '').startsWith('weekend')) {
    return 'border-[rgba(168,139,250,0.26)] bg-[rgba(93,62,143,0.14)]';
  }
  if ((stageCode || '').includes('running') || (stageCode || '').includes('sync')) {
    return 'border-[rgba(65,214,179,0.28)] bg-[rgba(65,214,179,0.10)]';
  }
  return 'border-[rgba(77,136,255,0.24)] bg-[rgba(77,136,255,0.08)]';
}

function formatTaskPlan(task: StageTask): string {
  if (task.type === 'interval') {
    return `${asNumber(task.interval_minutes)} 分钟`;
  }
  return task.scheduled_time || '-';
}

function buildIntervalTimestamp(dateValue: string | undefined, slotValue: number | undefined): string {
  if (!dateValue || typeof slotValue !== 'number' || slotValue < 0) {
    return '';
  }
  const hours = String(Math.floor(slotValue / 60)).padStart(2, '0');
  const minutes = String(slotValue % 60).padStart(2, '0');
  return `${dateValue}T${hours}:${minutes}:00`;
}

function resolveTaskTimestamp(task: StageTask): string {
  if (task.report_timestamp) {
    return task.report_timestamp;
  }
  return buildIntervalTimestamp(task.last_slot_date, task.last_slot_value);
}

function formatTaskReport(task: StageTask): string {
  const timestamp = resolveTaskTimestamp(task);
  return timestamp ? formatDateTime(timestamp) : '-';
}

function countValue(summary: RuntimeSummary | null, key: string): number {
  return asNumber(summary?.counts?.[key] ?? 0);
}

function isWeekendPhase(phaseCode: string | undefined): boolean {
  return (phaseCode || '').startsWith('weekend');
}

function formatNextTask(nextTask: PendingTask | null): string {
  if (!nextTask) {
    return '当前没有待执行任务';
  }
  if (nextTask.scheduled_time) {
    return `${nextTask.label || nextTask.name || '未命名任务'} · ${nextTask.scheduled_time}`;
  }
  if (nextTask.interval_minutes) {
    return `${nextTask.label || nextTask.name || '未命名任务'} · ${nextTask.interval_minutes} 分钟轮询`;
  }
  return nextTask.label || nextTask.name || '当前没有待执行任务';
}

function parseTimestamp(value: string | undefined): number {
  if (!value) {
    return Number.NEGATIVE_INFINITY;
  }
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function resolveLatestActivity(
  tasks: StageTask[],
  progress: MarketWarehouseProgress | null,
  followupState: FollowupState | null,
): ActivitySummary | null {
  let latest: ActivitySummary | null = null;
  let latestValue = Number.NEGATIVE_INFINITY;

  if (progress?.updated_at) {
    latest = {
      label: '基础库增量同步',
      detail: progress.phase ? `阶段：${progress.phase}` : '最近同步进度',
      timestamp: progress.updated_at,
    };
    latestValue = parseTimestamp(progress.updated_at);
  }

  if (followupState?.updated_at) {
    const parsed = parseTimestamp(followupState.updated_at);
    if (parsed > latestValue) {
      latest = {
        label: 'Post-market warehouse followup',
        detail: formatFollowupDetail(followupState),
        timestamp: followupState.updated_at,
      };
      latestValue = parsed;
    }
  }

  for (const task of tasks) {
    const timestamp = resolveTaskTimestamp(task);
    const parsed = parseTimestamp(timestamp);
    if (parsed <= latestValue) {
      continue;
    }
    latestValue = parsed;
    latest = {
      label: task.label || task.name || '未命名任务',
      detail: task.detail || task.status_label || task.status || '已记录执行结果',
      timestamp,
    };
  }

  return latest;
}

function statusOverview(summary: RuntimeSummary | null): string {
  return `完成 ${countValue(summary, 'done')} / 运行 ${countValue(summary, 'running')} / 待执行 ${
    countValue(summary, 'pending') + countValue(summary, 'due')
  } / 禁用 ${countValue(summary, 'disabled')}`;
}

function healthToneClass(code: string | undefined): string {
  switch ((code || '').toLowerCase()) {
    case 'degraded':
      return 'border-[rgba(255,123,123,0.30)] bg-[rgba(86,25,25,0.24)] text-bad';
    case 'warn':
      return 'border-[rgba(255,184,77,0.30)] bg-[rgba(97,60,11,0.22)] text-warn';
    default:
      return 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good';
  }
}

function formatFollowupDetail(followupState: FollowupState | null): string {
  if (!followupState) {
    return '-';
  }
  const parts = [
    followupState.stage,
    followupState.status,
    typeof followupState.payload?.reason === 'string' ? followupState.payload.reason : '',
  ].filter(Boolean);
  return parts.join(' | ') || '-';
}

function describeResumeReason(reason: string | undefined): string {
  switch ((reason || '').toLowerCase()) {
    case 'sync_running':
      return '当前已有更新任务在运行中';
    case 'followup_running':
      return '后续流程仍在运行中';
    case 'failed_symbols_incomplete':
      return '最新失败列表不完整，暂时不能做定向补更';
    case 'no_failed_symbols_to_retry':
      return '当前没有待补更的失败标的';
    case 'latest_report_missing':
      return '暂无可用的最新同步报告';
    default:
      return reason || '当前不可执行补更';
  }
}

export default function RuntimeStagePage() {
  const { data, error, loading, refresh, lastUpdated } = useAutoRefresh<RuntimeStageResponse>(
    () => apiGet<RuntimeStageResponse>('/runtime/stage'),
    [],
    15000,
  );
  const [resumeSubmitting, setResumeSubmitting] = useState(false);
  const [resumeFeedback, setResumeFeedback] = useState('');
  const [resumeError, setResumeError] = useState('');

  const summary = data?.summary ?? null;
  const nextTask = summary?.pending_next ?? null;
  const tasks = data?.tasks ?? [];
  const progress = data?.market_warehouse_progress ?? null;
  const followupState = data?.market_warehouse_followup_state ?? null;
  const followupResult = data?.market_warehouse_followup_result ?? null;
  const resumeAction = data?.market_warehouse_resume_action ?? null;
  const health = data?.health ?? null;
  const idleQueue = data?.idle_queue ?? null;
  const scheduler = data?.scheduler_state ?? null;
  const latestActivity = data?.latest_activity ?? resolveLatestActivity(tasks, progress, followupState);
  const weekendPhase = isWeekendPhase(data?.runtime_phase?.code);
  const schedulerDailyRuns = Object.keys(scheduler?.last_run ?? {}).length;
  const schedulerIntervalRuns = Object.keys(scheduler?.last_interval_slot ?? {}).length;
  const canResume =
    Boolean(resumeAction?.available) &&
    !resumeSubmitting &&
    (progress?.status || '').toLowerCase() !== 'running' &&
    (followupState?.status || '').toLowerCase() !== 'running';

  async function handleResumeMissingUpdates(): Promise<void> {
    if (!resumeAction?.retry_report_trace_id || !canResume) {
      return;
    }
    setResumeSubmitting(true);
    setResumeFeedback('');
    setResumeError('');
    try {
      const requestTraceId = `runtime-stage-resume-${Date.now()}`;
      const response = await apiPost<WarehouseSyncRunResponse>('/warehouse/sync/run', {
        force: false,
        notify_enabled: true,
        source_trace_id: requestTraceId,
        retry_failed_only: true,
        retry_report_trace_id: resumeAction.retry_report_trace_id,
      });
      const statusText = response.status || 'unknown';
      const reasonText = response.reason ? ` | ${response.reason}` : '';
      setResumeFeedback(`补更已发起：${statusText}${reasonText}`);
      await refresh();
    } catch (err) {
      setResumeError(err instanceof Error ? err.message : '继续补更失败');
    } finally {
      setResumeSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="glass-panel relative overflow-hidden border-[rgba(65,214,179,0.22)] bg-gradient-to-br from-[rgba(10,34,53,0.98)] via-[rgba(8,27,44,0.97)] to-[rgba(5,18,31,0.98)] p-6 shadow-[0_0_42px_rgba(65,214,179,0.08)]">
        <div className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full bg-[rgba(65,214,179,0.12)] blur-3xl" />
        <div className="pointer-events-none absolute -left-8 bottom-0 h-32 w-32 rounded-full bg-[rgba(61,123,255,0.10)] blur-3xl" />

        <div className="relative flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full border border-[rgba(65,214,179,0.25)] bg-[rgba(65,214,179,0.08)] px-3 py-1 text-xs tracking-[0.28em] text-accent">
              <Activity className="h-3.5 w-3.5" />
              RUNTIME STAGE
            </div>
            <h1 className="mt-4 flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
              <Clock3 className="h-7 w-7 text-accent" />
              当前系统阶段
            </h1>
            <p className="mt-3 max-w-3xl text-sm leading-7 text-muted">
              这里直接看当前处于盘前、盘中、盘后、夜间还是周末，以及关键定时任务此刻是待执行、运行中、已完成还是禁用。
            </p>
            <div className="mt-5 flex flex-wrap gap-3 text-xs text-muted">
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                接口时间：{formatDateTime(data?.as_of)}
              </div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                页面刷新：{lastUpdated ? formatDateTime(lastUpdated) : '-'}
              </div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">
                运行模式：{summary?.mode || '-'}
              </div>
              {weekendPhase ? (
                <div className="rounded-full border border-[rgba(168,139,250,0.28)] bg-[rgba(168,139,250,0.12)] px-3 py-1.5 text-[rgba(210,195,255,0.95)]">
                  周末阶段已生效
                </div>
              ) : null}
            </div>
            <div
              className={`mt-5 flex flex-wrap gap-3 rounded-2xl border p-4 shadow-[0_0_20px_rgba(8,27,44,0.18)] ${healthToneClass(
                health?.code,
              )}`}
            >
              <div className="min-w-[220px] flex-1">
                <div className="text-xs font-bold tracking-[0.24em] opacity-80">SYSTEM HEALTH</div>
                <div className="mt-2 text-xl font-bold">{health?.label || 'N/A'}</div>
                <div className="mt-2 text-xs leading-6 opacity-90">{health?.detail || '-'}</div>
              </div>
              <div className="min-w-[200px] flex-1 rounded-2xl border border-current/20 bg-[rgba(12,33,48,0.18)] p-3">
                <div className="text-xs opacity-80">LATEST TASK</div>
                <div className="mt-2 text-sm font-bold text-ink">
                  {health?.latest_task_timestamp ? formatDateTime(health.latest_task_timestamp) : '-'}
                </div>
                <div className="mt-1 text-xs opacity-80">
                  {health?.latest_task_label || latestActivity?.label || 'No recent task'}
                </div>
                <div className="mt-1 text-xs opacity-70">
                  {health?.latest_task_detail || latestActivity?.detail || '-'}
                </div>
              </div>
              <div className="min-w-[220px] flex-1 rounded-2xl border border-current/20 bg-[rgba(12,33,48,0.18)] p-3">
                <div className="text-xs opacity-80">WATCH NOTES</div>
                <div className="mt-2 text-sm font-bold text-ink">
                  数据源 {health?.provider_degraded ? '已降级' : '正常'}
                  {' / '}
                  候选池 {health?.week5_intraday_preserved ? '保守保池' : '正常跟踪'}
                </div>
                <div className="mt-1 text-xs opacity-80">
                  {health?.pause_new_buy ? '当前暂停新开仓；' : ''}
                  {health?.week5_empty_signal_triggered ? '存在空信号保护' : '当前无空信号保护'}
                </div>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <div className="text-right text-xs text-muted">
              <div>状态：{loading ? '刷新中' : '已同步'}</div>
              <div className="mt-1">任务总数：{asNumber(summary?.tasks_total)}</div>
            </div>
            <button className="btn-outline" onClick={() => void refresh()}>
              <RefreshCw className="h-4 w-4" />
              刷新
            </button>
            <button className="btn-primary" disabled={!canResume} onClick={() => void handleResumeMissingUpdates()}>
              <RefreshCw className={`h-4 w-4 ${resumeSubmitting ? 'animate-spin' : ''}`} />
              {resumeSubmitting ? '补更提交中' : '继续补更'}
            </button>
            <Link className="btn-primary" to="/ops">
              <ShieldAlert className="h-4 w-4" />
              查看系统日志
            </Link>
          </div>
        </div>

        <div
          className={`relative mt-6 rounded-2xl border p-5 shadow-[0_0_24px_rgba(8,27,44,0.22)] ${topBannerClass(
            data?.runtime_phase?.code,
            health?.code || data?.system_stage?.code,
          )}`}
        >
          <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
            <div className="max-w-3xl">
              <div className="text-xs font-bold tracking-[0.28em] text-muted">
                {weekendPhase ? 'WEEKEND WATCH' : 'RUNTIME WATCH'}
              </div>
              <div className="mt-3 text-2xl font-bold text-ink">
                {data?.system_stage?.label || data?.runtime_phase?.label || '暂无阶段信息'}
              </div>
              <div className="mt-2 text-sm leading-6 text-muted">
                {data?.system_stage?.detail || data?.runtime_phase?.detail || '等待阶段数据返回'}
              </div>
            </div>

            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:min-w-[520px] xl:max-w-[580px]">
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.48)] p-4">
                <div className="text-xs text-muted">当前时间阶段</div>
                <div className="mt-2 text-base font-bold text-ink">
                  {data?.runtime_phase?.label || '-'}
                  {data?.runtime_phase?.hhmm ? ` · ${data.runtime_phase.hhmm}` : ''}
                </div>
                <div className="mt-2 text-xs text-muted">{data?.runtime_phase?.detail || '-'}</div>
              </div>
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.48)] p-4">
                <div className="text-xs text-muted">下一任务</div>
                <div className="mt-2 text-base font-bold text-ink">{formatNextTask(nextTask)}</div>
                <div className="mt-2 text-xs text-muted">
                  {weekendPhase ? '周末优先关注维护、演化和守护任务。' : '按当前阶段挑出最近一条待执行任务。'}
                </div>
              </div>
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.48)] p-4">
                <div className="text-xs text-muted">最近一次任务</div>
                <div className="mt-2 text-base font-bold text-ink">
                  {latestActivity?.label || '暂无最近执行记录'}
                </div>
                <div className="mt-2 text-xs text-muted">
                  {latestActivity ? `${formatDateTime(latestActivity.timestamp)} · ${latestActivity.detail}` : '-'}
                </div>
              </div>
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.48)] p-4">
                <div className="text-xs text-muted">任务概况</div>
                <div className="mt-2 text-base font-bold text-ink">{statusOverview(summary)}</div>
                <div className="mt-2 text-xs text-muted">
                  total={asNumber(summary?.tasks_total)} / daily={schedulerDailyRuns} / interval={schedulerIntervalRuns}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {error ? <div className="glass-panel border-warn p-4 text-sm text-warn">阶段数据加载失败：{error}</div> : null}

      {resumeFeedback ? (
        <div className="glass-panel border-[rgba(77,223,126,0.28)] p-4 text-sm text-good">{resumeFeedback}</div>
      ) : null}
      {resumeError ? (
        <div className="glass-panel border-[rgba(255,123,123,0.30)] p-4 text-sm text-bad">{resumeError}</div>
      ) : null}
      <div className="grid grid-cols-1 gap-6 md:grid-cols-4">
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">时间阶段</div>
          <div className="mt-3 text-3xl font-bold">{data?.runtime_phase?.label || '-'}</div>
          <div className="mt-2 text-xs text-muted">{data?.runtime_phase?.detail || '-'}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">当前系统阶段</div>
          <div className="mt-3 text-2xl font-bold text-accent">{data?.system_stage?.label || '-'}</div>
          <div className="mt-2 text-xs text-muted">{data?.system_stage?.detail || '-'}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">下一任务</div>
          <div className="mt-3 text-2xl font-bold">{nextTask?.label || '暂无'}</div>
          <div className="mt-2 text-xs text-muted">
            {nextTask?.scheduled_time
              ? `计划时间 ${nextTask.scheduled_time}`
              : nextTask?.interval_minutes
                ? `${nextTask.interval_minutes} 分钟轮询`
                : '当前没有待执行任务'}
          </div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">最近一次任务</div>
          <div className="mt-3 text-2xl font-bold">{latestActivity?.label || '暂无'}</div>
          <div className="mt-2 text-xs text-muted">
            {latestActivity ? formatDateTime(latestActivity.timestamp) : '-'}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1.2fr,0.8fr]">
        <div className="glass-panel p-6">
          <div className="mb-4 flex items-center gap-2 text-xl font-bold">
            <Server className="h-5 w-5 text-accent" />
            关键任务状态
          </div>

          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>类别</th>
                  <th>状态</th>
                  <th>计划</th>
                  <th>最近报告</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {tasks.length ? (
                  tasks.map((task) => (
                    <tr key={task.name}>
                      <td>
                        <div className="font-bold text-ink">{task.label || task.name || '-'}</div>
                        <div className="mt-1 text-xs text-muted">{task.name || '-'}</div>
                      </td>
                      <td className="text-muted">{task.category || '-'}</td>
                      <td>
                        <span
                          className={`inline-flex rounded-full border px-3 py-1 text-xs font-bold ${statusToneClass(task.status)}`}
                        >
                          {task.status_label || task.status || '-'}
                        </span>
                      </td>
                      <td className="font-mono text-sm text-muted">{formatTaskPlan(task)}</td>
                      <td className="text-sm text-muted">{formatTaskReport(task)}</td>
                      <td className="max-w-[320px] whitespace-normal text-sm text-muted">{task.detail || '-'}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="py-8 text-center text-muted">
                      {loading ? '阶段任务加载中…' : '暂无任务数据'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-6">
          <div className="glass-panel p-6">
            <div className="mb-4 text-xl font-bold">更新恢复</div>
            <div className="space-y-4 text-sm">
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="flex items-center justify-between">
                  <span className="text-muted">补更状态</span>
                  <span
                    className={`rounded-full border px-3 py-1 text-xs font-bold ${statusToneClass(
                      resumeAction?.available ? 'done' : 'pending',
                    )}`}
                  >
                    {resumeAction?.available ? '可执行' : '待处理'}
                  </span>
                </div>
                <div className="mt-3 text-ink">
                  trace={resumeAction?.retry_report_trace_id || '-'} / failed=
                  {asNumber(resumeAction?.failed_symbols_total)}
                </div>
                <div className="mt-2 text-xs text-muted">
                  {resumeAction?.available
                    ? '会基于最近一次同步报告，仅继续补更未完成的失败标的。'
                    : describeResumeReason(resumeAction?.reason)}
                </div>
                <div className="mt-3">
                  <button
                    className="btn-primary"
                    disabled={!canResume}
                    onClick={() => void handleResumeMissingUpdates()}
                  >
                    <RefreshCw className={`h-4 w-4 ${resumeSubmitting ? 'animate-spin' : ''}`} />
                    {resumeSubmitting ? '补更提交中' : '继续补更缺失标的'}
                  </button>
                </div>
              </div>

              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="flex items-center justify-between">
                  <span className="text-muted">后续流程</span>
                  <span
                    className={`rounded-full border px-3 py-1 text-xs font-bold ${statusToneClass(
                      (followupState?.status || '').toLowerCase() === 'completed'
                        ? 'done'
                        : followupState?.status || (followupResult?.ok ? 'done' : 'pending'),
                    )}`}
                  >
                    {followupState?.status || (followupResult?.ok ? 'completed' : 'idle')}
                  </span>
                </div>
                <div className="mt-3 text-ink">{formatFollowupDetail(followupState)}</div>
                <div className="mt-2 text-xs text-muted">
                  最近结果：{followupResult?.reason || (followupResult?.ok ? 'completed' : '-')}
                </div>
                <div className="mt-2 text-xs text-muted">
                  最近更新：
                  {formatDateTime(
                    followupState?.updated_at || followupResult?.finished_at || followupResult?.started_at,
                  )}
                </div>
              </div>
            </div>
          </div>

          <div className="glass-panel p-6">
            <div className="mb-4 text-xl font-bold">Market Warehouse 进度</div>
            {progress ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">状态</span>
                  <span className={`rounded-full border px-3 py-1 text-xs font-bold ${statusToneClass(progress.status)}`}>
                    {progress.status || '-'}
                  </span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">阶段</span>
                  <span>{progress.phase || '-'}</span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">当前代码</span>
                  <span className="font-mono">{progress.current_symbol || '-'}</span>
                </div>
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted">完成度</span>
                  <span>
                    {asNumber(progress.symbols_completed)} / {asNumber(progress.symbols_total)}
                  </span>
                </div>
                <div>
                  <div className="mb-2 flex items-center justify-between text-xs text-muted">
                    <span>进度条</span>
                    <span>{formatPercent(progress.progress_ratio ?? 0, 1)}</span>
                  </div>
                  <div className="h-3 overflow-hidden rounded-full bg-[rgba(12,33,48,0.65)]">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-accent to-blue-500 transition-all"
                      style={{ width: `${Math.max(0, Math.min(100, asNumber(progress.progress_ratio) * 100))}%` }}
                    />
                  </div>
                </div>
                <div className="text-xs text-muted">最近更新：{formatDateTime(progress.updated_at)}</div>
              </div>
            ) : (
              <div className="text-sm text-muted">当前没有在途同步进度文件。</div>
            )}
          </div>

          <div className="glass-panel p-6">
            <div className="mb-4 text-xl font-bold">守护摘要</div>
            <div className="grid grid-cols-1 gap-3">
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="text-xs text-muted">Idle Queue</div>
                <div className="mt-2 text-sm text-ink">
                  enabled={idleQueue?.enabled ? 'true' : 'false'} / auto_run={idleQueue?.auto_run ? 'true' : 'false'}
                </div>
                <div className="mt-2 text-xs text-muted">
                  blocked={asNumber(idleQueue?.blocked_tasks)} / pending_ack={asNumber(idleQueue?.pending_manual_ack)}
                </div>
              </div>
              <div className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="text-xs text-muted">调度器快照</div>
                <div className="mt-2 text-sm text-ink">
                  daily_last_run={schedulerDailyRuns} / interval_last_slot={schedulerIntervalRuns}
                </div>
                <div className="mt-2 text-xs text-muted">可用于判断今天哪些任务已经跑过。</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
