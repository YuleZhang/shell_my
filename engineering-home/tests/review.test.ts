import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { redact } from '../server/security/sanitize.js';
import { setup, login, collectInput, emptyEvidence, jsonResponse, DUMMY_KEY } from './helpers.js';
import type { Report } from '../shared/contracts.js';

for (const input of ['{"pass\\u0077ord":"DUMMY"}', '{"password":"DUMMY"}', '{"token":"DUMMY"}', '{"nested":{"client_secret":{"value":"DUMMY"}}}', 'prefix {"access_token":"DUMMY with spaces"} suffix']) {
  test(`quoted/nested secret redaction: ${input}`, () => { assert.ok(!redact(input).includes('DUMMY')); });
}
test('synthetic secrets never reach saved inputs, evidence, preview, model or result', async () => {
  const secrets = ['DUMMY_PASSWORD', 'DUMMY_TOKEN', DUMMY_KEY]; let body = '';
  const b = await setup({ collectEvidence: async () => ({ ...emptyEvidence, warnings: ['{"password":"DUMMY_PASSWORD"}'] }),
    fetcher: async (_url, init) => { body = String(init?.body); return jsonResponse({ choices: [{ message: { content: '{"token":"DUMMY_TOKEN"}' } }] }); } });
  try {
    const headers = await login(b.app);
    const configured = [b.config.auth.cookieSecret, b.config.gitlab!.token, b.config.reports!.models[0].apiKey!];
    secrets.push(...configured);
    const job = b.app.backend.runner.collect({ ...collectInput, fragments: '{"password":"DUMMY_PASSWORD","nested":{"token":"DUMMY_TOKEN"}} ' + configured.join(' '), previousTodo: DUMMY_KEY });
    await b.app.backend.runner.idle();
    const preview = await b.app.inject({ url: `/api/reports/${job.reportId}`, headers });
    const report = preview.json().report;
    const gen = await b.app.inject({ method: 'POST', url: `/api/reports/${job.reportId}/generate`, headers, payload: {
      modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true, idempotencyKey: 'redaction-e2e' } });
    assert.equal(gen.statusCode, 202); await b.app.backend.runner.idle();
    const persisted = JSON.stringify(b.store.list('report:')) + JSON.stringify(b.store.jobInput(job.id)) + JSON.stringify(b.store.jobInput(gen.json().job.id)) + JSON.stringify(b.store.db.prepare('SELECT value FROM report_versions').all());
    for (const secret of secrets) for (const text of [persisted, preview.body, body, JSON.stringify(b.store.get(`generation:${gen.json().job.id}`))]) assert.ok(!text.includes(secret));
  } finally { await b.close(); }
});

test('historical report remains readable and editable with reports disabled', async () => {
  const b = await setup({}, c => { c.reports = undefined; });
  try {
    const report: Report = { id: 'saved', week: collectInput.week, projectIds: ['demo'], fragments: 'old fragment', previousTodo: 'old todo', evidence: emptyEvidence,
      text: 'saved text', revision: 0, createdAt: '2026-10-21', updatedAt: '2026-10-21', inputChars: 42, warnings: [], generation: null };
    b.store.saveReport(report); const headers = await login(b.app);
    const read = await b.app.inject({ url: '/api/reports/saved', headers }); assert.equal(read.statusCode, 200);
    assert.equal(read.json().report.text, 'saved text'); assert.equal(read.json().report.inputChars, 0);
    assert.equal(read.json().report.generationReadiness.ready, false);
    const edit = await b.app.inject({ method: 'PATCH', url: '/api/reports/saved', headers, payload: { revision: 0, text: 'edited' } });
    assert.equal(edit.statusCode, 200); assert.equal(edit.json().report.evidence.weekStart, emptyEvidence.weekStart);
    assert.equal(edit.json().report.fragments, 'old fragment');
    assert.equal((await b.app.inject({ url: '/api/reports', headers })).statusCode, 200);
    assert.equal(b.store.db.prepare('SELECT revision FROM report_versions WHERE report_id=? ORDER BY revision DESC').get('saved')!.revision, 1);
  } finally { await b.close(); }
});

test('historical report with missing rules still supports text, evidence and revisioned edits', async () => {
  const b = await setup({ collectEvidence: async () => emptyEvidence });
  try {
    const collected = b.app.backend.runner.collect(collectInput); await b.app.backend.runner.idle();
    renameSync(join(b.config.reports!.skillPath, 'references'), join(b.config.reports!.skillPath, 'references-disabled'));
    const headers = await login(b.app), path = `/api/reports/${collected.reportId}`;
    const read = await b.app.inject({ url: path, headers }); assert.equal(read.statusCode, 200);
    assert.equal(read.json().report.inputChars, 0); assert.equal(read.json().report.generationReadiness.ready, false);
    const patch = await b.app.inject({ method: 'PATCH', url: path, headers, payload: { revision: 0, text: 'Historical edit' } });
    assert.equal(patch.statusCode, 200); assert.equal(patch.json().report.revision, 1);
    assert.equal(patch.json().report.evidence.weekStart, emptyEvidence.weekStart);
  } finally { await b.close(); }
});
test('explicit collection resume is authenticated and never releases paid suspension', async () => {
  const b = await setup({ collectEvidence: async () => emptyEvidence });
  try {
    const job = b.store.enqueue('collect', 'resume-collect', { ...collectInput, idempotencyKey: 'resume-collect' }, 'resume-report');
    const headers = await login(b.app), path = `/api/jobs/${job.id}/resume`;
    assert.equal((await b.app.inject({ method: 'POST', url: path, headers, payload: { kind: 'collect' } })).statusCode, 400);
    assert.equal((await b.app.inject({ method: 'POST', url: path, headers, payload: { kind: 'collect', confirmResume: true } })).statusCode, 202);
    await b.app.backend.runner.idle(); assert.equal(b.store.job(job.id)!.state, 'succeeded');
    assert.equal((await b.app.inject({ method: 'POST', url: path, headers, payload: { kind: 'collect', confirmResume: true } })).statusCode, 409);
  } finally { await b.close(); }
});

test('job rediscovery GET and collection never unpause recovered paid jobs; only specific confirmed resume sends', async () => {
  let sends = 0;
  const b = await setup({ collectEvidence: async () => emptyEvidence, fetcher: async () => {
    sends++; return jsonResponse({ choices: [{ message: { content: 'Situation\nAction\nOthers:\nResult\nTodo' } }] });
  } });
  try {
    const headers = await login(b.app), collected = b.app.backend.runner.collect(collectInput); await b.app.backend.runner.idle();
    const report = b.store.get<Report>(`report:${collected.reportId}`)!;
    const input = { modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true as const, retryUnknown: false, idempotencyKey: 'paused-one' };
    const one = b.store.enqueue('generate', input.idempotencyKey, { input, report }, report.id);
    const other = b.store.enqueue('generate', 'paused-two', { input: { ...input, idempotencyKey: 'paused-two' }, report }, report.id);
    b.store.updateJob(other.id, 'ready'); b.store.recover();
    const list = await b.app.inject({ url: '/api/jobs', headers }); assert.equal(list.statusCode, 200);
    const found = list.json().jobs.find((j: { id: string }) => j.id === one.id);
    assert.equal(found.state, 'awaiting_resume'); assert.equal(found.resumeAction, 'generate'); assert.equal(found.resultAvailable, false);
    assert.equal(found.request.idempotencyKey, input.idempotencyKey); assert.ok(!list.body.includes('fragments'));
    assert.equal((await b.app.inject({ url: '/api/jobs', headers: { host: headers.host } })).statusCode, 401);
    b.app.backend.runner.collect({ ...collectInput, idempotencyKey: 'unrelated-collect' }); await b.app.backend.runner.idle();
    assert.equal(sends, 0); assert.equal(b.store.job(one.id)!.state, 'awaiting_resume');
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: { ...input, idempotencyKey: 'duplicate' } })).statusCode, 409);
    const payload = { ...input, kind: 'generate', confirmResume: true };
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers: { ...headers, 'x-csrf-token': 'bad' }, payload })).statusCode, 403);
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers, payload: { ...payload, confirmBudget: false } })).statusCode, 400);
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers, payload: { ...payload, modelId: 'not-allowed' } })).statusCode, 409);
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers, payload: { ...payload, accountId: 'not-supported' } })).statusCode, 409);
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers, payload: { ...payload, idempotencyKey: 'wrong-job' } })).statusCode, 409);
    writeFileSync(join(b.config.reports!.skillPath, 'references/style-guide.md'), 'Current changed report rules.');
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers, payload })).statusCode, 409);
    const preview = (await b.app.inject({ url: `/api/reports/${report.id}`, headers })).json().report;
    const resumed = await b.app.inject({ method: 'POST', url: `/api/jobs/${one.id}/resume`, headers, payload: { ...payload, confirmedInputChars: preview.inputChars } });
    assert.equal(resumed.statusCode, 202); assert.equal(resumed.json().job.id, one.id); await b.app.backend.runner.idle();
    assert.equal(sends, 1); assert.equal(b.store.job(other.id)!.state, 'awaiting_resume');
    const result = (await b.app.inject({ url: `/api/jobs/${one.id}`, headers })).json();
    assert.equal(result.job.resultAvailable, true); assert.equal(result.result.generation.inputHash.length, 64);
    b.store.updateJob(other.id, 'sending'); b.store.recover();
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/jobs/${other.id}/resume`, headers, payload: { ...payload, idempotencyKey: 'paused-two' } })).statusCode, 409);
    b.app.backend.runner.kick(); await b.app.backend.runner.idle(); assert.equal(sends, 1);
  } finally { await b.close(); }
});
