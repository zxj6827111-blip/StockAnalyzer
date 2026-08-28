// 后端在返回 /ui 的 index.html 时会把 window.SA_API_TOKEN 内联注入页面
// （见 main.py 的 _render_index_html_with_token），不落 localStorage、不需要
// 用户任何操作。这里读取该全局变量并附加到写操作请求头，配合后端
// _verify_api_auth 的 X-SA-API-Key 校验解锁 POST 端点。
// 声明为 any 而非精确类型：这是运行时注入的全局变量，不参与类型系统。
declare global {
  interface Window {
    SA_API_TOKEN?: string;
  }
}

function apiTokenHeaders(): Record<string, string> {
  const token = typeof window !== 'undefined' ? window.SA_API_TOKEN : undefined;
  return token ? { 'X-SA-API-Key': token } : {};
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    headers: {
      Accept: 'application/json',
    },
  });
  if (!response.ok) {
    throw new Error(`GET ${path} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...apiTokenHeaders(),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`POST ${path} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}
