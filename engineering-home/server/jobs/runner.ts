import { randomUUID } from 'node:crypto';
import { resumeJobSchema, type CollectInput, type GenerateInput, type Job, type JobDiscovery, type Report } from '../../shared/contracts.js';
import type { Config } from '../config.js';
import type { Store } from '../store.js';
import { SafeError, redact, redactData, safeError } from '../security/sanitize.js';
import { configSecrets, type WeeklyEvidence } from '../adapters/weekly-report.js';
import { PROMPT_VERSION, reportInput, type ModelAdapter } from '../adapters/model.js';

export class JobRunner {
  private running = false; private closed = false;
  private current?: Promise<void>; private abort = new AbortController();
  constructor(private config: Config, private store: Store, private evidence: Pick<WeeklyEvidence, 'collect'>, private model: ModelAdapter) {}
  collect(input: CollectInput): Job {
    if (!this.config.reports) throw new SafeError('reports_disabled', 503);
    if (!input.projectIds.every(id => this.config.projectRoots.some(p => p.id === id)) || new Set(input.projectIds).size !== input.projectIds.length) throw new SafeError('project_not_allowed');
    const sanitized = { ...input, fragments: redact(input.fragments, configSecrets(this.config)), previousTodo: redact(input.previousTodo, configSecrets(this.config)) };
    const old = this.store.db.prepare('SELECT id FROM jobs WHERE idem=?').get(`collect:${input.idempotencyKey}`);
    const reportId = old ? this.store.job(old.id as string)!.reportId! : randomUUID();
    const job = this.store.enqueue('collect', input.idempotencyKey, sanitized, reportId); this.kick(); return job;
  }
  generate(reportId: string, input: GenerateInput): Job {
    const old = this.store.db.prepare('SELECT id FROM jobs WHERE idem=?').get(`generate:${input.idempotencyKey}`);
    if (old) {
      const job = this.store.job(old.id as string)!;
      const original = this.store.jobInput<{ input: GenerateInput; report: Report }>(job.id);
      if (job.reportId !== reportId) throw new SafeError('idempotency_conflict', 409);
      if (job.state === 'awaiting_resume') return this.resumeGeneration(job, input);
      if (JSON.stringify(original.input) !== JSON.stringify(input)) throw new SafeError('idempotency_conflict', 409);
      if (job.state === 'queued') this.kick();
      return job;
    }
    const report = this.store.get<Report>(`report:${reportId}`); if (!report) throw new SafeError('report_not_found', 404);
    if (this.store.hasGeneration(reportId)) throw new SafeError('generation_in_progress', 409);
    if (this.store.uncertainGeneration(reportId) && !input.retryUnknown) throw new SafeError('explicit_retry_required', 409);
    const prepared = this.model.prepare(report, input);
    const job = this.store.enqueue('generate', input.idempotencyKey, { input, report: redactData(report, configSecrets(this.config)) }, reportId);
    this.saveGenerationMeta(job.id, input, prepared); this.kick(); return job;
  }
  discover(job: Job): JobDiscovery {
    const saved = job.kind === 'generate' ? this.store.jobInput<{ input: GenerateInput; report: Report }>(job.id) : null;
    const input = saved?.input;
    return { ...job, resultAvailable: job.kind === 'collect' ? !!this.store.get(`report:${job.reportId}`) : !!this.store.get(`generation:${job.id}`),
      resumeAction: job.state === 'awaiting_resume' && job.kind === 'generate' ? 'generate' : job.kind === 'collect' && job.state === 'queued' ? 'collect' : null,
      request: input ? { modelId: input.modelId, accountId: input.accountId, maxOutputTokens: input.maxOutputTokens,
        idempotencyKey: input.idempotencyKey, confirmedInputChars: input.confirmedInputChars, reportRevision: saved!.report.revision } : null,
      generation: this.store.get(`generation_meta:${job.id}`) };
  }
  resume(id: string, body: unknown): Job {
    const input = resumeJobSchema.parse(body), job = this.store.job(id);
    if (!job) throw new SafeError('job_not_found', 404);
    if (job.kind !== input.kind) throw new SafeError('job_kind_mismatch', 409);
    if (input.kind === 'generate') {
      const { kind: _kind, confirmResume: _confirm, ...generation } = input;
      return this.resumeGeneration(job, generation);
    }
    if (job.state !== 'queued') throw new SafeError('job_not_resumable', 409);
    if (!this.config.reports) throw new SafeError('reports_disabled', 503);
    const original = this.store.jobInput<CollectInput>(id);
    if (!original.projectIds.every(project => this.config.projectRoots.some(p => p.id === project))) throw new SafeError('project_not_allowed');
    this.kick(); return job;
  }
  private resumeGeneration(job: Job, input: GenerateInput): Job {
    if (job.state !== 'awaiting_resume') throw new SafeError('job_not_resumable', 409);
    const original = this.store.jobInput<{ input: GenerateInput; report: Report }>(job.id);
    if (input.idempotencyKey !== original.input.idempotencyKey) throw new SafeError('idempotency_conflict', 409);
    if (input.modelId !== original.input.modelId || input.accountId !== original.input.accountId || input.maxOutputTokens !== original.input.maxOutputTokens) throw new SafeError('resume_binding_changed', 409);
    const report = this.store.get<Report>(`report:${job.reportId}`);
    if (!report) throw new SafeError('report_not_found', 404);
    if (this.store.uncertainGeneration(report.id)) throw new SafeError('explicit_retry_required', 409);
    const prepared = this.model.prepare(report, input);
    this.store.replaceJobInput(job.id, { input, report: redactData(report, configSecrets(this.config)) });
    this.saveGenerationMeta(job.id, input, prepared);
    this.store.updateJob(job.id, 'queued'); this.kick(); return this.store.job(job.id)!;
  }
  private saveGenerationMeta(id: string, input: GenerateInput, prepared: ReturnType<ModelAdapter['prepare']>) {
    this.store.set(`generation_meta:${id}`, { modelId: input.modelId, accountId: input.accountId ?? null,
      skillHash: prepared.skillHash, inputHash: prepared.inputHash, promptVersion: PROMPT_VERSION });
  }
  kick() {
    if (this.closed || this.running) return;
    this.running = true;
    this.current = new Promise<void>(resolve => setImmediate(resolve)).then(async () => {
      while (!this.closed) {
        const job = this.store.nextJob(); if (!job) break;
        await this.execute(job);
      }
    }).finally(() => { this.running = false; });
  }
  async idle() { await this.current; }
  async close() { this.closed = true; this.abort.abort(); await this.current; }
  private async execute(job: Job) {
    this.store.updateJob(job.id, 'preparing');
    let sent = false;
    try {
      if (job.kind === 'collect') {
        const existing = this.store.get<Report>(`report:${job.reportId}`);
        if (existing) { this.store.updateJob(job.id, 'succeeded'); return; }
        const input = this.store.jobInput<CollectInput>(job.id);
        const evidence = redactData(await this.evidence.collect(input, this.abort.signal), configSecrets(this.config));
        const now = new Date().toISOString();
        const report: Report = { id: job.reportId!, week: input.week, projectIds: input.projectIds, fragments: input.fragments,
          previousTodo: input.previousTodo, evidence, text: '', revision: 0, createdAt: now, updatedAt: now,
          inputChars: 0, generation: null, warnings: evidence.warnings };
        report.inputChars = reportInput(report, this.config).chars;
        this.store.updateJob(job.id, 'ready'); this.store.saveReport(report);
        this.store.updateJob(job.id, 'succeeded');
      } else {
        const { input, report } = this.store.jobInput<{ input: GenerateInput; report: Report }>(job.id);
        const current = this.store.get<Report>(`report:${report.id}`);
        if (current?.revision !== report.revision) throw new SafeError('report_revision_changed', 409);
        const prepared = this.model.prepare(report, input);
        this.store.updateJob(job.id, 'ready');
        this.store.set(`generation_meta:${job.id}`, { modelId: input.modelId, accountId: input.accountId ?? null,
          skillHash: prepared.skillHash, inputHash: prepared.inputHash, promptVersion: PROMPT_VERSION });
        this.store.updateJob(job.id, 'sending'); sent = true;
        const result = await this.model.send(prepared, input);
        const latest = this.store.get<Report>(`report:${report.id}`)!;
        const generation = { modelId: input.modelId, accountId: input.accountId ?? null,
          skillHash: prepared.skillHash, inputHash: prepared.inputHash, promptVersion: PROMPT_VERSION };
        this.store.set(`generation:${job.id}`, { text: result.text, warnings: result.warnings, generation });
        if (latest.revision !== report.revision) {
          // Preserve user edits made while the request was in flight; result remains durable and recoverable.
          this.store.set(`generation:${job.id}`, { text: result.text, warnings: [...result.warnings, 'Report edited during generation; result not applied'], generation });
        } else {
          this.store.saveReport({ ...latest, text: result.text, generation, revision: latest.revision + 1,
            warnings: [...latest.warnings, ...result.warnings], updatedAt: new Date().toISOString() });
        }
        this.store.updateJob(job.id, 'succeeded');
      }
    } catch (e) {
      this.store.updateJob(job.id, sent ? 'outcome_unknown' : 'failed', sent ? { code: 'outcome_unknown', message: 'Request may have been charged; explicit retry required' } : safeError(e));
    }
  }
}
