import { RefreshCw, Shield, Terminal } from 'lucide-react';

import { apiGet, apiPost } from '../lib/api';
import { formatDateTime, truncateText } from '../lib/format';
import { useAutoRefresh } from '../lib/useAutoRefresh';

interface OpsStateResponse {
  mode?: string;
  enabled?: boolean;
  toggle_enabled?: boolean;
  advisory_only?: boolean;
  execution_mode?: string;
}

interface AuditEvent {
  event_id?: string;
  timestamp?: string;
  event_type?: string;
  level?: string;
  payload?: {
    title?: string;
    content?: string;
    reason?: string;
    dedup_key?: string;
  };
}

interface AuditResponse {
  events?: AuditEvent[];
}

interface OpsPageBundle {
  ops: OpsStateResponse;
  audit: AuditResponse;
  suppressed: AuditResponse;
}

export default function SystemOps() {
  const { data, error, loading, refresh, lastUpdated } = useAutoRefresh<OpsPageBundle>(
    async () => {
      const [ops, audit, suppressed] = await Promise.all([
        apiGet<OpsStateResponse>('/dashboard/ops/state'),
        apiGet<AuditResponse>('/audit/events?limit=40'),
        apiGet<AuditResponse>('/audit/events?limit=20&event_type=notification_suppressed'),
      ]);
      return { ops, audit, suppressed };
    },
    [],
    15000,
  );

  async function toggleOps(): Promise<void> {
    if (!data?.ops.toggle_enabled) {
      return;
    }
    await apiPost('/dashboard/ops/toggle', {
      enabled: !data.ops.enabled,
    });
    await refresh();
  }

  const recentEvents = data?.audit.events ?? [];
  const suppressedEvents = data?.suppressed.events ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold font-mono tracking-wide flex items-center gap-3">
            <Terminal className="text-accent w-7 h-7" /> 系统与日志
          </h1>
          <p className="text-muted mt-2">这里直接展示后端操作状态、最近事件，以及被去重压制的重复通知。</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right text-xs text-muted">
            <div>最近刷新：{lastUpdated ? formatDateTime(lastUpdated) : '-'}</div>
            <div>状态：{loading ? '更新中' : '已同步'}</div>
          </div>
          <button className="btn-outline" onClick={() => void refresh()}>
            <RefreshCw className="w-4 h-4" /> 刷新
          </button>
          <button className="btn-primary" onClick={() => void toggleOps()} disabled={!data?.ops.toggle_enabled}>
            <Shield className="w-4 h-4" /> {data?.ops.enabled ? '关闭操作权限' : '开启操作权限'}
          </button>
        </div>
      </div>

      {error ? <div className="glass-panel p-4 border-warn text-warn">加载失败：{error}</div> : null}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">运行模式</div>
          <div className="text-2xl font-bold mt-3">{data?.ops.mode || '-'}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">执行权限</div>
          <div className="text-2xl font-bold mt-3">{data?.ops.enabled ? '已开启' : '已关闭'}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">执行模式</div>
          <div className="text-2xl font-bold mt-3">{data?.ops.execution_mode || '-'}</div>
        </div>
        <div className="glass-panel p-6">
          <div className="text-muted text-sm font-bold tracking-wider">通知去重压制</div>
          <div className="text-2xl font-bold mt-3">{suppressedEvents.length}</div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div className="glass-panel p-6">
          <h2 className="text-xl font-bold mb-4">最近系统事件</h2>
          <div className="space-y-3 max-h-[520px] overflow-y-auto">
            {recentEvents.length ? recentEvents.slice().reverse().map((item) => (
              <div key={item.event_id} className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="flex items-center justify-between gap-4">
                  <div className="font-bold">{item.event_type || 'unknown'}</div>
                  <div className={`text-xs ${item.level === 'warn' ? 'text-warn' : 'text-muted'}`}>{item.level || 'info'}</div>
                </div>
                <div className="text-xs text-muted mt-1">{formatDateTime(item.timestamp)}</div>
                <div className="text-sm mt-2">{truncateText(item.payload?.title ?? item.payload?.content ?? '-', 88)}</div>
              </div>
            )) : <div className="text-muted">暂无事件。</div>}
          </div>
        </div>

        <div className="glass-panel p-6">
          <h2 className="text-xl font-bold mb-4">被压制的重复通知</h2>
          <div className="space-y-3 max-h-[520px] overflow-y-auto">
            {suppressedEvents.length ? suppressedEvents.slice().reverse().map((item) => (
              <div key={item.event_id} className="rounded-xl border border-panelBorder bg-[rgba(12,33,48,0.55)] p-4">
                <div className="font-bold">{item.payload?.title || item.event_type || 'notification_suppressed'}</div>
                <div className="text-xs text-muted mt-1">{formatDateTime(item.timestamp)}</div>
                <div className="text-sm mt-2">原因：{item.payload?.reason || 'unchanged'}</div>
                <div className="text-xs text-muted mt-2 break-all">去重键：{item.payload?.dedup_key || '-'}</div>
              </div>
            )) : <div className="text-muted">暂无被压制的重复通知。</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
