import type { Snapshot } from '../shared/contracts.js';
import { safeError } from './security/sanitize.js';
import type { Store } from './store.js';

export class Cache {
  private inflight = new Map<string, Promise<Snapshot<unknown>>>();
  constructor(private store: Store) {}
  read<T>(key: string, fallback: T, ttl: number): Snapshot<T> {
    const old = this.store.get<Snapshot<T>>(`cache:${key}`);
    if (!old) return { data: fallback, updatedAt: null, stale: true, error: null };
    return { ...old, stale: old.stale || !old.updatedAt || Date.now() - Date.parse(old.updatedAt) >= ttl };
  }
  async refresh<T>(key: string, fallback: T, ttl: number, load: () => Promise<T>, force = false): Promise<Snapshot<T>> {
    const old = this.read(key, fallback, ttl);
    if (!force && !old.stale) return old;
    const pending = this.inflight.get(key); if (pending) return pending as Promise<Snapshot<T>>;
    const job = (async () => {
      let next: Snapshot<T>;
      try { next = { data: await load(), updatedAt: new Date().toISOString(), stale: false, error: null }; }
      catch (e) { next = { ...old, stale: true, error: safeError(e) }; }
      this.store.set(`cache:${key}`, next); return next;
    })();
    this.inflight.set(key, job); try { return await job; } finally { this.inflight.delete(key); }
  }
}
