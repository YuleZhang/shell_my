import { z } from 'zod';

export const idSchema = z.string().regex(/^[a-zA-Z0-9_-]{1,100}$/);
export const dateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(v => {
  const d = new Date(`${v}T00:00:00Z`);
  return Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === v;
}, 'Invalid calendar date');
export const loginSchema = z.object({ password: z.string().min(1).max(512) }).strict();
export const accountPatchSchema = z.object({
  alias: z.string().max(120).optional(), tags: z.array(z.string().max(40)).max(20).optional(),
  notes: z.string().max(4000).optional(), enabled: z.boolean().optional(),
}).strict();
export const accountRefreshSchema = z.object({ id: idSchema.optional(), force: z.boolean().default(false) }).strict();
export const mrFilterSchema = z.object({
  state: z.enum(['opened', 'merged', 'closed', 'all']).default('opened'),
  page: z.coerce.number().int().min(1).max(10000).default(1),
  perPage: z.coerce.number().int().min(1).max(100).default(20),
  projectId: z.coerce.number().int().positive().optional(), search: z.string().max(200).optional(),
  updatedAfter: dateSchema.optional(), updatedBefore: dateSchema.optional(),
}).strict().refine(v => !v.updatedAfter || !v.updatedBefore || v.updatedAfter <= v.updatedBefore, 'Invalid date range');
export const servicePatchSchema = z.object({
  alias: z.string().max(120).optional(), notes: z.string().max(4000).optional(),
  category: z.string().max(80).optional(), favorite: z.boolean().optional(),
  protocol: z.enum(['http', 'https', 'ssh', 'tcp', 'none']).optional(),
  path: z.string().max(500).regex(/^\/(?!\/)[^\s\\?#]*$/).optional(),
}).strict();
export const collectSchema = z.object({
  week: dateSchema, projectIds: z.array(idSchema).min(1).max(30), consentScan: z.literal(true),
  includeContext: z.boolean().default(false), fragments: z.string().max(30000).default(''),
  previousTodo: z.string().max(15000).default(''), idempotencyKey: idSchema,
}).strict();
export const reportPatchSchema = z.object({ text: z.string().max(100000), revision: z.number().int().nonnegative() }).strict();
export const generateSchema = z.object({
  modelId: idSchema, accountId: idSchema.optional(), maxOutputTokens: z.number().int().min(100).max(16000),
  confirmedInputChars: z.number().int().nonnegative(), confirmBudget: z.literal(true),
  retryUnknown: z.boolean().default(false), idempotencyKey: idSchema,
}).strict();

export const resumeJobSchema = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('collect'), confirmResume: z.literal(true) }).strict(),
  generateSchema.extend({ kind: z.literal('generate'), confirmResume: z.literal(true) }).strict(),
]);

export type ApiError = { code: string; message: string };
export type Snapshot<T> = { data: T; updatedAt: string | null; stale: boolean; error: ApiError | null };
export type BudgetBucket = {
  id: string; source: 'usage' | 'budgetPool'; label: string; used: number | null; total: number | null;
  remaining: number | null; currency: string | null; period: string | null; expiresAt: string | null;
};
export type Account = {
  id: string; originalName: string; alias: string; tags: string[]; notes: string; enabled: boolean;
  provider: 'stepcode'; available: boolean; usage: Snapshot<BudgetBucket[]>;
};
export type MergeRequest = {
  id: number; iid: number; projectId: number; project: string; title: string; state: string;
  sourceBranch: string; targetBranch: string; updatedAt: string; url: string | null;
  reviewers: string[]; pipelineStatus: string | null;
};
export type MrPage = { items: MergeRequest[]; page: number; perPage: number; hasMore: boolean; nextPage: number | null; authorId: number | null };
export type Endpoint = { address: string; port: number; transport: 'tcp'; url: string | null; localOnly: boolean; reachability: 'unverified' };
export type Service = {
  id: string; name: string; alias: string; notes: string; category: string; favorite: boolean;
  protocol: 'http' | 'https' | 'ssh' | 'tcp' | 'none'; path: string; sources: string[];
  state: string; endpoints: Endpoint[]; discoveredAt: string;
};
export type EvidenceCommit = {
  projectId: string; sha: string; time: string; title: string; body: string; branch: string;
  context: string | null; match: string; mrs: { title: string; url: string; state: string }[];
  mrStatus: 'verified' | 'unverified' | 'none';
};
export type Evidence = { weekStart: string; weekEnd: string; commits: EvidenceCommit[]; warnings: string[] };
export type Report = {
  id: string; week: string; projectIds: string[]; fragments: string; previousTodo: string;
  evidence: Evidence; text: string; revision: number; createdAt: string; updatedAt: string;
  inputChars: number; generationReadiness?: { ready: boolean; error: ApiError | null };
  warnings: string[]; generation: null | { modelId: string; accountId: string | null; skillHash: string; inputHash: string; promptVersion: string };
};
export type JobState = 'queued' | 'preparing' | 'ready' | 'awaiting_resume' | 'sending' | 'succeeded' | 'failed' | 'outcome_unknown';
export type Job = { id: string; kind: 'collect' | 'generate'; reportId: string | null; state: JobState; createdAt: string; updatedAt: string; error: ApiError | null };
export type JobDiscovery = Job & {
  resultAvailable: boolean; resumeAction: 'collect' | 'generate' | null;
  request: null | Pick<GenerateInput, 'modelId' | 'accountId' | 'maxOutputTokens' | 'idempotencyKey'> & { confirmedInputChars: number; reportRevision: number };
  generation: Report['generation'];
};
export type JobsResponse = { jobs: JobDiscovery[] };
export type Capabilities = {
  accounts: boolean; mrs: boolean; services: boolean; reports: boolean; generation: boolean;
  credentialImport: false; projects: { id: string; label: string }[];
  models: { id: string; label: string; maxOutputTokens: number; maxInputChars: number; usesAccount: boolean }[];
  scanNotice: string;
};
export type AccountsResponse = { accounts: Account[] };
export type AccountResponse = { account: Account };
export type ServiceResponse = { service: Service };
export type ReportSummary = Pick<Report, 'id' | 'week' | 'projectIds' | 'revision' | 'createdAt' | 'updatedAt' | 'warnings'> & { generated: boolean };
export type ReportsResponse = { reports: ReportSummary[] };
export type ReportResponse = { report: Report };
export type GenerationResult = { text: string; warnings: string[]; generation: NonNullable<Report['generation']> };
export type JobResponse = { job: Job; result?: GenerationResult | null };
export type JobDiscoveryResponse = { job: JobDiscovery; result: GenerationResult | null };
export type ResumeJobInput = z.infer<typeof resumeJobSchema>;
export type ErrorResponse = { error: ApiError };
export type LoginResponse = { authenticated: true; csrfToken: string };
export type SessionResponse = { authenticated: true; csrfToken: string; capabilities: Capabilities };
export type MrFilter = z.infer<typeof mrFilterSchema>;
export type CollectInput = z.infer<typeof collectSchema>;
export type GenerateInput = z.infer<typeof generateSchema>;
