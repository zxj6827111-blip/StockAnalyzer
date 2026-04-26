import { useCallback, useEffect, useRef, useState } from 'react';

interface AutoRefreshState<T> {
  data: T | null;
  error: string;
  loading: boolean;
  lastUpdated: string;
  refresh: () => Promise<void>;
}

export function useAutoRefresh<T>(
  loader: () => Promise<T>,
  intervalMs = 15000,
): AutoRefreshState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState('');
  const loaderRef = useRef(loader);

  useEffect(() => {
    loaderRef.current = loader;
  });

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const next = await loaderRef.current();
      setData(next);
      setLastUpdated(new Date().toISOString());
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [refresh, intervalMs]);

  return { data, error, loading, lastUpdated, refresh };
}
