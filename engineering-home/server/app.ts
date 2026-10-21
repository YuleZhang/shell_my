import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import { ZodError } from 'zod';
import { configSchema, validateConfig, type Config } from './config.js';
import { Store } from './store.js';
import { installAuth } from './security/auth.js';
import { SafeError, safeError, redact, redactData } from './security/sanitize.js';
import { Accounts } from './adapters/stepcode.js';
import { GitLab } from './adapters/gitlab.js';
import { Services } from './adapters/services.js';
import { WeeklyEvidence, configSecrets } from './adapters/weekly-report.js';
import { ModelAdapter, reportView } from './adapters/model.js';
import { JobRunner } from './jobs/runner.js';
import type { CommandRunner } from './adapters/process.js';
import type { Fetcher } from './adapters/http.js';
import { idSchema, mrFilterSchema, accountRefreshSchema, collectSchema, generateSchema, reportPatchSchema,
  type Capabilities, type Report, type Evidence, type SessionResponse } from '../shared/contracts.js';

export type Backend = { runner: JobRunner; accounts: Accounts; gitlab: GitLab; services: Services; capabilities: Capabilities };
declare module 'fastify' { interface FastifyInstance { backend: Backend } }
export type AppOptions = {
  fetcher?: Fetcher; readKeys?: () => string; run?: CommandRunner;
  collectEvidence?: (input: ReturnType<typeof collectSchema.parse>, signal?: AbortSignal) => Promise<Evidence>;
  procRead?: (path: string) => string; staticRoot?: string; startJobs?: boolean;
};
export async function buildApp(input: Config, store: Store, options: AppOptions = {}) {
  const config = validateConfig(input);
  const app = Fastify({ logger: false, bodyLimit: 150000, trustProxy: false, requestTimeout: 15000 });
  app.setErrorHandler((error, _req, reply) => {
    const bodyLimit = (error as { statusCode?: number })?.statusCode === 413;
    const status = error instanceof SafeError ? error.status : error instanceof ZodError ? 400 : bodyLimit ? 413 : 500;
    const errorDto = error instanceof ZodError ? { code: 'invalid_request', message: 'Invalid request' } : bodyLimit ? { code: 'body_limit', message: 'Request body too large' } : safeError(error);
    reply.code(status).send({ error: errorDto });
  });
  app.addHook('onSend', async (_req, reply, payload) => {
    reply.header('Cache-Control', 'no-store').header('X-Content-Type-Options', 'nosniff')
      .header('Referrer-Policy', 'same-origin').header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
    return payload;
  });
  const auth = await installAuth(app, config);
  const fetcher = options.fetcher ?? fetch;
  const accounts = new Accounts(config, store, fetcher, options.readKeys);
  const gitlab = new GitLab(config, store, fetcher);
  const services = new Services(config, store, options.run, options.procRead);
  const evidence = options.collectEvidence ? { collect: options.collectEvidence } : new WeeklyEvidence(config, config.gitlab ? gitlab : undefined, options.run);
  const model = new ModelAdapter(config, accounts, fetcher);
  const runner = new JobRunner(config, store, evidence, model);
  store.recover();
  // Restart recovery is local only. Explicit authenticated submissions resume safe queued work.
  if (options.startJobs === true) runner.kick();
  const capabilities: Capabilities = {
    accounts: !!config.stepcode, mrs: !!config.gitlab, services: config.services.enabled,
    reports: !!config.reports, generation: !!config.reports?.models.length, credentialImport: false,
    projects: config.projectRoots.map(p => ({ id: p.id, label: p.label })),
    models: config.reports?.models.map(m => ({ id: m.id, label: m.label, maxOutputTokens: m.maxOutputTokens, maxInputChars: m.maxInputChars, usesAccount: m.useStepcodeAccount })) ?? [],
    scanNotice: 'Explicit collection scans local Git and existing CLI session directories via the configured weekly script. Only selected allowed project commits are retained; excerpts require separate opt-in. No raw transcripts are saved.',
  };
  app.get('/api/session', async (req): Promise<SessionResponse> => ({ authenticated: true, csrfToken: auth.session(req)!.csrfToken, capabilities }));
  app.get('/api/accounts', async () => ({ accounts: accounts.list() }));
  app.patch<{ Params: { id: string } }>('/api/accounts/:id', async req => ({ account: accounts.patch(idSchema.parse(req.params.id), req.body) }));
  app.post('/api/accounts/refresh', async req => { const input = accountRefreshSchema.parse(req.body ?? {}); return { accounts: await accounts.refresh(input.id, input.force) }; });
  app.get('/api/mrs', async req => gitlab.read(mrFilterSchema.parse(req.query)));
  app.post('/api/mrs/refresh', async req => gitlab.refresh(req.body ?? {}, true));
  app.get('/api/services', async () => services.read());
  app.post('/api/services/refresh', async () => services.refresh(true));
  app.patch<{ Params: { id: string } }>('/api/services/:id', async req => ({ service: services.patch(idSchema.parse(req.params.id), req.body) }));
  app.get('/api/reports', async () => ({ reports: redactData(store.list<Report>('report:'), configSecrets(config)).sort((a, b) => b.createdAt.localeCompare(a.createdAt)).map(r => ({
    id: r.id, week: r.week, projectIds: r.projectIds, revision: r.revision, createdAt: r.createdAt, updatedAt: r.updatedAt, generated: r.generation !== null, warnings: r.warnings,
  })) }));
  app.post('/api/reports/collect', async (req, reply) => { const job = runner.collect(collectSchema.parse(req.body)); reply.code(202); return { job }; });
  app.get('/api/jobs', async () => ({ jobs: store.jobs().map(job => runner.discover(job)) }));
  app.post<{ Params: { id: string } }>('/api/jobs/:id/resume', async (req, reply) => {
    const job = runner.resume(idSchema.parse(req.params.id), req.body); reply.code(202); return { job };
  });
  app.get<{ Params: { id: string } }>('/api/jobs/:id', async req => {
    const id = idSchema.parse(req.params.id), job = store.job(id);
    if (!job) throw new SafeError('job_not_found', 404);
    return { job: runner.discover(job), result: redactData(store.get(`generation:${id}`), configSecrets(config)) };
  });
  app.get<{ Params: { id: string } }>('/api/reports/:id', async req => {
    const report = store.get<Report>(`report:${idSchema.parse(req.params.id)}`);
    if (!report) throw new SafeError('report_not_found', 404);
    return { report: reportView(report, config) };
  });
  app.patch<{ Params: { id: string } }>('/api/reports/:id', async req => {
    const id = idSchema.parse(req.params.id), input = reportPatchSchema.parse(req.body);
    const report = store.get<Report>(`report:${id}`); if (!report) throw new SafeError('report_not_found', 404);
    if (input.revision !== report.revision) throw new SafeError('revision_conflict', 409);
    const updated = { ...redactData(report, configSecrets(config)), text: redact(input.text, configSecrets(config)), revision: report.revision + 1, updatedAt: new Date().toISOString() };
    store.saveReport(updated); return { report: reportView(updated, config) };
  });
  app.post<{ Params: { id: string } }>('/api/reports/:id/generate', async (req, reply) => {
    const job = runner.generate(idSchema.parse(req.params.id), generateSchema.parse(req.body)); reply.code(202); return { job };
  });
  if (options.staticRoot) await app.register(fastifyStatic, { root: options.staticRoot });
  app.addHook('onClose', async () => { await runner.close(); });
  return Object.assign(app, { backend: { runner, accounts, gitlab, services, capabilities } });
}
export { configSchema, Store };
