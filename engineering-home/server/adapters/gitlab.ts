import { z } from 'zod';
import type { MergeRequest, MrFilter, MrPage, Snapshot } from '../../shared/contracts.js';
import { mrFilterSchema } from '../../shared/contracts.js';
import type { Config } from '../config.js';
import type { Store } from '../store.js';
import { Cache } from '../cache.js';
import { redact, SafeError, safeUrl, stableId } from '../security/sanitize.js';
import { fetchJson, fixedUrl, type Fetcher } from './http.js';

const identitySchema = z.object({ id: z.number().int().positive(), bot: z.boolean().optional(), user_type: z.string().optional() });
const mrSchema = z.object({ id: z.number().int(), iid: z.number().int(), project_id: z.number().int(),
  title: z.string(), state: z.string(), source_branch: z.string(), target_branch: z.string(), updated_at: z.string(),
  web_url: z.string().optional(), references: z.object({ full: z.string().optional() }).optional(),
  reviewers: z.array(z.object({ name: z.string().optional(), username: z.string().optional() })).optional(),
  head_pipeline: z.object({ status: z.string().optional() }).nullable().optional(), author: z.object({ id: z.number() }).optional(),
});
export class GitLab {
  private cache: Cache;
  constructor(private config: Config, store: Store, private fetcher: Fetcher) { this.cache = new Cache(store); }
  private settings() { if (!this.config.gitlab) throw new SafeError('gitlab_disabled', 503); return this.config.gitlab; }
  private headers() { const c = this.settings(); return { ...c.headers, 'PRIVATE-TOKEN': c.token, Accept: 'application/json' }; }
  private key(filter: MrFilter) {
    const c = this.settings();
    return stableId('mr', JSON.stringify([c.baseUrl, c.authorId ?? 'self', stableId('identity', c.token), filter]));
  }
  empty(filter: MrFilter): MrPage { return { items: [], page: filter.page, perPage: filter.perPage, hasMore: false, nextPage: null, authorId: null }; }
  read(filter: MrFilter): Snapshot<MrPage> { const c = this.settings(); return this.cache.read(this.key(filter), this.empty(filter), c.ttlMs); }
  async identity(): Promise<number> {
    const c = this.settings();
    const result = await this.cache.refresh(stableId('gitlab_identity', `${c.baseUrl}:${c.token}:${c.authorId}:${c.botBindingExplicit}`), null as number | null, 300000, async () => {
      const { data } = await fetchJson(this.fetcher, fixedUrl(c.baseUrl, '/api/v4/user'), { headers: this.headers() }, c.timeoutMs);
      const user = identitySchema.parse(data);
      const bot = user.bot || ['project_bot', 'service_account', 'alert_bot', 'support_bot'].includes(user.user_type ?? '');
      if (bot && (!c.botBindingExplicit || !c.authorId)) throw new SafeError('bot_author_binding_required', 403);
      if (c.authorId && c.authorId !== user.id && !c.botBindingExplicit) throw new SafeError('author_binding_mismatch', 403);
      return c.authorId ?? user.id;
    });
    if (result.error || result.data === null) throw new SafeError(result.error?.code ?? 'identity_unavailable', 503);
    return result.data;
  }
  toMr(raw: unknown): MergeRequest {
    const item = mrSchema.parse(raw); const c = this.settings();
    const clean = (s: string) => redact(s, [c.token]).slice(0, 1000);
    return { id: item.id, iid: item.iid, projectId: item.project_id,
      project: clean(item.references?.full?.split('!')[0] ?? String(item.project_id)), title: clean(item.title), state: clean(item.state),
      sourceBranch: clean(item.source_branch), targetBranch: clean(item.target_branch), updatedAt: clean(item.updated_at),
      url: safeUrl(item.web_url, new URL(c.baseUrl).origin), reviewers: (item.reviewers ?? []).map(v => clean(v.name ?? v.username ?? '')),
      pipelineStatus: item.head_pipeline?.status ? clean(item.head_pipeline.status) : null };
  }
  async refresh(raw: unknown, force = false): Promise<Snapshot<MrPage>> {
    const filter = mrFilterSchema.parse(raw), c = this.settings();
    return this.cache.refresh(this.key(filter), this.empty(filter), c.ttlMs, async () => {
      const authorId = await this.identity(); const perPage = Math.min(filter.perPage, c.perPageLimit);
      const path = filter.projectId ? `/api/v4/projects/${filter.projectId}/merge_requests` : '/api/v4/merge_requests';
      const url = new URL(fixedUrl(c.baseUrl, path));
      const params: Record<string, string> = { scope: 'all', author_id: String(authorId), state: filter.state,
        page: String(filter.page), per_page: String(perPage), order_by: 'updated_at', sort: 'desc' };
      if (filter.search) params.search = filter.search;
      if (filter.updatedAfter) params.updated_after = `${filter.updatedAfter}T00:00:00Z`;
      if (filter.updatedBefore) params.updated_before = `${filter.updatedBefore}T23:59:59Z`;
      url.search = new URLSearchParams(params).toString();
      const { data, headers } = await fetchJson(this.fetcher, url.toString(), { headers: this.headers() }, c.timeoutMs, 2000000);
      const rawItems = z.array(mrSchema).max(100).parse(data);
      if (rawItems.some(v => v.author && v.author.id !== authorId)) throw new SafeError('upstream_author_mismatch', 502);
      let nextPage: number | null = null;
      const next = headers.get('x-next-page');
      if (next !== null) {
        if (next && (!/^\d+$/.test(next) || Number(next) <= filter.page)) throw new SafeError('invalid_pagination', 502);
        nextPage = next ? Number(next) : null;
      } else {
        const link = headers.get('link') ?? '';
        const m = /<([^>]+)>;\s*rel="?next"?/.exec(link);
        if (m) {
          const u = new URL(m[1]);
          const p = u.searchParams.get('page');
          if (u.origin !== url.origin || u.pathname !== url.pathname || !p || !/^\d+$/.test(p) || Number(p) <= filter.page) throw new SafeError('invalid_pagination', 502);
          nextPage = Number(p);
        } else if (rawItems.length === perPage && !headers.has('x-total-pages')) throw new SafeError('pagination_unavailable', 502);
        else if (Number(headers.get('x-total-pages')) > filter.page) nextPage = filter.page + 1;
      }
      return { items: rawItems.map(v => this.toMr(v)), page: filter.page, perPage, hasMore: nextPage !== null, nextPage, authorId };
    }, force);
  }
  async commitMrs(projectPath: string, sha: string): Promise<Snapshot<{ title: string; url: string; state: string }[]>> {
    const c = this.settings();
    if (!/^[a-f0-9]{40,64}$/.test(sha) || !/^[\w./-]+$/.test(projectPath) || projectPath.includes('..')) throw new SafeError('invalid_commit_reference');
    const key = stableId('commit_mr', `${c.baseUrl}:${c.authorId}:${stableId('auth', c.token)}:${projectPath}:${sha}`);
    return this.cache.refresh(key, [], 3600000, async () => {
      const { data } = await fetchJson(this.fetcher, fixedUrl(c.baseUrl, `/api/v4/projects/${encodeURIComponent(projectPath)}/repository/commits/${sha}/merge_requests`), { headers: this.headers() }, c.timeoutMs);
      return z.array(z.object({ title: z.string(), web_url: z.string(), state: z.string() })).parse(data).flatMap(v => {
        const url = safeUrl(v.web_url, new URL(c.baseUrl).origin);
        return url ? [{ title: redact(v.title, [c.token]), url, state: redact(v.state, [c.token]) }] : [];
      });
    });
  }
}
