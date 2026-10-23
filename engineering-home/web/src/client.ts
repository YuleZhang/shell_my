import type { ErrorResponse } from '../../shared/contracts';

export class RequestError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
let csrfToken: string | null = null;
let onUnauthorized: (() => void) | undefined;
export function setSessionToken(token: string | null) { csrfToken = token; }
export function watchUnauthorized(callback: () => void) {
  onUnauthorized = callback;
  return () => { if (onUnauthorized === callback) onUnauthorized = undefined; };
}
export function errorMessage(error: unknown): string {
  return error instanceof RequestError ? `${error.message}（${error.code}）` : '请求失败，请检查连接后重试。';
}
export async function request<T>(path: string, options: { method?: 'GET' | 'POST' | 'PATCH'; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  if (!path.startsWith('/api/') || path.includes('\\')) throw new Error('Only same-origin API paths are allowed');
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  }
  // Origin is supplied by the browser; credentials and requests stay same-origin.
  const response = await fetch(path, { method, headers, credentials: 'same-origin', mode: 'same-origin', redirect: 'error',
    signal: options.signal, ...(method !== 'GET' ? { body: JSON.stringify(options.body ?? {}) } : {}) });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/auth/login') { csrfToken = null; onUnauthorized?.(); }
    const error = (payload as ErrorResponse | null)?.error;
    throw new RequestError(response.status, error?.code ?? 'request_failed', error?.message ?? '服务器请求失败');
  }
  return payload as T;
}
export function safeExternalUrl(url: string | null, protocols = ['http:', 'https:']): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return protocols.includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : undefined;
  } catch { return undefined; }
}
