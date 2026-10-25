import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';

// Load exactly the packaged asset without requiring a project package.json.
const source = fs.readFileSync(new URL('./step_watch_assets/index.js', import.meta.url), 'utf8');
const { default: bridge, RecoveryGate, NativeUISafety } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const uninstalled = new NativeUISafety({ ui: {} });
assert.doesNotThrow(() => uninstalled.close());
assert.doesNotThrow(() => uninstalled.close());
let time = 0;
const ctx = { isIdle: () => true, hasPendingMessages: () => false,
              ui: { getEditorText: () => '' } };
const safe = { verified: true, modal: false, images: false, draft: false, input: false };
const failure = { messages: [{ role: 'assistant', stopReason: 'error', errorMessage:
  'Our servers are currently overloaded. Please try again later.' }] };
const gate = new RecoveryGate(() => time);
const arm = () => { gate.end(failure); gate.settle(); time += 3001; };
arm();
assert.equal(gate.status(ctx).recovery, 'ui-safety-unavailable');
assert.equal(gate.status(ctx, safe).eligible, true);
for (const [field, reason] of [['modal', 'modal'], ['images', 'draft'], ['draft', 'draft'], ['input', 'input']]) {
  assert.equal(gate.status(ctx, { ...safe, [field]: true }).recovery, reason);
  assert.equal(gate.status(ctx, { ...safe, [field]: undefined }).eligible, false);
}
assert.equal(gate.status({ ...ctx, isIdle: () => false }, safe).recovery, 'busy');
assert.equal(gate.status({ ...ctx, hasPendingMessages: () => true }, safe).recovery, 'pending');
assert.equal(gate.status({ ...ctx, ui: { getEditorText: () => 'draft' } }, safe).recovery, 'draft');
assert.equal(gate.status({}, safe).eligible, false);
const sends = [];
assert.equal(gate.recover({ sendUserMessage: (...args) => sends.push(args) }, ctx, safe).recovery, 'submitted-unconfirmed');
assert.deepEqual(sends, [['continue', { deliverAs: 'followUp' }]]);
assert.equal(gate.recover({}, ctx, safe).eligible, false);
arm(); assert.equal(gate.status(ctx, safe).recovery, 'cooldown');
time += 60000;
gate.cancel(); assert.equal(gate.status(ctx, safe).recovery, 'no-candidate');
gate.end(failure); assert.equal(gate.status(ctx, safe).recovery, 'not-settled');
gate.settle(); assert.equal(gate.status(ctx, safe).recovery, 'quiet');
for (const stopReason of ['stop', 'aborted', 'length']) {
  gate.end({ messages: [{ ...failure.messages[0], stopReason }] });
  assert.equal(gate.candidate, null);
}
gate.end({ messages: [...failure.messages, { role: 'assistant', stopReason: 'stop' }] });
assert.equal(gate.candidate, null);
const rateError = 'Error: Retry failed after 3 attempts: OpenAI API error (429): {"message":"Rate limit exceeded\n [trace_id=synthetic\n code=20011]","type":"rate_limit_exceeded","code":"rate_limit_exceeded"}';
const quotaError = 'Error: OpenAI API error (402): {"message":"API Key quota has been exhausted. | Used 2001 of\n 2000 in the current cycle.","type":"insufficient_quota","code":api_key_quota_exhausted"}';
const fail = errorMessage => ({ messages: [{ role: 'assistant', stopReason: 'error', errorMessage }] });
// Settlement is required, even after the native retry budget is exhausted.
time += 60000;
gate.end(fail(rateError));
assert.equal(gate.status(ctx, safe).recovery, 'not-settled');
gate.settle(); time += 3001;
assert.equal(gate.status(ctx, safe).eligible, true);
gate.recover({ sendUserMessage: (...args) => sends.push(args) }, ctx, safe);
assert.equal(gate.candidate, null);
assert.equal(sends.length, 2);
// Identical errors in new runs remain recoverable after the cooldown.
gate.end(fail(rateError)); gate.settle(); time += 3001;
assert.equal(gate.status(ctx, safe).recovery, 'cooldown');
time += 60000;
gate.recover({ sendUserMessage: (...args) => sends.push(args) }, ctx, safe);
assert.equal(sends.length, 3);
for (const message of [quotaError, quotaError + '\nrate_limit_exceeded\n' + failure.messages[0].errorMessage]) {
  gate.end(fail(message)); gate.settle(); time += 60001;
  assert.equal(gate.status(ctx, safe).recovery, 'quota-exhausted');
  gate.recover({ sendUserMessage: () => assert.fail('quota must never send') }, ctx, safe);
}
for (const message of ['API Key quota has been exhausted', 'OpenAI API error (402): unrelated',
                       'Error: OpenAI API error (500): unrelated']) {
  gate.end(fail(message));
  assert.equal(gate.status(ctx, safe).recovery, 'no-candidate');
}
gate.end(fail(rateError)); gate.end({ messages: [{ role: 'assistant', stopReason: 'stop' }] });
gate.settle(); time += 3001;
assert.equal(gate.status(ctx, safe).recovery, 'no-candidate');
gate.end(fail('rate_limit_exceeded')); gate.settle(); time += 60001;
gate.recover({ sendUserMessage: () => { throw Error('private'); } }, ctx, safe);
assert.equal(gate.candidate, null);
assert.equal(gate.recover({}, ctx, safe).eligible, false);
gate.cancel();
time += 60000; arm();
gate.recover({ sendUserMessage: () => { throw Error('private'); } }, ctx, safe);
assert.equal(gate.candidate, null);

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'yw-js-'));
fs.chmodSync(root, 0o700);
process.env.YUUTILS_WATCH_RUNTIME = root;
const handlers = new Map();
let removeInput = 0;
ctx.ui.onTerminalInput = callback => { ctx.terminalInput = callback; return () => removeInput++; };
bridge({ on: (name, handler) => handlers.set(name, handler), sendUserMessage: () => { throw Error('must not send'); } });
const request = (meta, changes = {}, nonce = Date.now().toString(16).padStart(12, '0') + crypto.randomBytes(10).toString('hex')) => {
  const body = JSON.stringify({ op: 'status', cap: meta.cap, generation: meta.generation, nonce, ...changes });
  return new Promise((resolve, reject) => {
    const req = http.request({ socketPath: path.join(root, `b-${meta.pid}-${meta.generation}.sock`),
      method: 'POST', path: '/', headers: { 'Content-Length': Buffer.byteLength(body) } }, res => {
      let data = ''; res.on('data', c => data += c); res.on('end', () => resolve({ code: res.statusCode, data: JSON.parse(data) }));
    });
    req.on('error', reject); req.end(body);
  });
};
try {
  await handlers.get('session_start')({}, ctx);
  const file = fs.readdirSync(root).find(f => f.endsWith('.json'));
  assert.ok(file, 'session_start creates metadata');
  const meta = JSON.parse(fs.readFileSync(path.join(root, file)));
  assert.deepEqual(Object.keys(meta).sort(), ['cap', 'generation', 'pid', 'start', 'uid']);
  for (const file of fs.readdirSync(root)) assert.equal(fs.statSync(path.join(root, file)).mode & 0o777, 0o600);
  assert.equal((await request(meta)).data.recovery, 'ui-safety-unavailable');
  assert.equal((await request(meta, { cap: 'x'.repeat(64) })).code, 403);
  assert.equal((await request(meta, { op: 'prompt', prompt: 'anything' })).code, 403);
  assert.equal((await request(meta, { op: 'recover' })).data.eligible, false);
  const nonce = Date.now().toString(16).padStart(12, '0') + 'a'.repeat(20);
  assert.equal((await request(meta, {}, nonce)).code, 200);
  assert.equal((await request(meta, {}, nonce)).code, 403);
  assert.equal((await request(meta, {}, '0'.repeat(32))).code, 403);
  for (const event of ['input', 'agent_start', 'tool_execution_start', 'ui_prompt_start', 'ui_prompt_end']) {
    handlers.get('agent_end')(failure, ctx); handlers.get('agent_settled')({}, ctx);
    handlers.get(event)({}, ctx);
    assert.equal((await request(meta, { op: 'recover' })).data.eligible, false);
  }
  ctx.terminalInput('paste');
  await handlers.get('session_start')({}, ctx);
  assert.equal(fs.existsSync(path.join(root, file)), false);
  assert.equal(removeInput, 1);
  await handlers.get('session_shutdown')();
  assert.deepEqual(fs.readdirSync(root), []);
  assert.equal(removeInput, 2);
} finally {
  await handlers.get('session_shutdown')();
  fs.rmSync(root, { recursive: true });
}
console.log('bridge mock API + real Node Unix HTTP IPC: passed');
