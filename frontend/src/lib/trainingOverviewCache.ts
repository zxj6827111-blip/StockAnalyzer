import { apiGet } from './api';

const TRAINING_OVERVIEW_CACHE_KEY = 'stock-analyzer:training-overview:v1';
const TRAINING_OVERVIEW_CACHE_TTL_MS = 90_000;

interface CacheEnvelope<T> {
  cachedAt: number;
  data: T;
}

let memoryCache: CacheEnvelope<unknown> | null = null;
let inFlightPrefetch: Promise<unknown> | null = null;

function canUseSessionStorage(): boolean {
  return typeof window !== 'undefined' && typeof window.sessionStorage !== 'undefined';
}

function isFreshEnvelope<T>(envelope: CacheEnvelope<T> | null): envelope is CacheEnvelope<T> {
  if (!envelope) {
    return false;
  }
  return Date.now() - envelope.cachedAt <= TRAINING_OVERVIEW_CACHE_TTL_MS;
}

function readEnvelope<T>(): CacheEnvelope<T> | null {
  if (isFreshEnvelope(memoryCache as CacheEnvelope<T> | null)) {
    return memoryCache as CacheEnvelope<T>;
  }
  if (!canUseSessionStorage()) {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(TRAINING_OVERVIEW_CACHE_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as CacheEnvelope<T>;
    if (!isFreshEnvelope(parsed)) {
      window.sessionStorage.removeItem(TRAINING_OVERVIEW_CACHE_KEY);
      return null;
    }
    memoryCache = parsed as CacheEnvelope<unknown>;
    return parsed;
  } catch {
    return null;
  }
}

export function loadTrainingOverviewCache<T>(): T | null {
  return readEnvelope<T>()?.data ?? null;
}

export function saveTrainingOverviewCache<T>(data: T): void {
  const envelope: CacheEnvelope<T> = {
    cachedAt: Date.now(),
    data,
  };
  memoryCache = envelope as CacheEnvelope<unknown>;
  if (!canUseSessionStorage()) {
    return;
  }
  try {
    window.sessionStorage.setItem(TRAINING_OVERVIEW_CACHE_KEY, JSON.stringify(envelope));
  } catch {
    // Ignore sessionStorage quota or privacy-mode failures.
  }
}

export async function prefetchTrainingOverview<T>(): Promise<T | null> {
  const cached = loadTrainingOverviewCache<T>();
  if (cached) {
    return cached;
  }
  if (inFlightPrefetch) {
    return (await inFlightPrefetch) as T;
  }
  inFlightPrefetch = apiGet<T>('/dashboard/training-overview?history_limit=6')
    .then((payload) => {
      saveTrainingOverviewCache(payload);
      return payload;
    })
    .finally(() => {
      inFlightPrefetch = null;
    });
  return (await inFlightPrefetch) as T;
}
