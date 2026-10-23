import { createHash } from 'node:crypto';
import { join, resolve } from 'node:path';
import { readFileSync } from 'node:fs';
import { z } from 'zod';
import type { CollectInput, Evidence } from '../../shared/contracts.js';
import type { Config } from '../config.js';
import { assertNoSymlinks } from '../security/files.js';
import { redact, SafeError } from '../security/sanitize.js';
import { runCommand, type CommandRunner } from './process.js';
import type { GitLab } from './gitlab.js';

export function weekBounds(week: string): [string, string] {
  const start = new Date(`${week}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - (start.getUTCDay() + 6) % 7);
  const end = new Date(start); end.setUTCDate(end.getUTCDate() + 7);
  return [start.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
}
const rawSchema = z.object({ week_start: z.string(), week_end: z.string(), commits: z.array(z.object({
  repo: z.string(), sha: z.string().regex(/^[a-f0-9]{40,64}$/), time: z.string().datetime({ offset: true }),
  title: z.string().max(10000), branch: z.string().max(1000).default(''), intent: z.string().max(10000).optional(),
  match: z.enum(['sha', 'mr', 'issue', 'branch', 'time', 'none']).default('none'),
  sessions: z.array(z.object({ context: z.string().max(10000).optional(), match: z.string().optional() })).max(100).default([]),
})).max(10000) });
export function configSecrets(config: Config): string[] {
  return [config.auth.cookieSecret, config.auth.passwordHash, config.gitlab?.token ?? '', ...Object.values(config.gitlab?.headers ?? {}), ...(config.reports?.models.map(m => m.apiKey ?? '') ?? [])];
}
export function filterEvidence(raw: unknown, input: CollectInput, config: Config): Evidence {
  const source = rawSchema.parse(raw), [weekStart, weekEnd] = weekBounds(input.week);
  if (source.week_start !== weekStart || source.week_end !== weekEnd) throw new SafeError('evidence_week_mismatch');
  const projects = config.projectRoots.filter(p => input.projectIds.includes(p.id));
  if (projects.length !== new Set(input.projectIds).size) throw new SafeError('project_not_allowed');
  const clean = (v: string) => redact(v, configSecrets(config)).slice(0, 4000);
  const seen = new Set<string>();
  const commits = source.commits.flatMap(row => {
    const project = projects.find(p => resolve(p.root) === resolve(row.repo));
    if (!project || row.time.slice(0, 10) < weekStart || row.time.slice(0, 10) >= weekEnd) return [];
    const key = `${project.id}:${row.sha}`; if (seen.has(key)) return []; seen.add(key);
    const context = input.includeContext ? clean(row.sessions.map(s => s.context ?? '').filter(Boolean).slice(0, 3).join('\n')) : null;
    return [{ projectId: project.id, sha: row.sha, time: row.time, title: clean(row.title), body: '', branch: clean(row.branch),
      context, match: row.match, mrs: [], mrStatus: 'unverified' as const }];
  });
  if (commits.length > 100) throw new SafeError('evidence_commit_limit');
  return { weekStart, weekEnd, commits, warnings: input.includeContext ? ['Session excerpts are context, not verified performance facts'] : [] };
}
export function loadRules(config: Config): { text: string; hash: string } {
  if (!config.reports) throw new SafeError('reports_disabled', 503);
  const parts = ['references/style-guide.md', 'references/template.md'].map(file => {
    const path = join(config.reports!.skillPath, file); assertNoSymlinks(path);
    const content = readFileSync(path, 'utf8');
    if (Buffer.byteLength(content) > 100000) throw new SafeError('skill_rules_limit');
    return content;
  });
  const text = redact(parts.join('\n\n'), configSecrets(config));
  return { text, hash: createHash('sha256').update(parts.join('\n\n')).digest('hex') };
}
export class WeeklyEvidence {
  constructor(private config: Config, private gitlab?: GitLab, private run: CommandRunner = runCommand) {}
  async collect(input: CollectInput, signal?: AbortSignal): Promise<Evidence> {
    const c = this.config.reports; if (!c) throw new SafeError('reports_disabled', 503);
    if (!input.consentScan) throw new SafeError('scan_consent_required');
    for (const id of input.projectIds) {
      const p = this.config.projectRoots.find(v => v.id === id); if (!p) throw new SafeError('project_not_allowed');
      assertNoSymlinks(p.root);
    }
    const script = join(c.skillPath, 'scripts/weekly_commits.py'); assertNoSymlinks(script); assertNoSymlinks(c.python);
    const stdout = await this.run(c.python, [script, '--week', input.week, '--json', '--offline'],
      { cwd: c.skillPath, maxBytes: c.maxBytes, timeoutMs: c.timeoutMs, signal });
    let raw: unknown; try { raw = JSON.parse(stdout); } catch { throw new SafeError('evidence_invalid_json'); }
    const evidence = filterEvidence(raw, input, this.config);
    for (const commit of evidence.commits) {
      if (signal?.aborted) throw new SafeError('collector_cancelled');
      const project = this.config.projectRoots.find(p => p.id === commit.projectId)!;
      try {
        const body = await this.run('/usr/bin/git', ['-c', 'core.hooksPath=/dev/null', '-C', project.root, 'show', '-s', '--format=%B', '--no-show-signature', commit.sha, '--'], { maxBytes: 20000, timeoutMs: 3000, signal });
        commit.body = redact(body, configSecrets(this.config)).slice(0, 4000);
      } catch { evidence.warnings.push('Some commit bodies unavailable'); }
      if (this.gitlab && this.config.gitlab) {
        try {
          const remote = (await this.run('/usr/bin/git', ['-C', project.root, 'config', '--get', 'remote.origin.url'], { maxBytes: 2000, timeoutMs: 2000, signal })).trim();
          const base = new URL(this.config.gitlab.baseUrl);
          let path: string | null = null;
          try { const u = new URL(remote); if (u.origin === base.origin && !u.username && !u.password) path = u.pathname.slice(1).replace(/\.git$/, ''); } catch {
            const m = /^git@([^:]+):([\w./-]+?)(?:\.git)?$/.exec(remote); if (m?.[1] === base.hostname) path = m[2];
          }
          if (path) {
            const mrs = await this.gitlab.commitMrs(path, commit.sha);
            commit.mrs = mrs.data; commit.mrStatus = mrs.error || mrs.stale ? 'unverified' : mrs.data.length ? 'verified' : 'none';
          }
        } catch { evidence.warnings.push('Some MR associations unverified'); }
      }
    }
    evidence.warnings = [...new Set(evidence.warnings)];
    return evidence;
  }
}
