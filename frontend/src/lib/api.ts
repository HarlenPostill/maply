// Minimal fetch wrapper for the local maply API (same origin; proxied in dev).
// Rule: every request WITH a body is sent as application/json (the server
// returns 415 otherwise). Requests without a body send no Content-Type.

export class ApiError extends Error {
  status: number;
  detail: unknown;
  messages: string[];

  constructor(status: number, body: unknown) {
    const messages = extractMessages(status, body);
    super(messages.join('; '));
    this.name = 'ApiError';
    this.status = status;
    this.detail = body;
    this.messages = messages;
  }
}

function extractMessages(status: number, body: unknown): string[] {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return [d];
    if (d && typeof d === 'object' && !Array.isArray(d) && 'errors' in d) {
      const errs = (d as { errors: unknown }).errors;
      if (Array.isArray(errs)) return errs.map((e) => String(e));
    }
    if (Array.isArray(d)) {
      // FastAPI / pydantic validation errors
      return d.map((e) => {
        if (e && typeof e === 'object') {
          const o = e as { loc?: unknown[]; msg?: string };
          const loc = Array.isArray(o.loc) ? o.loc.filter((x) => x !== 'body').join('.') : '';
          return loc ? `${loc}: ${o.msg ?? JSON.stringify(e)}` : o.msg ?? JSON.stringify(e);
        }
        return String(e);
      });
    }
    return [JSON.stringify(d)];
  }
  if (typeof body === 'string' && body.trim()) return [`HTTP ${status}: ${body.slice(0, 300)}`];
  return [`HTTP ${status}`];
}

export function errorMessages(e: unknown): string[] {
  if (e instanceof ApiError) return e.messages;
  if (e instanceof Error) return [e.message];
  return [String(e)];
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const init: RequestInit = { method, headers, credentials: 'same-origin', cache: 'no-store' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new Error(`Cannot reach the local API (${path}): ${e instanceof Error ? e.message : String(e)}`);
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : '';
}
