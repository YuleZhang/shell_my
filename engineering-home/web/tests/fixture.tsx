import { createRoot } from 'react-dom/client';
import { Reports } from '../src/pages/Reports';
import '../src/styles.css';

const generation = { modelId: 'original', accountId: 'bound', skillHash: 'fixture-rules', inputHash: 'fixture-input', promptVersion: 'fixture-v1' };
const params = new URLSearchParams(location.search);
const report = { id: 'report', week: '2026-10-19', projectIds: ['project'], fragments: 'Fixture facts', previousTodo: '',
  evidence: { weekStart: '2026-10-19', weekEnd: '2026-10-25', commits: [], warnings: [] }, text: 'Manual saved report', revision: 7,
  createdAt: '2026-10-19T00:00:00Z', updatedAt: '2026-10-20T00:00:00Z', inputChars: 2345, warnings: [], generation: null,
  generationReadiness: { ready: !params.has('disabled'), error: params.has('disabled') ? { code: 'generation_disabled', message: 'Fixture rules unavailable' } : null } };
const capabilities = { accounts: true, mrs: false, services: false, reports: true, generation: true, credentialImport: false as const,
  projects: [{ id: 'project', label: 'Fixture project' }], scanNotice: 'Fixture-only scan',
  models: [{ id: 'original', label: 'Original model', maxInputChars: 9000, maxOutputTokens: 4000, usesAccount: true }] };
const saved = { modelId: 'original', accountId: 'bound', maxOutputTokens: 3000, idempotencyKey: 'original-key', confirmedInputChars: 999, reportRevision: 3 };
const base = { reportId: 'report', createdAt: report.createdAt, updatedAt: report.updatedAt, error: null, generation };
const jobs = [
  { ...base, id: 'recover', kind: 'generate', state: 'awaiting_resume', resumeAction: 'generate', request: saved, resultAvailable: false },
  { ...base, id: 'unknown', kind: 'generate', state: 'outcome_unknown', resumeAction: null, request: { ...saved, idempotencyKey: 'unknown-key' }, resultAvailable: false },
  { ...base, id: 'draft', kind: 'generate', state: 'succeeded', resumeAction: null, request: saved, resultAvailable: true },
  { ...base, id: 'collect', kind: 'collect', state: 'queued', resumeAction: 'collect', request: null, resultAvailable: false },
];
const account = { id: 'bound', originalName: 'Fixture account', alias: '', tags: [], notes: '', enabled: true, available: true, provider: 'stepcode',
  usage: { data: [{ id: 'budget', label: 'Fixture budget', source: 'usage', remaining: 10, used: 0, total: 10, currency: 'USD', period: null, expiresAt: null }], stale: true, updatedAt: report.updatedAt, error: null } };
const calls: { path: string; method: string; body: unknown }[] = [];
Object.assign(window, { fixture: { calls, report, jobs } });
window.confirm = () => true;
window.fetch = async (path, options) => {
  const url = String(path), method = options?.method ?? 'GET';
  const body = options?.body ? JSON.parse(String(options.body)) : null;
  calls.push({ path: url, method, body });
  let payload: unknown;
  if (url === '/api/jobs' && method === 'GET') payload = { jobs };
  else if (url === '/api/reports' && method === 'GET') payload = { reports: [{ ...report, generated: false }] };
  else if (url === '/api/accounts' && method === 'GET') payload = { accounts: [account] };
  else if (url === '/api/reports/report' && method === 'GET') payload = { report };
  else if (url === '/api/reports/report' && method === 'PATCH') { report.text = body.text; report.revision++; payload = { report }; }
  else if (url === '/api/reports/report/generate' && method === 'POST') {
    const job = { ...base, id: 'retry', kind: 'generate', state: 'queued', resumeAction: null, request: body, resultAvailable: false };
    jobs.push(job); payload = { job };
  } else if (url.startsWith('/api/jobs/')) {
    const job = jobs.find(j => j.id === url.split('/')[3]);
    if (!job) throw new Error('Unexpected fixture job');
    if (method === 'POST' && url.endsWith('/resume')) { job.state = 'queued'; job.resumeAction = null; payload = { job }; }
    else payload = { job, result: job.id === 'draft' ? { text: 'Durable alternative draft', warnings: ['Edited during generation'], generation } : null };
  } else throw new Error(`Blocked unexpected fixture request: ${method} ${url}`);
  return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
createRoot(document.getElementById('root')!).render(<Reports capabilities={capabilities} />);
