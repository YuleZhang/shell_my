import { SafeError } from '../security/sanitize.js';
export type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;
export async function fetchJson(fetcher: Fetcher, url: string, init: RequestInit = {}, timeoutMs = 5000, maxBytes = 1000000): Promise<{ data: unknown; headers: Headers }> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(url, { ...init, redirect: 'error', signal: init.signal ? AbortSignal.any([controller.signal, init.signal]) : controller.signal });
    if (!response.ok) throw new SafeError(response.status === 401 || response.status === 403 ? 'upstream_permission_denied' : response.status === 429 ? 'upstream_rate_limited' : 'upstream_failed', 502);
    if (!response.body) throw new SafeError('upstream_invalid_data', 502);
    const reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
    try {
      while (true) { const r = await reader.read(); if (r.done) break; size += r.value.byteLength;
        if (size > maxBytes) { controller.abort(); throw new SafeError('upstream_body_limit', 502); } chunks.push(r.value); }
    } finally { reader.releaseLock(); }
    try { return { data: JSON.parse(Buffer.concat(chunks).toString('utf8')), headers: response.headers }; }
    catch { throw new SafeError('upstream_invalid_data', 502); }
  } catch (e) { if (e instanceof SafeError) throw e; throw new SafeError(controller.signal.aborted ? 'upstream_timeout' : 'upstream_unavailable', 502); }
  finally { clearTimeout(timeout); }
}
export function fixedUrl(base: string, path: string): string { return `${base.replace(/\/$/, '')}${path}`; }
