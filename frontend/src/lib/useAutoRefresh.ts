import { useCallback, useEffect, useState } from 'react';

interface AutoRefreshState<T> {
  data: T | null;
  error: string;
  loading: boolean;
  lastUpdated: string;
  refresh: () => Promise<void>;
}

export function useAutoRefresh<T>(
  loader: () => Promise<T>,
  deps: readonly unknown[] = [],
  intervalMs = 15000,
): AutoRefreshState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const next = await loader();
      setData(next);
      setLastUpdated(new Date().toISOString());
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    } finally {
      setLoading(false);
    }
  }, deps);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [refresh, intervalMs]);

  return { data, error, loading, lastUpdated, refresh };
}
