import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Database,
  GitBranch,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { apiGet } from '../lib/api';
import { asNumber, formatDateTime, formatNumber, formatPercent } from '../lib/format';
import {
  loadTrainingOverviewCache,
  saveTrainingOverviewCache,
} from '../lib/trainingOverviewCache';
import { useAutoRefresh } from '../lib/useAutoRefresh';

type Dict = Record<string, unknown>;

interface TrainingOverviewResponse {
  generated_at?: string;
  bootstrap?: Dict;
  model_artifact?: Dict;
  training_evaluation?: Dict;
  baseline?: Dict;
  acceptance?: Dict;
  evolution?: Dict;
  warehouse?: Dict;
  runtime?: Dict;
}

const backgroundLabels: Record<string, string> = {
  holder_count: '股东户数',
  block_trade_net: '大宗交易净额',
  financing_balance: '融资余额',
  margin_financing_balance: '两融余额',
  northbound_net: '北向资金',
  dragon_tiger_flag: '龙虎榜',
  background_data_complete: '背景完整度',
};

function obj(value: unknown): Dict {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Dict) : {};
}

function arr(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown, fallback = '-'): string {
  const normalized = String(value ?? '').trim();
  return normalized || fallback;
}

function yesNo(value: unknown): string {
  return value ? '是' : '否';
}

function ageLabel(value: unknown): string {
  const hours = asNumber(value, -1);
  if (hours < 0) {
    return '未知';
  }
  if (hours < 1) {
    return `${Math.max(1, Math.round(hours * 60))} 分钟前`;
  }
  if (hours < 48) {
    return `${formatNumber(hours, 1)} 小时前`;
  }
  return `${formatNumber(hours / 24, 1)} 天前`;
}

function statusToneClass(status: unknown): string {
  switch (String(status ?? '').toLowerCase()) {
    case 'pass':
    case 'ok':
    case 'healthy':
    case 'consistent':
      return 'border-[rgba(77,223,126,0.28)] bg-[rgba(77,223,126,0.10)] text-good';
    case 'warn':
    case 'partial':
      return 'border-[rgba(255,184,77,0.28)] bg-[rgba(255,184,77,0.10)] text-warn';
    case 'fail':
    case 'failed':
    case 'degraded':
      return 'border-[rgba(255,123,123,0.28)] bg-[rgba(255,123,123,0.10)] text-bad';
    default:
      return 'border-panelBorder bg-[rgba(12,33,48,0.45)] text-muted';
  }
}

function summaryToneClass(tone: 'good' | 'warn' | 'bad' | 'accent'): string {
  switch (tone) {
    case 'good':
      return 'border-[rgba(77,223,126,0.24)] bg-[rgba(77,223,126,0.10)]';
    case 'warn':
      return 'border-[rgba(255,184,77,0.24)] bg-[rgba(255,184,77,0.10)]';
    case 'bad':
      return 'border-[rgba(255,123,123,0.24)] bg-[rgba(255,123,123,0.10)]';
    default:
      return 'border-[rgba(65,214,179,0.24)] bg-[rgba(65,214,179,0.10)]';
  }
}

function fileLine(payload: Dict): string {
  if (!payload.exists) {
    return '文件不存在';
  }
  return `${text(payload.path)} / ${formatDateTime(payload.updated_at)}`;
}

function joinList(value: unknown, fallback = '-'): string {
  const items = arr(value).map((item) => String(item).trim()).filter(Boolean);
  return items.length ? items.join(' / ') : fallback;
}

function gateOutcomeLabel(status: string): string {
  switch (status) {
    case 'pass':
    case 'ok':
      return '已通过';
    case 'warn':
    case 'partial':
      return '还要继续观察';
    case 'fail':
    case 'failed':
      return '还没达到放行标准';
    default:
      return '还需要人工确认';
  }
}

function acceptanceLabel(status: string): string {
  switch (status) {
    case 'pass':
    case 'ok':
      return '通过';
    case 'warn':
    case 'partial':
      return '有提醒，但不是硬阻塞';
    case 'fail':
    case 'failed':
      return '未通过';
    case '':
    case '-':
      return '暂无结果';
    default:
      return status;
  }
}

function nasRecommendation(windowOverall: string): { title: string; detail: string } {
  switch (windowOverall) {
    case 'pass':
    case 'ok':
      return {
        title: '可以考虑',
        detail: '夜间学习结果已经基本过线，现在更像是确认稳定性，而不是继续救火。',
      };
    case 'warn':
    case 'partial':
      return {
        title: '再观察一下',
        detail: '主链路大体是通的，但还有边缘提醒，最好再多看 1 到 2 个交易日。',
      };
    case 'fail':
    case 'failed':
      return {
        title: '暂不建议',
        detail: '不是不能跑，而是这批学习结果还没达到放心迁回 NAS 的稳定程度。',
      };
    default:
      return {
        title: '先别着急',
        detail: '当前缺少足够明确的放行结论，建议继续在本地观察和补齐信息。',
      };
  }
}

function primaryRiskSummary(params: {
  failCount: number;
  warnCount: number;
  staleSymbols: number;
}): { title: string; detail: string } {
  if (params.failCount > 0) {
    return {
      title: '夜间学习还有阻塞',
      detail: `目前还有 ${params.failCount} 个明确阻塞项，说明这批结果还不够稳定。`,
    };
  }

  if (params.staleSymbols > 0) {
    return {
      title: '少量股票数据偏旧',
      detail: `还有 ${params.staleSymbols} 只股票不是最新交易日，建议顺手补齐。`,
    };
  }

  if (params.warnCount > 0) {
    return {
      title: '有提醒但不算严重',
      detail: `当前有 ${params.warnCount} 个提醒项，先观察是否会连续出现。`,
    };
  }

  return {
    title: '暂无明显风险',
    detail: '主链路当前没有看到会立刻影响本地验证的阻塞问题。',
  };
}

function readinessSummary(windowOverall: string, bootstrapCompleted: boolean): {
  title: string;
  detail: string;
  tone: 'good' | 'warn' | 'bad' | 'accent';
} {
  if (!bootstrapCompleted) {
    return {
      title: '现在还不适合长期依赖',
      detail: '基础训练还没完成，先不要把它当成稳定版本。',
      tone: 'bad',
    };
  }
  if (windowOverall === 'fail') {
    return {
      title: '本地可以继续观察，但还不建议回 NAS',
      detail: '系统能跑，但演化门控没通过，说明还有稳定性问题没有完全解决。',
      tone: 'warn',
    };
  }
  if (windowOverall === 'warn') {
    return {
      title: '本地基本可用，但还需要再观察',
      detail: '主链路是通的，不过还存在一些边缘风险，适合继续本地观察。',
      tone: 'accent',
    };
  }
  return {
    title: '本地运行整体正常',
    detail: '训练、演化和数据覆盖都处于比较健康的状态，可以继续稳定验证。',
    tone: 'good',
  };
}

function modelSummary(bootstrapAgeHours: number): { title: string; detail: string; tone: 'good' | 'warn' } {
  if (bootstrapAgeHours > 24 * 7) {
    return {
      title: '模型能用，但基础训练有点旧',
      detail: '说明现在不是完全没模型，而是完整训练工件已经有一段时间没刷新了。',
      tone: 'warn',
    };
  }
  return {
    title: '模型比较新',
    detail: '基础训练时间不旧，模型工件是近期生成的。',
    tone: 'good',
  };
}

function evolutionSummary(latestAgeHours: number, windowOverall: string): {
  title: string;
  detail: string;
  tone: 'good' | 'warn' | 'bad';
} {
  if (latestAgeHours < 0 || latestAgeHours > 48) {
    return {
      title: '最近没有看到新的例行演化',
      detail: '这通常意味着夜间学习链路没有按预期跑起来，需要先排查调度或任务失败。',
      tone: 'bad',
    };
  }
  if (windowOverall === 'fail') {
    return {
      title: '每天都在跑，但还没完全放行',
      detail: '系统在持续学习，不过门控显示这批结果还不够稳定，仍然偏保守运行。',
      tone: 'warn',
    };
  }
  return {
    title: '每天都在正常学习',
    detail: '最近一次演化是新的，而且门控没有明显阻塞，可以认为学习链路是通的。',
    tone: 'good',
  };
}

function dataSummary(coverageRatio: number, staleSymbols: number): {
  title: string;
  detail: string;
  tone: 'good' | 'warn';
} {
  if (coverageRatio >= 0.998 && staleSymbols <= 10) {
    return {
      title: '全量数据基本齐了',
      detail: '大部分股票已经是最新交易日，只剩极少数需要补齐。',
      tone: 'good',
    };
  }
  return {
    title: '数据大体可用，但还有缺口',
    detail: '说明全量链路已经打通，不过还存在部分股票或字段没有补全。',
    tone: 'warn',
  };
}

function buildActionItems(params: {
  bootstrapAgeHours: number;
  windowOverall: string;
  failCount: number;
  staleSymbols: number;
  latestChecks: unknown[];
}): { priority: string; title: string; detail: string }[] {
  const items: { priority: string; title: string; detail: string }[] = [];

  if (params.windowOverall === 'fail') {
    const artifactIntegrity = arr(params.latestChecks)
      .map((item) => obj(item))
      .find((item) => text(item.name) === 'artifact_integrity');
    items.push({
      priority: '高优先级',
      title: '先处理演化门控失败',
      detail: artifactIntegrity
        ? text(artifactIntegrity.detail)
        : `最近几轮夜间学习里还有 ${params.failCount} 个明确阻塞项，说明这批结果还没达到稳定放行标准。`,
    });
  }

  if (params.bootstrapAgeHours > 24 * 7) {
    items.push({
      priority: '中优先级',
      title: '补一次完整训练与评估',
      detail: '现在基础模型工件已经比较旧，建议重新跑一次完整训练，让基线工件和评估报告都更新到最新。',
    });
  }

  if (params.staleSymbols > 0) {
    items.push({
      priority: '中优先级',
      title: '补齐少量落后的全量股票数据',
      detail: `当前仍有 ${params.staleSymbols} 只股票不是最新交易日，建议补跑这部分增量同步。`,
    });
  }

  if (!items.length) {
    items.push({
      priority: '当前重点',
      title: '继续观察最近 1 到 2 个交易日',
      detail: '当前没有特别突出的阻塞项，建议继续观察训练、演化和背景数据的连续性。',
    });
  }

  return items.slice(0, 3);
}

function buildReasonBullets(params: {
  bootstrapCompleted: boolean;
  bootstrapAgeHours: number;
  latestEvolutionAgeHours: number;
  windowOverall: string;
  coverageRatio: number;
  staleSymbols: number;
  acceptanceOverall: string;
}): string[] {
  const bullets: string[] = [];

  bullets.push(
    params.bootstrapCompleted
      ? `基础训练已经完成，但完整模型工件距离现在约 ${ageLabel(params.bootstrapAgeHours)}。`
      : '基础训练还没有完成，当前版本不适合当作稳定基线。'
  );

  bullets.push(
    params.latestEvolutionAgeHours >= 0
      ? `最近一次夜间演化是 ${ageLabel(params.latestEvolutionAgeHours)}，说明“每天学习”链路是有在跑的。`
      : '最近没有拿到明确的夜间演化结果，需要先确认学习调度是否正常。'
  );

  bullets.push(
    `夜间学习的放行检查目前判断为“${gateOutcomeLabel(params.windowOverall)}”，这比单看“有没有跑起来”更重要，因为它代表这批结果能不能放心继续沿用。`
  );

  bullets.push(
    `全量背景数据覆盖率目前是 ${formatPercent(params.coverageRatio, 2)}，还有 ${params.staleSymbols} 只股票不是最新交易日。`
  );

  if (params.acceptanceOverall) {
    bullets.push(`最近一次验收结论是“${acceptanceLabel(params.acceptanceOverall)}”，说明主链路健康检查现在是有结果可参考的。`);
  }

  return bullets;
}

function DetailRow(props: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-[rgba(108,158,190,0.12)] py-3 text-sm last:border-b-0">
      <div className="text-muted">{props.label}</div>
      <div className="max-w-[60%] text-right text-ink">{props.value}</div>
    </div>
  );
}

export default function LearningOverviewPage() {
  const [cachedData, setCachedData] = useState<TrainingOverviewResponse | null>(() =>
    loadTrainingOverviewCache<TrainingOverviewResponse>(),
  );
  const {
    data: liveData,
    error,
    loading,
    refresh,
    lastUpdated,
  } = useAutoRefresh<TrainingOverviewResponse>(
    () => apiGet<TrainingOverviewResponse>('/dashboard/training-overview?history_limit=6'),
    [],
    30000,
  );
  useEffect(() => {
    if (!liveData) {
      return;
    }
    saveTrainingOverviewCache(liveData);
    setCachedData(liveData);
  }, [liveData]);

  const data = liveData ?? cachedData;
  const initialLoading = loading && !data && !error;
  const refreshingWithCachedData = loading && Boolean(data);

  if (initialLoading) {
    return (
      <div className="space-y-6">
        <div className="glass-panel relative overflow-hidden border-[rgba(65,214,179,0.22)] bg-gradient-to-br from-[rgba(10,34,53,0.98)] via-[rgba(8,27,44,0.97)] to-[rgba(5,18,31,0.98)] p-6 shadow-[0_0_42px_rgba(65,214,179,0.08)]">
          <div className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full bg-[rgba(65,214,179,0.12)] blur-3xl" />
          <div className="pointer-events-none absolute -left-8 bottom-0 h-32 w-32 rounded-full bg-[rgba(61,123,255,0.10)] blur-3xl" />
          <div className="relative flex flex-col gap-6 xl:flex-row xl:items-start xl:justify-between">
            <div className="max-w-4xl">
              <div className="inline-flex items-center gap-2 rounded-full border border-[rgba(65,214,179,0.25)] bg-[rgba(65,214,179,0.08)] px-3 py-1 text-xs tracking-[0.28em] text-accent">
                <Brain className="h-3.5 w-3.5" />
                LEARNING OVERVIEW
              </div>

              <h1 className="mt-4 flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
                <BarChart3 className="h-7 w-7 text-accent" />
                训练与演化总览
              </h1>

              <p className="mt-3 max-w-3xl text-sm leading-7 text-muted">
                正在汇总训练、演化和基础库状态。首次冷启动通常需要几秒钟，之后会优先命中缓存并在后台刷新。
              </p>

              <div className="mt-6 rounded-3xl border border-[rgba(65,214,179,0.24)] bg-[rgba(65,214,179,0.10)] p-5 shadow-[0_0_24px_rgba(8,27,44,0.22)]">
                <div className="flex items-start gap-4">
                  <div className="mt-1 rounded-2xl border border-current/20 bg-[rgba(12,33,48,0.20)] p-3 text-accent">
                    <RefreshCw className="h-5 w-5 animate-spin" />
                  </div>
                  <div className="flex-1">
                    <div className="text-xs font-bold tracking-[0.24em] opacity-80">正在加载</div>
                    <div className="mt-2 text-2xl font-bold text-ink">后台正在生成训练总览</div>
                    <div className="mt-3 text-sm leading-7 opacity-90">
                      这一步会聚合训练工件、演化窗口、仓库覆盖率和运行阶段，先展示明确的加载状态，不再先落到默认占位值。
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <button className="btn-outline" onClick={() => void refresh()}>
                <RefreshCw className="h-4 w-4 animate-spin" />
                加载中
              </button>
              <Link className="btn-outline" to="/runtime-stage">
                <Activity className="h-4 w-4" />
                运行阶段
              </Link>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const bootstrap = obj(data?.bootstrap);
  const modelArtifact = obj(data?.model_artifact);
  const modelMetrics = obj(modelArtifact.training_metrics);
  const trainingEval = obj(data?.training_evaluation);
  const strictRegime = obj(trainingEval.strict_temporal);
  const strictMetrics = obj(strictRegime.metrics);
  const legacyRegime = obj(trainingEval.legacy_validation_only);
  const legacyMetrics = obj(legacyRegime.metrics);
  const baseline = obj(data?.baseline);
  const baselineModelStatus = obj(baseline.model_status);
  const walkForward = obj(baseline.walk_forward_summary);
  const acceptance = obj(data?.acceptance);
  const evolution = obj(data?.evolution);
  const latestEvolution = obj(evolution.latest);
  const latestM9 = obj(latestEvolution.m9);
  const runtimeControls = obj(latestEvolution.runtime_controls);
  const latestModules = obj(latestEvolution.modules);
  const m2 = obj(latestModules.m2);
  const m5 = obj(latestModules.m5);
  const m10 = obj(latestModules.m10);
  const m11 = obj(latestModules.m11);
  const latestMarketSync = obj(latestEvolution.market_sync);
  const evolutionWindow = obj(evolution.window);
  const recentRuns = arr(evolution.recent_runs);
  const warehouse = obj(data?.warehouse);
  const background = obj(warehouse.background);
  const backgroundFields = Object.entries(obj(background.fields));
  const runtime = obj(data?.runtime);
  const runtimePhase = obj(runtime.phase);
  const runtimeHealth = obj(runtime.health);
  const runtimeNextTask = obj(runtime.next_task);
  const latestActivity = obj(runtime.latest_activity);
  const loaderInputs = arr(latestEvolution.loader_inputs);

  const coverageRatio = asNumber(background.latest_trade_date_coverage_ratio);
  const staleSymbols = asNumber(background.symbols_stale);
  const bootstrapAgeHours = asNumber(bootstrap.last_bootstrap_age_hours, -1);
  const latestEvolutionAgeHours = asNumber(latestEvolution.age_hours, -1);
  const windowOverall = text(evolutionWindow.overall, '');
  const acceptanceOverall = text(acceptance.overall, '');
  const failCount = asNumber(obj(evolutionWindow.summary).fail_count);
  const warnCount = asNumber(obj(evolutionWindow.summary).warn_count);

  const readiness = readinessSummary(windowOverall, Boolean(bootstrap.completed));
  const modelView = modelSummary(bootstrapAgeHours);
  const evolutionView = evolutionSummary(latestEvolutionAgeHours, windowOverall);
  const dataView = dataSummary(coverageRatio, staleSymbols);
  const actions = buildActionItems({
    bootstrapAgeHours,
    windowOverall,
    failCount,
    staleSymbols,
    latestChecks: arr(evolutionWindow.checks),
  });
  const nasView = nasRecommendation(windowOverall);
  const riskView = primaryRiskSummary({ failCount, warnCount, staleSymbols });
  const reasons = buildReasonBullets({
    bootstrapCompleted: Boolean(bootstrap.completed),
    bootstrapAgeHours,
    latestEvolutionAgeHours,
    windowOverall,
    coverageRatio,
    staleSymbols,
    acceptanceOverall,
  });

  return (
    <div className="space-y-6">
      <div className="glass-panel relative overflow-hidden border-[rgba(65,214,179,0.22)] bg-gradient-to-br from-[rgba(10,34,53,0.98)] via-[rgba(8,27,44,0.97)] to-[rgba(5,18,31,0.98)] p-6 shadow-[0_0_42px_rgba(65,214,179,0.08)]">
        <div className="pointer-events-none absolute -right-12 -top-12 h-40 w-40 rounded-full bg-[rgba(65,214,179,0.12)] blur-3xl" />
        <div className="pointer-events-none absolute -left-8 bottom-0 h-32 w-32 rounded-full bg-[rgba(61,123,255,0.10)] blur-3xl" />

        <div className="relative flex flex-col gap-6 xl:flex-row xl:items-start xl:justify-between">
          <div className="max-w-4xl">
            <div className="inline-flex items-center gap-2 rounded-full border border-[rgba(65,214,179,0.25)] bg-[rgba(65,214,179,0.08)] px-3 py-1 text-xs tracking-[0.28em] text-accent">
              <Brain className="h-3.5 w-3.5" />
              LEARNING OVERVIEW
            </div>

            <h1 className="mt-4 flex items-center gap-3 font-mono text-3xl font-bold tracking-wide">
              <BarChart3 className="h-7 w-7 text-accent" />
              训练与演化总览
            </h1>

            <p className="mt-3 max-w-3xl text-sm leading-7 text-muted">
              这个页面现在优先告诉你“结论”，而不是先丢一堆参数。你只需要先看能不能继续用、为什么还不能回 NAS、接下来最该处理什么。
            </p>

            <div className="mt-5 flex flex-wrap gap-3 text-xs text-muted">
              {refreshingWithCachedData ? (
                <div className="rounded-full border border-[rgba(65,214,179,0.22)] bg-[rgba(65,214,179,0.08)] px-3 py-1.5 text-accent">
                  缓存已显示，后台刷新中
                </div>
              ) : null}
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">页面刷新：{lastUpdated ? formatDateTime(lastUpdated) : '-'}</div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">接口生成：{formatDateTime(data?.generated_at)}</div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">当前阶段：{text(runtimePhase.label)}</div>
              <div className="rounded-full border border-panelBorder bg-[rgba(12,33,48,0.55)] px-3 py-1.5">全量覆盖：{formatPercent(coverageRatio, 2)}</div>
            </div>

            <div className={`mt-6 rounded-3xl border p-5 shadow-[0_0_24px_rgba(8,27,44,0.22)] ${summaryToneClass(readiness.tone)}`}>
              <div className="flex items-start gap-4">
                <div className="mt-1 rounded-2xl border border-current/20 bg-[rgba(12,33,48,0.20)] p-3">
                  <Sparkles className="h-5 w-5" />
                </div>
                <div className="flex-1">
                  <div className="text-xs font-bold tracking-[0.24em] opacity-80">一句话判断</div>
                  <div className="mt-2 text-2xl font-bold text-ink">{readiness.title}</div>
                  <div className="mt-3 text-sm leading-7 opacity-90">{readiness.detail}</div>
                </div>
              </div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <button className="btn-outline" onClick={() => void refresh()}>
              <RefreshCw className="h-4 w-4" />
              刷新
            </button>
            <Link className="btn-outline" to="/runtime-stage">
              <Activity className="h-4 w-4" />
              运行阶段
            </Link>
            <Link className="btn-primary" to="/ops">
              <ShieldCheck className="h-4 w-4" />
              系统运维
            </Link>
          </div>
        </div>
      </div>

      {error ? <div className="glass-panel border-warn p-4 text-sm text-warn">总览数据加载失败：{error}</div> : null}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className={`glass-panel p-6 ${summaryToneClass(modelView.tone)}`}>
          <div className="text-sm font-bold tracking-wider text-muted">1. 模型现在怎么样</div>
          <div className="mt-3 text-2xl font-bold text-ink">{modelView.title}</div>
          <div className="mt-3 text-sm leading-7 text-muted">{modelView.detail}</div>
          <div className="mt-4 rounded-2xl border border-current/15 bg-[rgba(12,33,48,0.20)] p-4 text-sm">
            <div>基础训练：{bootstrap.completed ? '已完成' : '未完成'}</div>
            <div className="mt-2">最近完整训练：{formatDateTime(bootstrap.last_bootstrap_at)}</div>
            <div className="mt-2">距离现在：{ageLabel(bootstrap.last_bootstrap_age_hours)}</div>
          </div>
        </div>

        <div className={`glass-panel p-6 ${summaryToneClass(evolutionView.tone)}`}>
          <div className="text-sm font-bold tracking-wider text-muted">2. 系统每天有没有继续学习</div>
          <div className="mt-3 text-2xl font-bold text-ink">{evolutionView.title}</div>
          <div className="mt-3 text-sm leading-7 text-muted">{evolutionView.detail}</div>
          <div className="mt-4 rounded-2xl border border-current/15 bg-[rgba(12,33,48,0.20)] p-4 text-sm">
            <div>最近一次演化：{formatDateTime(latestEvolution.timestamp)}</div>
            <div className="mt-2">距离现在：{ageLabel(latestEvolution.age_hours)}</div>
            <div className="mt-2">当前运行模式：{runtimeControls.degraded_mode ? '保守模式' : '常规模式'}</div>
          </div>
        </div>

        <div className={`glass-panel p-6 ${summaryToneClass(dataView.tone)}`}>
          <div className="text-sm font-bold tracking-wider text-muted">3. 数据是不是基本齐了</div>
          <div className="mt-3 text-2xl font-bold text-ink">{dataView.title}</div>
          <div className="mt-3 text-sm leading-7 text-muted">{dataView.detail}</div>
          <div className="mt-4 rounded-2xl border border-current/15 bg-[rgba(12,33,48,0.20)] p-4 text-sm">
            <div>覆盖率：{formatPercent(coverageRatio, 2)}</div>
            <div className="mt-2">最新交易日股票数：{formatNumber(background.symbols_on_latest_trade_date, 0)} / {formatNumber(background.symbols_total, 0)}</div>
            <div className="mt-2">仍需补齐：{formatNumber(staleSymbols, 0)} 只股票</div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1.1fr,0.9fr]">
        <div className="glass-panel p-6">
          <div className="mb-4 flex items-center gap-2 text-xl font-bold">
            <CheckCircle2 className="h-5 w-5 text-accent" />
            为什么会得到这个结论
          </div>

          <div className="space-y-3">
            {reasons.map((reason, index) => (
              <div
                key={`${reason}-${index}`}
                className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4 text-sm leading-7 text-muted"
              >
                <div className="flex items-start gap-3">
                  <div className="mt-1 rounded-full border border-[rgba(65,214,179,0.20)] bg-[rgba(65,214,179,0.10)] p-1.5 text-accent">
                    <ChevronRight className="h-3.5 w-3.5" />
                  </div>
                  <div>{reason}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="glass-panel p-6">
          <div className="mb-4 flex items-center gap-2 text-xl font-bold">
            <AlertTriangle className="h-5 w-5 text-warn" />
            接下来最值得先做的 3 件事
          </div>

          <div className="space-y-4">
            {actions.map((item) => (
              <div key={item.title} className="rounded-2xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-xs tracking-[0.2em] text-warn">{item.priority}</div>
                    <div className="mt-2 text-lg font-bold text-ink">{item.title}</div>
                  </div>
                </div>
                <div className="mt-3 text-sm leading-7 text-muted">{item.detail}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-4">
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">本地能不能继续跑</div>
          <div className="mt-3 text-3xl font-bold text-ink">{bootstrap.completed ? '能' : '不能'}</div>
          <div className="mt-2 text-xs text-muted">当前基础模型已经存在，运行并未被训练门槛挡住。</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">现在适不适合回 NAS</div>
          <div className="mt-3 text-3xl font-bold text-ink">{nasView.title}</div>
          <div className="mt-2 text-xs text-muted">{nasView.detail}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">今天主要风险</div>
          <div className="mt-3 text-3xl font-bold text-ink">{riskView.title}</div>
          <div className="mt-2 text-xs text-muted">{riskView.detail}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-sm font-bold tracking-wider text-muted">当前运行健康</div>
          <div className="mt-3 text-3xl font-bold text-ink">{text(runtimeHealth.label)}</div>
          <div className="mt-2 text-xs text-muted">{text(runtimeHealth.detail)}</div>
        </div>
      </div>

      <details className="glass-panel p-6">
        <summary className="cursor-pointer list-none text-xl font-bold text-ink">
          技术明细
          <span className="ml-3 text-sm font-normal text-muted">排查问题时再展开看</span>
        </summary>

        <div className="mt-6 grid grid-cols-1 gap-6 xl:grid-cols-[1.05fr,0.95fr]">
          <div className="space-y-6">
            <div className="rounded-3xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-5">
              <div className="mb-3 flex items-center gap-2 text-lg font-bold">
                <Brain className="h-5 w-5 text-accent" />
                训练与模型
              </div>
              <DetailRow label="训练评估文件" value={fileLine(trainingEval)} />
              <DetailRow label="模型工件文件" value={fileLine(modelArtifact)} />
              <DetailRow label="Strict Temporal" value={`acc ${formatPercent(strictMetrics.accuracy, 1)} / auc ${formatNumber(strictMetrics.auc, 3)}`} />
              <DetailRow label="Legacy Validation" value={`acc ${formatPercent(legacyMetrics.accuracy, 1)} / auc ${formatNumber(legacyMetrics.auc, 3)}`} />
              <DetailRow label="工件训练指标" value={`accuracy ${formatPercent(modelMetrics.accuracy, 1)} / auc ${formatNumber(modelMetrics.auc, 3)}`} />
              <DetailRow label="Baseline 装载状态" value={`predictor=${text(baselineModelStatus.predictor_mode)} / degraded=${yesNo(baselineModelStatus.degraded_model_mode)}`} />
              <DetailRow label="Walk Forward" value={`folds ${formatNumber(walkForward.folds, 0)} / trades ${formatNumber(walkForward.total_trades, 0)} / final_equity ${formatNumber(walkForward.final_equity, 3)}`} />
            </div>

            <div className="rounded-3xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-5">
              <div className="mb-3 flex items-center gap-2 text-lg font-bold">
                <GitBranch className="h-5 w-5 text-accent" />
                最近一次演化
              </div>
              <DetailRow label="Run ID" value={text(latestEvolution.run_id)} />
              <DetailRow label="最近一次演化时间" value={`${formatDateTime(latestEvolution.timestamp)} / ${ageLabel(latestEvolution.age_hours)}`} />
              <DetailRow label="M9 状态" value={`success=${yesNo(latestM9.success)} / retry_pending=${yesNo(latestM9.retry_pending)}`} />
              <DetailRow label="运行控制" value={`degraded=${yesNo(runtimeControls.degraded_mode)} / conservative=${yesNo(runtimeControls.conservative_mode)} / regime=${text(runtimeControls.regime_hint)}`} />
              <DetailRow label="M2 / M10 / M11" value={`M2 ${text(m2.active_state)} / M10 ${text(m10.status)} / M11 ${formatNumber(m11.score, 1)}`} />
              <DetailRow label="M5 标签质量" value={`coverage ${formatPercent(m5.label_coverage_ratio, 1)} / alignment ${formatPercent(m5.alignment, 1)}`} />
              <DetailRow label="同步结果" value={`${text(latestMarketSync.status)} / ${formatNumber(latestMarketSync.symbols_completed, 0)} / ${formatNumber(latestMarketSync.symbols_total, 0)}`} />
              <DetailRow label="演化原因" value={joinList(runtimeControls.reasons)} />
            </div>
          </div>

          <div className="space-y-6">
            <div className="rounded-3xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-5">
              <div className="mb-3 flex items-center gap-2 text-lg font-bold">
                <ShieldCheck className="h-5 w-5 text-accent" />
                门控与运行态
              </div>
              <DetailRow label="Week4 验收" value={`${text(acceptance.overall)} / ${formatDateTime(acceptance.timestamp)}`} />
              <DetailRow label="演化窗口" value={`${text(evolutionWindow.overall)} / fail=${formatNumber(obj(evolutionWindow.summary).fail_count, 0)} / warn=${formatNumber(obj(evolutionWindow.summary).warn_count, 0)}`} />
              <DetailRow label="当前阶段" value={text(runtimePhase.label)} />
              <DetailRow label="系统健康" value={text(runtimeHealth.label)} />
              <DetailRow label="下一任务" value={`${text(runtimeNextTask.label)} / ${text(runtimeNextTask.scheduled_time)}`} />
              <DetailRow label="最近活动" value={`${text(latestActivity.label)} / ${formatDateTime(latestActivity.timestamp)}`} />
            </div>

            <div className="rounded-3xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-5">
              <div className="mb-3 flex items-center gap-2 text-lg font-bold">
                <Database className="h-5 w-5 text-accent" />
                全量背景数据
              </div>
              <DetailRow label="覆盖率" value={formatPercent(coverageRatio, 2)} />
              <DetailRow label="最新交易日覆盖" value={`${formatNumber(background.symbols_on_latest_trade_date, 0)} / ${formatNumber(background.symbols_total, 0)}`} />
              <DetailRow label="仍落后股票" value={`${formatNumber(staleSymbols, 0)} 只`} />
              <DetailRow label="非阻塞原因" value={joinList(background.nonblocking_reasons)} />
            </div>

            <div className="rounded-3xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-5">
              <div className="mb-3 flex items-center gap-2 text-lg font-bold">
                <Clock3 className="h-5 w-5 text-accent" />
                每日输入与历史
              </div>
              <div className="space-y-3">
                {loaderInputs.length ? (
                  loaderInputs.map((rawItem, index) => {
                    const item = obj(rawItem);
                    return (
                      <div key={`${text(item.module)}-${index}`} className="rounded-2xl border border-panelBorder bg-[rgba(7,24,36,0.55)] p-4 text-sm">
                        <div className="font-bold text-ink">{text(item.module)}</div>
                        <div className="mt-2 text-muted">{formatNumber(item.records, 0)} records / fresh={yesNo(item.fresh)}</div>
                        <div className="mt-2 text-xs text-muted">{text(item.path)}</div>
                      </div>
                    );
                  })
                ) : (
                  <div className="rounded-2xl border border-panelBorder bg-[rgba(7,24,36,0.55)] p-4 text-sm text-muted">暂无最近输入摘要</div>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="mt-6">
          <div className="mb-3 text-lg font-bold text-ink">最近几次演化</div>
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>Run ID</th>
                  <th>Dry Run</th>
                  <th>M9</th>
                  <th>模式</th>
                  <th>M2 / M10 / M11</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {recentRuns.length ? (
                  recentRuns.map((rawItem, index) => {
                    const item = obj(rawItem);
                    return (
                      <tr key={`${text(item.run_id)}-${index}`}>
                        <td>
                          <div className="font-bold text-ink">{formatDateTime(item.timestamp)}</div>
                          <div className="mt-1 text-xs text-muted">{ageLabel(item.age_hours)}</div>
                        </td>
                        <td className="font-mono text-sm text-muted">{text(item.run_id)}</td>
                        <td>{yesNo(item.dry_run)}</td>
                        <td>
                          <span className={`rounded-full border px-3 py-1 text-xs font-bold ${statusToneClass(item.m9_success ? 'pass' : 'warn')}`}>
                            {item.m9_success ? 'success' : 'review'}
                          </span>
                        </td>
                        <td className="text-muted">{item.degraded_mode ? 'degraded' : 'normal'} / {text(item.regime_hint)}</td>
                        <td className="text-muted">{text(item.m2_state)} / {text(item.m10_status)} / {formatNumber(item.m11_score, 1)}</td>
                        <td className="max-w-[320px] whitespace-normal text-sm text-muted">{joinList(item.runtime_reasons)}</td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={7} className="py-8 text-center text-muted">
                      {loading ? '演化历史加载中…' : '暂无演化历史'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="mt-6">
          <div className="mb-3 text-lg font-bold text-ink">背景字段覆盖</div>
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>字段</th>
                  <th>非空覆盖</th>
                  <th>非零覆盖</th>
                  <th>非空条数</th>
                  <th>非零条数</th>
                </tr>
              </thead>
              <tbody>
                {backgroundFields.length ? (
                  backgroundFields.map(([field, rawSummary]) => {
                    const summary = obj(rawSummary);
                    return (
                      <tr key={field}>
                        <td>
                          <div className="font-bold text-ink">{backgroundLabels[field] || field}</div>
                          <div className="mt-1 text-xs text-muted">{field}</div>
                        </td>
                        <td>{formatPercent(summary.non_null_ratio, 1)}</td>
                        <td>{formatPercent(summary.non_zero_ratio, 1)}</td>
                        <td className="text-muted">{formatNumber(summary.non_null_count, 0)}</td>
                        <td className="text-muted">{formatNumber(summary.non_zero_count, 0)}</td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={5} className="py-8 text-center text-muted">
                      {loading ? '背景覆盖数据加载中…' : '暂无背景覆盖数据'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </details>
    </div>
  );
}
