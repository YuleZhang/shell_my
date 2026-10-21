import { createHash } from 'node:crypto';
import type { Account, BudgetBucket } from '../../shared/contracts.js';
import { accountPatchSchema } from '../../shared/contracts.js';
import type { Config } from '../config.js';
import { KEY_FILE } from '../config.js';
import type { Store } from '../store.js';
import { Cache } from '../cache.js';
import { readPrivateFile } from '../security/files.js';
import { redact, redactData, SafeError, stableId } from '../security/sanitize.js';
import { configSecrets } from './weekly-report.js';
import { fetchJson, fixedUrl, type Fetcher } from './http.js';

export function parseCredentials(text: string): Map<string, string> {
  const result = new Map<string, string>();
  for (const line of text.split(/\r?\n/)) {
    const m = /^\s*([^#:\s]+)\s*:?\s+(ak-[A-Za-z0-9_-]{10,})\s*(?:#.*)?$/.exec(line);
    if (!m) continue;
    if (result.has(m[1])) throw new SafeError('duplicate_account');
    result.set(m[1], m[2]);
  }
  if (!result.size) throw new SafeError('no_accounts');
  return result;
}
const object = (v: unknown): Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {};
const number = (v: unknown): number | null => typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && /^-?\d+(\.\d+)?$/.test(v) && Number.isFinite(Number(v)) ? Number(v) : null;
const pick = (v: Record<string, unknown>, keys: string[]) => keys.map(k => v[k]).find(x => x !== undefined);
const label = (v: unknown): string | null => typeof v === 'string' ? redact(v).slice(0, 200) : null;
const normalizedBase = (value: string): string => { const url = new URL(value); return `${url.origin}${url.pathname.replace(/\/+$/, '')}`; };
export function parseBuckets(payload: unknown): BudgetBucket[] {
  const root = object(payload);
  if (root.code !== undefined && root.code !== 200) throw new SafeError('usage_schema_unsupported', 502);
  const data = object(root.data ?? payload);
  const out: BudgetBucket[] = [];
  function add(value: unknown, source: BudgetBucket['source'], key: string, depth = 0) {
    if (depth > 5 || out.length > 500) throw new SafeError('usage_bucket_limit', 502);
    if (Array.isArray(value)) { if (value.length > 100) throw new SafeError('usage_bucket_limit', 502); value.forEach((v, i) => add(v, source, `${key}.${i}`, depth + 1)); return; }
    const o = object(value);
    const unlimited = o.unlimited === true;
    const used = number(pick(o, ['used', 'usedBudget', 'used_budget', 'consumed', 'consumedAmount', 'usage', 'amountUsed']));
    const total = unlimited ? null : number(pick(o, ['total', 'totalBudget', 'total_budget', 'limit', 'budget', 'amount', 'totalAmount']));
    const explicit = pick(o, ['remaining', 'remainingBudget', 'remaining_budget', 'balance', 'available', 'amountRemaining']);
    const derived = !unlimited && total !== null && total > 0 && used !== null ? Math.max(0, total - used) : null;
    const remaining = unlimited ? null : explicit !== undefined ? number(explicit) : number(derived);
    if (unlimited || used !== null || total !== null || remaining !== null) {
      out.push({ id: stableId('bucket', `${source}:${key}`), source,
        label: ['channel_name', 'channel_id', 'name', 'label', 'type', 'plan', 'usageType', 'usage_type', 'budgetType'].map(k => label(o[k])).find(v => v?.trim()) ?? key,
        used, total, remaining, currency: label(o.currency), period: label(pick(o, ['reset_cycle', 'usage_type', 'period', 'cycle', 'resetPeriod'])),
        expiresAt: label(pick(o, ['expiresAt', 'expires_at', 'expireTime', 'expiration'])) });
    }
    for (const [k, v] of Object.entries(o)) if (v !== null && typeof v === 'object') add(v, source, `${key}.${k}`, depth + 1);
  }
  for (const k of ['usages', 'usage', 'budgets', 'plans']) if (data[k] !== undefined) add(data[k], 'usage', k);
  for (const k of ['budgetPool', 'budgetPools', 'budget_pool', 'budget_pools', 'budget_pool_usages']) if (data[k] !== undefined) add(data[k], 'budgetPool', k);
  if (!out.length) throw new SafeError('usage_schema_unsupported', 502);
  return out;
}
export class Accounts {
  readonly cache: Cache;
  constructor(private config: Config, private store: Store, private fetcher: Fetcher, private readKeys = () => readPrivateFile(KEY_FILE)) { this.cache = new Cache(store); }
  credentials() { return parseCredentials(this.readKeys()); }
  private usageKey(id: string, key: string): string {
    const endpoint = normalizedBase(this.config.stepcode?.baseUrl ?? 'https://disabled.invalid');
    const fingerprint = createHash('sha256').update(`${endpoint}\0${key}`).digest('hex');
    return `usage:${id}:${fingerprint}`;
  }
  list(): Account[] {
    let entries: Map<string, string>;
    try { entries = this.credentials(); } catch { throw new SafeError('credential_source_unavailable', 503); }
    return [...entries.keys()].map(name => {
      const id = stableId('account', name);
      const meta = redactData(this.store.get<Partial<Account>>(`account:${id}`) ?? {}, [...configSecrets(this.config), ...entries.values()]);
      return { id, originalName: name, alias: meta.alias ?? name, tags: meta.tags ?? [], notes: meta.notes ?? '',
        enabled: meta.enabled ?? true, provider: 'stepcode', available: true,
        usage: this.cache.read(this.usageKey(id, entries.get(name)!), [], this.config.stepcode?.ttlMs ?? 60000) };
    });
  }
  patch(id: string, body: unknown): Account {
    const item = this.list().find(a => a.id === id); if (!item) throw new SafeError('account_not_found', 404);
    const meta = accountPatchSchema.parse(body);
    const secrets = [...configSecrets(this.config), ...this.credentials().values()];
    if (meta.alias !== undefined) meta.alias = redact(meta.alias, secrets);
    if (meta.notes !== undefined) meta.notes = redact(meta.notes, secrets);
    if (meta.tags) meta.tags = meta.tags.map(v => redact(v, secrets));
    this.store.set(`account:${id}`, { ...(this.store.get(`account:${id}`) ?? {}), ...meta });
    return this.list().find(a => a.id === id)!;
  }
  async refresh(id?: string, force = false): Promise<Account[]> {
    const c = this.config.stepcode; if (!c) throw new SafeError('accounts_disabled', 503);
    const items = this.list().filter(a => (!id || a.id === id) && a.enabled);
    if (id && !items.length) throw new SafeError('account_not_found_or_disabled', 404);
    const keys = this.credentials(); const deadline = AbortSignal.timeout(c.batchTimeoutMs); let cursor = 0;
    await Promise.all(Array.from({ length: Math.min(c.concurrency, items.length) }, async () => {
      while (cursor < items.length) {
        const item = items[cursor++]; const key = keys.get(item.originalName)!;
        await this.cache.refresh(this.usageKey(item.id, key), [], c.ttlMs, async () => {
          if (deadline.aborted) throw new SafeError('batch_timeout', 502);
          const { data } = await fetchJson(this.fetcher, fixedUrl(normalizedBase(c.baseUrl), '/desk/v1/stepcode/user/info'),
            { method: 'GET', headers: { Authorization: `Bearer ${key}`, Accept: 'application/json' }, signal: deadline }, c.timeoutMs);
          return redactData(parseBuckets(data), [...configSecrets(this.config), ...keys.values()]);
        }, force);
      }
    }));
    return this.list();
  }
  keyFor(id: string): string {
    const item = this.list().find(a => a.id === id && a.enabled);
    if (!item) throw new SafeError('account_not_found_or_disabled');
    return this.credentials().get(item.originalName)!;
  }
}
