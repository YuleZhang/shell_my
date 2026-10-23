import test from 'node:test';
import assert from 'node:assert/strict';
import { sandbox, setup, login, collectInput, emptyEvidence, DUMMY_KEY, jsonResponse } from './helpers.js';
import { filterEvidence, weekBounds, WeeklyEvidence } from '../server/adapters/weekly-report.js';
import { outputWarnings } from '../server/adapters/model.js';
import type { Report } from '../shared/contracts.js';
const sha = 'a'.repeat(40);
const row = (repo: string) => ({ repo, sha, time: '2026-10-21T13:00:00+08:00', title: `Dummy optimization ${DUMMY_KEY}`, branch: 'feat',
  intent: 'raw intent must not leak', match: 'time', sessions: [{ path: '/home/dummy/secret-transcript', id: 'raw-id', context: `notes ${DUMMY_KEY}`, match: 'time' }] });

test('Monday week bounds cross year and leap year correctly', () => {
  assert.deepEqual(weekBounds('2026-10-21'), ['2026-10-19', '2026-10-26']);
  assert.deepEqual(weekBounds('2027-01-01'), ['2026-12-28', '2027-01-04']);
  assert.deepEqual(weekBounds('2024-02-29'), ['2024-02-26', '2024-03-04']);
});
test('evidence selected exact root/date filtering, redaction and transcript default exclusion', () => {
  const b = sandbox(); try {
    const raw = { week_start: '2026-10-19', week_end: '2026-10-26', commits: [row(b.config.projectRoots[0].root), row('/tmp/not-allowed'),
      { ...row(b.config.projectRoots[0].root), sha: 'b'.repeat(40), time: '2026-10-26T01:00:00+08:00' }] };
    const evidence = filterEvidence(raw, collectInput, b.config); assert.equal(evidence.commits.length, 1);
    assert.equal(evidence.commits[0].context, null); assert.equal(evidence.commits[0].mrStatus, 'unverified');
    for (const bad of [DUMMY_KEY, b.dir, 'secret-transcript', 'raw-id', 'raw intent']) assert.ok(!JSON.stringify(evidence).includes(bad));
    const context = filterEvidence(raw, { ...collectInput, includeContext: true }, b.config);
    assert.equal(context.commits[0].context, 'notes [REDACTED]');
    assert.throws(() => filterEvidence(raw, { ...collectInput, projectIds: ['unknown'] }, b.config));
    assert.throws(() => filterEvidence({ ...raw, week_start: '2026-10-12' }, collectInput, b.config));
  } finally { b.cleanup(); }
});
test('weekly adapter calls same configured script with Python fixed offline argv and bounded commit enrichment', async () => {
  const b = sandbox(); const calls: string[][] = [];
  try {
    const adapter = new WeeklyEvidence(b.config, undefined, async (command, args, options) => {
      calls.push([command, ...args]); assert.ok(options?.maxBytes);
      if (command === '/usr/bin/python3.10') return JSON.stringify({ week_start: '2026-10-19', week_end: '2026-10-26', commits: [row(b.config.projectRoots[0].root)] });
      return `Verified commit body, token=${DUMMY_KEY}`;
    });
    const evidence = await adapter.collect(collectInput);
    assert.deepEqual(calls[0], ['/usr/bin/python3.10', `${b.config.reports!.skillPath}/scripts/weekly_commits.py`, '--week', '2026-10-21', '--json', '--offline']);
    assert.equal(calls[1][0], '/usr/bin/git'); assert.ok(calls[1].includes(sha)); assert.ok(!evidence.commits[0].body.includes(DUMMY_KEY));
  } finally { b.cleanup(); }
});
test('collect job durable/idempotent, fragment/Todo retained and raw credential never persisted', async () => {
  let calls = 0; const b = await setup({ collectEvidence: async () => { calls++; return emptyEvidence; } });
  try {
    const headers = await login(b.app), input = { ...collectInput, fragments: `Keep fragment ${DUMMY_KEY}` };
    const one = await b.app.inject({ method: 'POST', url: '/api/reports/collect', headers, payload: input });
    const two = await b.app.inject({ method: 'POST', url: '/api/reports/collect', headers, payload: input });
    assert.equal(one.statusCode, 202); assert.equal(one.json().job.id, two.json().job.id);
    await b.app.backend.runner.idle(); assert.equal(calls, 1);
    const job = b.store.job(one.json().job.id)!; assert.equal(job.state, 'succeeded');
    const response = await b.app.inject({ url: `/api/reports/${job.reportId}`, headers });
    const report = response.json().report;
    assert.equal(report.previousTodo, collectInput.previousTodo); assert.equal(report.fragments, 'Keep fragment [REDACTED]'); assert.equal(report.text, '');
    assert.ok(report.inputChars > 100); assert.ok(!response.body.includes(DUMMY_KEY));
    assert.ok(!JSON.stringify(b.store.jobInput(job.id)).includes(DUMMY_KEY));
    const edited = await b.app.inject({ method: 'PATCH', url: `/api/reports/${job.reportId}`, headers, payload: { revision: 0, text: 'Plain edited Markdown' } });
    assert.equal(edited.json().report.revision, 1);
    assert.equal((await b.app.inject({ method: 'PATCH', url: `/api/reports/${job.reportId}`, headers, payload: { revision: 0, text: 'conflict' } })).statusCode, 409);
  } finally { await b.close(); }
});
test('collect rejects absent consent and projects outside configured allowlist', async () => {
  const b = await setup(); try {
    const headers = await login(b.app);
    for (const payload of [{ ...collectInput, consentScan: false }, { ...collectInput, projectIds: ['evil'] }]) assert.equal((await b.app.inject({ method: 'POST', url: '/api/reports/collect', headers, payload })).statusCode, 400);
  } finally { await b.close(); }
});
test('generate performs exactly one no-tools request, persists sending before request, idempotent and revisioned result', async () => {
  let sends = 0; let sentState = ''; const text = 'Situation\n- Demo\nAction\n- Demo\nOthers:\n- notes\nResult\n- Demo 待测\nTodo\n- carry';
  const b = await setup({ collectEvidence: async () => emptyEvidence, fetcher: async (_url, init) => {
    sends++; sentState = String(b.store.db.prepare("SELECT state FROM jobs WHERE kind='generate'").get()!.state);
    const body = JSON.parse(String(init?.body)); assert.equal(body.tools, undefined); assert.equal(body.stream, false); assert.equal(body.max_tokens, 1000);
    assert.ok(body.messages[1].content.includes(collectInput.previousTodo));
    return jsonResponse({ choices: [{ message: { content: text } }] });
  } });
  try {
    const headers = await login(b.app);
    const collect = b.app.backend.runner.collect(collectInput); await b.app.backend.runner.idle();
    const id = collect.reportId!;
    const report = b.store.get<Report>(`report:${id}`)!;
    const input = { modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true, idempotencyKey: 'generate-1' };
    const one = await b.app.inject({ method: 'POST', url: `/api/reports/${id}/generate`, headers, payload: input }); assert.equal(one.statusCode, 202);
    const two = await b.app.inject({ method: 'POST', url: `/api/reports/${id}/generate`, headers, payload: input }); assert.equal(one.json().job.id, two.json().job.id);
    await b.app.backend.runner.idle(); assert.equal(sends, 1); assert.equal(sentState, 'sending');
    const generated = b.store.get<Report>(`report:${id}`)!;
    assert.equal(generated.text, text); assert.equal(generated.revision, 1); assert.equal(generated.generation?.skillHash.length, 64);
    assert.equal(b.store.job(one.json().job.id)!.state, 'succeeded');
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/reports/${id}/generate`, headers, payload: input })).json().job.id, one.json().job.id); assert.equal(sends, 1);
  } finally { await b.close(); }
});
test('uncertain model network result never auto-retries; user retry and scale/budget confirmations required', async () => {
  let sends = 0; const b = await setup({ collectEvidence: async () => emptyEvidence, fetcher: async () => { sends++; throw new Error(DUMMY_KEY); } });
  try {
    const headers = await login(b.app), collect = b.app.backend.runner.collect(collectInput); await b.app.backend.runner.idle();
    const report = b.store.get<Report>(`report:${collect.reportId}`)!;
    const input = { modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true, idempotencyKey: 'send-unknown' };
    assert.equal((await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: { ...input, confirmedInputChars: 0 } })).statusCode, 409);
    const res = await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: input });
    await b.app.backend.runner.idle(); assert.equal(b.store.job(res.json().job.id)!.state, 'outcome_unknown'); assert.equal(sends, 1);
    b.store.recover(); b.app.backend.runner.kick(); await b.app.backend.runner.idle(); assert.equal(sends, 1);
    const retry = await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: { ...input, idempotencyKey: 'retry-new' } }); assert.equal(retry.statusCode, 409);
    const confirmed = await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: { ...input, idempotencyKey: 'retry-confirmed', retryUnknown: true } }); assert.equal(confirmed.statusCode, 202);
    await b.app.backend.runner.idle(); assert.equal(sends, 2);
  } finally { await b.close(); }
});
test('restart recovery requeues preparing/ready safely and sending becomes outcome_unknown', () => {
  const b = sandbox(); try {
    const a = b.store.enqueue('collect', 'a', {}, 'report-a'), c = b.store.enqueue('generate', 'c', {}, 'report-c'), d = b.store.enqueue('generate', 'd', {}, 'report-d');
    b.store.updateJob(a.id, 'preparing'); b.store.updateJob(c.id, 'ready'); b.store.updateJob(d.id, 'sending');
    b.store.recover(); assert.equal(b.store.job(a.id)!.state, 'queued'); assert.equal(b.store.job(c.id)!.state, 'awaiting_resume'); assert.equal(b.store.job(d.id)!.state, 'outcome_unknown');
    assert.throws(() => b.store.enqueue('collect', 'a', { changed: true }, 'report-a'));
  } finally { b.cleanup(); }
});
test('editing a report during sending preserves the edit and durable generated alternative', async () => {
  let release!: () => void; let entered!: () => void;
  const waiting = new Promise<void>(resolve => { entered = resolve; });
  const barrier = new Promise<void>(resolve => { release = resolve; });
  const b = await setup({ collectEvidence: async () => emptyEvidence, fetcher: async () => {
    entered(); await barrier;
    return jsonResponse({ choices: [{ message: { content: 'Situation\nAction\nOthers:\nResult\nTodo' } }] });
  } });
  try {
    const headers = await login(b.app), collect = b.app.backend.runner.collect(collectInput);
    await b.app.backend.runner.idle();
    const report = b.store.get<Report>(`report:${collect.reportId}`)!;
    const response = await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers,
      payload: { modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true, idempotencyKey: 'concurrent-edit' } });
    await waiting;
    const edit = await b.app.inject({ method: 'PATCH', url: `/api/reports/${report.id}`, headers, payload: { revision: 0, text: 'User edit must survive' } });
    assert.equal(edit.statusCode, 200); release(); await b.app.backend.runner.idle();
    assert.equal(b.store.get<Report>(`report:${report.id}`)!.text, 'User edit must survive');
    const job = await b.app.inject({ url: `/api/jobs/${response.json().job.id}`, headers });
    assert.equal(job.json().job.state, 'succeeded'); assert.ok(job.json().result.text.includes('Situation'));
    assert.ok(job.json().result.warnings.some((w: string) => w.includes('not applied')));
  } finally { release(); await b.close(); }
});
test('restart with queued paid work performs no network until explicit resubmission', async () => {
  const b = sandbox(); let sends = 0;
  const { buildApp } = await import('../server/app.js');
  const app = await buildApp(b.config, b.store, { readKeys: () => `demo ${DUMMY_KEY}`, collectEvidence: async () => emptyEvidence,
    fetcher: async () => { sends++; return jsonResponse({ choices: [{ message: { content: 'Situation\nAction\nOthers:\nResult\nTodo' } }] }); } });
  let restarted: typeof app | undefined;
  try {
    const collect = app.backend.runner.collect(collectInput); await app.backend.runner.idle();
    const report = b.store.get<Report>(`report:${collect.reportId}`)!;
    const input = { modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true as const, retryUnknown: false, idempotencyKey: 'restart-paid' };
    const job = b.store.enqueue('generate', input.idempotencyKey, { input, report }, report.id);
    b.store.updateJob(job.id, 'preparing'); await app.close();
    restarted = await buildApp(b.config, b.store, { startJobs: true, readKeys: () => `demo ${DUMMY_KEY}`, fetcher: async () => {
      sends++; return jsonResponse({ choices: [{ message: { content: 'Situation\nAction\nOthers:\nResult\nTodo' } }] });
    } });
    await new Promise(resolve => setTimeout(resolve, 10)); assert.equal(sends, 0);
    const headers = await login(restarted);
    assert.equal((await restarted.inject({ url: `/api/jobs/${job.id}`, headers })).json().job.state, 'awaiting_resume'); assert.equal(sends, 0);
    const resume = await restarted.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: input });
    assert.equal(resume.json().job.id, job.id); await restarted.backend.runner.idle(); assert.equal(sends, 1);
  } finally { if (restarted) await restarted.close(); else await app.close(); b.cleanup(); }
});
test('output structure warning is truthful rather than deterministic fabricated report', () => {
  assert.equal(outputWarnings('Situation\nAction\nOthers:\nResult\nTodo').length, 0);
  assert.ok(outputWarnings('Not a valid weekly report').length > 0);
  assert.ok(outputWarnings('<script>alert(1)</script>').some(s => s.includes('HTML')));
});
