export function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

export function cleanDisplayText(value: unknown): string {
  const text = String(value ?? '').trim();
  if (!text) {
    return '';
  }
  return ['nan', 'null', 'none', 'undefined'].includes(text.toLowerCase()) ? '' : text;
}

export function formatNumber(value: unknown, digits = 2): string {
  return asNumber(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPercent(value: unknown, digits = 1): string {
  return `${(asNumber(value) * 100).toFixed(digits)}%`;
}

export function formatDateTime(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    return '-';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', {
    hour12: false,
  });
}

export function formatTimeShort(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) {
    return '--:--';
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

export function formatBoolZh(value: unknown): string {
  return Boolean(value) ? '是' : '否';
}

export function truncateText(value: unknown, limit = 42): string {
  const text = String(value ?? '').trim();
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, Math.max(1, limit - 1))}…`;
}
