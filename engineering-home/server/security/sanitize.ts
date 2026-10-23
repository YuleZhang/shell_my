import { createHash } from 'node:crypto';
import type { ApiError } from '../../shared/contracts.js';
export class SafeError extends Error {
  constructor(public code: string, public status = 400) { super(code); }
}
export function safeError(error: unknown): ApiError {
  const code = error instanceof SafeError ? error.code : 'integration_failed';
  return { code, message: code.replaceAll('_', ' ') };
}
export const stableId = (prefix: string, value: string) => `${prefix}_${createHash('sha256').update(value).digest('hex').slice(0, 24)}`;
const secretKey = /(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|credential)s?$|(?:apiKey|accessToken|refreshToken|clientSecret)$/i;
export function redact(text: string, secrets: string[] = []): string {
  let out = text;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === 'object') out = JSON.stringify(redactData(parsed, secrets));
  } catch { /* Free-form excerpts are also redacted below. */ }
  for (const s of secrets.filter(Boolean).sort((a, b) => b.length - a.length)) out = out.split(s).join('[REDACTED]');
  const pattern = /(["'])([\w-]+)\1\s*:\s*/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(out))) {
    if (!secretKey.test(match[2])) continue;
    const start = pattern.lastIndex; let end = start, depth = 0, quote = '', escaped = false;
    for (; end < out.length; end++) {
      const c = out[end];
      if (quote) {
        if (escaped) escaped = false;
        else if (c === '\\') escaped = true;
        else if (c === quote) { quote = ''; if (!depth) { end++; break; } }
        continue;
      }
      if (c === '"' || c === "'") { quote = c; continue; }
      if (c === '{' || c === '[') depth++;
      else if (c === '}' || c === ']') { if (!depth) break; depth--; }
      else if ((c === ',' || c === '\n') && !depth) break;
    }
    out = out.slice(0, start) + '"[REDACTED]"' + out.slice(end);
    pattern.lastIndex = start + 12;
  }
  return out.replace(/\b(?:ak-|sk-|glpat-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}\b/g, '[REDACTED]')
    .replace(/\b(Bearer\s+)[^\s"'<>]+/gi, '$1[REDACTED]')
    .replace(/((?:[\w-]*[_-])?(?:api[_-]?key|token|password|passwd|secret|credential)\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,"'<>]+)/gi, '$1[REDACTED]')
    .replace(/https?:\/\/[^\s/@]+:[^\s/@]+@[^\s]+/g, '[REDACTED URL]')
    .replace(/(?:\/home\/[^/\s]+|\/Users\/[^/\s]+)\/[^\s"'<>]+/g, '[PRIVATE PATH]');
}
export function redactData<T>(value: T, secrets: string[] = []): T {
  if (typeof value === 'string') return redact(value, secrets) as T;
  if (Array.isArray(value)) return value.map(v => redactData(v, secrets)) as T;
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, v]) =>
    [key, secretKey.test(key) ? '[REDACTED]' : redactData(v, secrets)])) as T;
  return value;
}
export function safeUrl(value: unknown, origin?: string): string | null {
  if (typeof value !== 'string' || [...value].some(c => c.charCodeAt(0) <= 32 || c === '\\')) return null;
  try {
    const u = new URL(value);
    if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password || (origin && u.origin !== origin)) return null;
    u.search = ''; u.hash = '';
    return u.toString();
  } catch { return null; }
}
