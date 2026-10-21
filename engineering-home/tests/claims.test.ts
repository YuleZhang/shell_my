import test from 'node:test';
import assert from 'node:assert/strict';
import { outputWarnings } from '../server/adapters/model.js';
import { setup, login, emptyEvidence, collectInput, jsonResponse } from './helpers.js';

const structure = (claim: string) => `Situation\nAction\nOthers:\nResult\n- ${claim}\nTodo`;
const source = (fragments = '', previousTodo = '') => ({ fragments, previousTodo, evidence: emptyEvidence });
for (const claim of ['Verified latency 2ms, improved 90%', '性能已验证：延迟 2 毫秒，提升 90％', 'Measured performance is faster', 'Passed 500 tests']) {
  test(`unsupported performance/numeric claim warns: ${claim}`, () => {
    assert.ok(outputWarnings(structure(claim), source()).some(w => w.startsWith('WARN:')));
  });
}
test('measurement normalization handles separators, time units, percentages and memory units', () => {
  const support = source('Measured latency 2,000 us; measured reduction 90 percent; memory 1,024 KB.');
  assert.equal(outputWarnings(structure('Measured latency 2 ms; measured reduction 90%; memory 1 MB.'), support).length, 0);
});
test('date SHA list numbering and pending non-claims do not trigger numeric warnings', () => {
  assert.equal(outputWarnings(structure('2026-10-21 commit 1234567890abcdef1234567890abcdef12345678\n1. Review code\n2. Benchmark pending 待测'), source()).length, 0);
});
test('large numerical measurements are not mistaken for all-digit SHA identifiers', () => {
  assert.ok(outputWarnings(structure('Measured memory 10000000 bytes'), source()).some(w => w.startsWith('WARN:')));
});
test('matching numbers do not substantiate a different metric or unperformed Todo verification', () => {
  assert.ok(outputWarnings(structure('Verified latency 2ms'), source('', 'TODO: test latency 2ms')).some(w => w.startsWith('WARN:')));
  assert.ok(outputWarnings(structure('Throughput improved 90%'), source('Latency reduced 90%.')).some(w => w.startsWith('WARN:')));
});
test('unsupported claims persist clearly in report warnings and durable job alternative without replacing text', async () => {
  const text = structure('Verified latency 2ms, improved 90%');
  const b = await setup({ collectEvidence: async () => emptyEvidence, fetcher: async () => jsonResponse({ choices: [{ message: { content: text } }] }) });
  try {
    const headers = await login(b.app), collect = b.app.backend.runner.collect(collectInput); await b.app.backend.runner.idle();
    const report = (await b.app.inject({ url: `/api/reports/${collect.reportId}`, headers })).json().report;
    const response = await b.app.inject({ method: 'POST', url: `/api/reports/${report.id}/generate`, headers, payload: {
      modelId: 'dummy', maxOutputTokens: 1000, confirmedInputChars: report.inputChars, confirmBudget: true, idempotencyKey: 'claim-warning' } });
    await b.app.backend.runner.idle();
    const saved = (await b.app.inject({ url: `/api/reports/${report.id}`, headers })).json().report;
    assert.equal(saved.text, text); assert.ok(saved.warnings.some((w: string) => w.startsWith('WARN:')));
    const job = (await b.app.inject({ url: `/api/jobs/${response.json().job.id}`, headers })).json();
    assert.equal(job.result.text, text); assert.ok(job.result.warnings.some((w: string) => w.startsWith('WARN:')));
  } finally { await b.close(); }
});
