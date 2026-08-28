// 后端在返回 /ui 的 index.html 时会把 window.SA_API_TOKEN 内联注入页面
// （见 main.py 的 _render_index_html_with_token），不落 localStorage、不需要
// 用户任何操作。这里读取该全局变量并附加到请求头，配合后端
// _verify_api_auth 的 X-SA-API-Key 校验解锁受保护端点。
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
      // GET 也必须带鉴权头：虽然大多数 GET 端点是公开只读的，但有 9 个
      // 显式挂了 Depends(get_verify_api_auth())，不带头会直接 401 ——
      // /tasks 与 /tasks/{task_id}（回测等 202 异步任务的轮询接口，缺了它
      // 前端无法自动展示结果）、/week5/{night-scan,auction,market-radar,
      // live-runtime}/latest、/week5/candidate-state、/learning/weekend/latest、
      // /settings/blacklist。token 已注入页面，对同源 GET 一律带上不会造成
      // 额外泄露；公开端点收到未声明的请求头也会被 FastAPI 忽略，无副作用。
      // 因此这里统一附加，而不是维护一份"哪些 GET 需要鉴权"的名单。
      ...apiTokenHeaders(),
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
