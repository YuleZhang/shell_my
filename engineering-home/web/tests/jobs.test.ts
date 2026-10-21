import assert from 'node:assert/strict';
import test from 'node:test';
import type { Account, Capabilities, JobDiscovery, Report } from '../../shared/contracts';
import { resumeJobSchema } from '../../shared/contracts';
import { generationDisabled, generationResumeInput, isActiveJob, jobLabels, resumeProblem } from '../src/jobs';
import { request, safeExternalUrl, setSessionToken } from '../src/client';

const capabilities: Capabilities = { accounts: true, mrs: false, services: false, reports: true, generation: true,
  credentialImport: false, projects: [], scanNotice: '', models: [{ id: 'original', label: 'Original', usesAccount: true, maxInputChars: 9000, maxOutputTokens: 4000 }] };
const report: Report = { id: 'report', week: '2026-10-19', projectIds: [], fragments: '', previousTodo: '',
  evidence: { weekStart: '2026-10-19', weekEnd: '2026-10-25', commits: [], warnings: [] }, text: 'Manual draft',
  revision: 7, createdAt: '', updatedAt: '', inputChars: 2345, generationReadiness: { ready: true, error: null }, warnings: [], generation: null };
const job: JobDiscovery = { id: 'job', kind: 'generate', reportId: report.id, state: 'awaiting_resume', createdAt: '', updatedAt: '', error: null,
  resultAvailable: false, resumeAction: 'generate', generation: null, request: { modelId: 'original', accountId: 'bound', maxOutputTokens: 3000,
    idempotencyKey: 'original-key', confirmedInputChars: 999, reportRevision: 3 } };
const account: Account = { id: 'bound', originalName: 'Fixture', alias: '', tags: [], notes: '', enabled: true, available: true,
  provider: 'stepcode', usage: { data: [], updatedAt: null, stale: true, error: null } };

test('paused recovery and uncertain outcomes are terminal for polling, all states have labels', () => {
  for (const state of ['awaiting_resume', 'failed', 'succeeded', 'outcome_unknown'] as const) assert.equal(isActiveJob(state), false);
  for (const state of ['queued', 'preparing', 'ready', 'sending'] as const) assert.equal(isActiveJob(state), true);
  assert.equal(Object.keys(jobLabels).length, 8);
});
test('resume uses exact original binding/key/output limit but fresh server-confirmed size', () => {
  assert.equal(resumeProblem(job, report, capabilities, [account]), null);
  const input = generationResumeInput(job, report);
  assert.deepEqual(input, { kind: 'generate', confirmResume: true, modelId: 'original', accountId: 'bound',
    maxOutputTokens: 3000, idempotencyKey: 'original-key', confirmedInputChars: 2345, confirmBudget: true, retryUnknown: false });
  assert.deepEqual(resumeJobSchema.parse(input), input);
  assert.equal(report.text, 'Manual draft'); assert.equal(report.revision, 7);
});
test('resume refuses outcome unknown, wrong report, missing configuration and changed limits/account', () => {
  assert.ok(resumeProblem({ ...job, state: 'outcome_unknown', resumeAction: null }, report, capabilities, [account]));
  assert.ok(resumeProblem(job, { ...report, id: 'other' }, capabilities, [account]));
  assert.ok(resumeProblem(job, report, { ...capabilities, models: [] }, [account]));
  assert.ok(resumeProblem(job, report, { ...capabilities, models: [{ ...capabilities.models[0], maxOutputTokens: 2000 }] }, [account]));
  assert.ok(resumeProblem(job, { ...report, inputChars: 9001 }, capabilities, [account]));
  assert.ok(resumeProblem(job, report, capabilities, [{ ...account, enabled: false }]));
  assert.ok(resumeProblem(job, report, capabilities, [{ ...account, id: 'different' }]));
});
test('historical reports stay readable while generation fails closed on absent/false readiness', () => {
  for (const readiness of [undefined, { ready: false, error: { code: 'generation_disabled', message: 'disabled' } }]) {
    assert.match(generationDisabled({ ...report, generationReadiness: readiness }, capabilities)!, /生成已禁用/);
    assert.ok(resumeProblem(job, { ...report, generationReadiness: readiness }, capabilities, [account]));
  }
  assert.equal(generationDisabled(report, capabilities), null);
});
test('API reads are GET-only, same-origin, without CSRF; explicit resume carries consent', async () => {
  const oldFetch = globalThis.fetch; const calls: { path: string; options: RequestInit }[] = [];
  globalThis.fetch = async (path, options) => { calls.push({ path: String(path), options: options! }); return new Response('{}', { headers: { 'Content-Type': 'application/json' } }); };
  try {
    setSessionToken('fixture-only');
    await request('/api/jobs'); await request('/api/jobs/job');
    await request('/api/jobs/job/resume', { method: 'POST', body: generationResumeInput(job, report) });
    assert.equal(calls[0].options.method, 'GET'); assert.equal(calls[1].options.body, undefined);
    assert.equal((calls[0].options.headers as Record<string, string>)['X-CSRF-Token'], undefined);
    assert.equal(calls[2].options.credentials, 'same-origin'); assert.equal(calls[2].options.mode, 'same-origin');
    assert.equal(JSON.parse(calls[2].options.body as string).confirmResume, true);
    await assert.rejects(request('https://example.invalid/api/jobs'));
    assert.equal(safeExternalUrl('https://user:password@example.invalid/'), undefined);
    assert.equal(calls.length, 3);
  } finally { globalThis.fetch = oldFetch; setSessionToken(null); }
});
