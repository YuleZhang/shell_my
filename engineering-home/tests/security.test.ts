import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync, symlinkSync, chmodSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { setup, login, sandbox, DUMMY_KEY, PASSWORD } from './helpers.js';
import { validateConfig } from '../server/config.js';
import { readPrivateFile } from '../server/security/files.js';
import { redact, safeUrl, safeError } from '../server/security/sanitize.js';
import { dateSchema, servicePatchSchema, mrFilterSchema } from '../shared/contracts.js';
import { fetchJson } from '../server/adapters/http.js';

 test('auth config missing is fail closed', () => { assert.throws(() => validateConfig({ publicOrigin: 'http://localhost:4310' })); });
test('nonloopback insecure cookies and credential URLs rejected', () => {
  const b = sandbox(); try {
    assert.throws(() => validateConfig({ ...b.config, publicOrigin: 'https://home.example.test' }));
    assert.throws(() => validateConfig({ ...b.config, stepcode: { baseUrl: 'https://user:pass@host.test' } }));
  } finally { b.cleanup(); }
});
test('all API reads reject unauthenticated clients and startup makes no network requests', async () => {
  let calls = 0; const b = await setup({ fetcher: async () => { calls++; throw new Error('must not call'); } });
  try { for (const path of ['/api/session', '/api/accounts', '/api/mrs', '/api/services', '/api/reports', '/api/jobs/nope']) {
    assert.equal((await b.app.inject({ url: path, headers: { host: 'localhost:4310' } })).statusCode, 401);
  } assert.equal(calls, 0); } finally { await b.close(); }
});
test('exact Host and Origin required; proxy headers cannot override', async () => {
  const b = await setup(); try {
    assert.equal((await b.app.inject({ url: '/api/session', headers: { host: 'evil.test', 'x-forwarded-host': 'localhost:4310' } })).statusCode, 403);
    assert.equal((await b.app.inject({ method: 'POST', url: '/api/auth/login', headers: { host: 'localhost:4310', origin: 'http://localhost:4310/' }, payload: { password: PASSWORD } })).statusCode, 403);
    assert.equal((await b.app.inject({ method: 'POST', url: '/api/auth/login', headers: { host: 'localhost:4310' }, payload: { password: PASSWORD } })).statusCode, 403);
  } finally { await b.close(); }
});
test('same-origin referrer policy preserves Firefox Origin while cross-site Origin null is rejected', async () => {
  const b = await setup();
  try {
    const response = await b.app.inject({ url: '/api/session', headers: { host: 'localhost:4310' } });
    assert.equal(response.headers['referrer-policy'], 'same-origin');
    assert.equal((await b.app.inject({ method: 'POST', url: '/api/auth/login', headers: { host: 'localhost:4310', origin: 'null' }, payload: { password: PASSWORD } })).statusCode, 403);
  } finally { await b.close(); }
});
test('login cookie, CSRF writes and logout', async () => {
  const b = await setup(); try {
    const headers = await login(b.app);
    const session = await b.app.inject({ url: '/api/session', headers });
    assert.equal(session.statusCode, 200); assert.equal(session.json().csrfToken, headers['x-csrf-token']);
    const accounts = await b.app.inject({ url: '/api/accounts', headers }); const id = accounts.json().accounts[0].id;
    assert.equal((await b.app.inject({ method: 'PATCH', url: `/api/accounts/${id}`, headers: { ...headers, 'x-csrf-token': 'bad' }, payload: { alias: 'Renamed' } })).statusCode, 403);
    assert.equal((await b.app.inject({ method: 'PATCH', url: `/api/accounts/${id}`, headers, payload: { alias: 'Renamed' } })).json().account.originalName, 'demo');
    await b.app.inject({ method: 'POST', url: '/api/auth/logout', headers });
    assert.equal((await b.app.inject({ url: '/api/session', headers })).statusCode, 401);
  } finally { await b.close(); }
});
test('session capabilities omit credentials, fixed paths and endpoints', async () => {
  const b = await setup(); try {
    const res = await b.app.inject({ url: '/api/session', headers: await login(b.app) });
    for (const secret of [b.dir, b.config.gitlab!.token, b.config.auth.cookieSecret, 'model.example.test', DUMMY_KEY]) assert.ok(!res.body.includes(secret));
    assert.deepEqual(res.json().capabilities.projects, [{ id: 'demo', label: 'Demo project' }]);
  } finally { await b.close(); }
});
test('login attempts are rate limited and errors redacted', async () => {
  const b = await setup(); try {
    for (let i = 0; i < 10; i++) assert.equal((await b.app.inject({ method: 'POST', url: '/api/auth/login', headers: { host: 'localhost:4310', origin: 'http://localhost:4310' }, payload: { password: DUMMY_KEY } })).statusCode, 401);
    const res = await b.app.inject({ method: 'POST', url: '/api/auth/login', headers: { host: 'localhost:4310', origin: 'http://localhost:4310' }, payload: { password: DUMMY_KEY } });
    assert.equal(res.statusCode, 429); assert.ok(!res.body.includes(DUMMY_KEY));
  } finally { await b.close(); }
});
test('safe URLs reject executable, foreign, credential and control-character links', () => {
  for (const url of ['javascript:alert(1)', 'https://u:p@gitlab.example.test/x', 'https://evil.test/a', 'https://gitlab.example.test/\nx']) assert.equal(safeUrl(url, 'https://gitlab.example.test'), null);
  assert.equal(safeUrl('https://gitlab.example.test/a?token=dummy#x', 'https://gitlab.example.test'), 'https://gitlab.example.test/a');
});
test('redaction excludes known secrets, token patterns, basic auth URLs and private paths', () => {
  const clean = redact(`${DUMMY_KEY} custom-secret password=dummy https://u:p@host/a /home/dummy/sessions/raw`, ['custom-secret']);
  for (const bad of [DUMMY_KEY, 'custom-secret', 'password=dummy', 'u:p', '/home/dummy']) assert.ok(!clean.includes(bad));
  assert.ok(!JSON.stringify(safeError(new Error(DUMMY_KEY))).includes(DUMMY_KEY));
});
test('private file ownership/mode/symlink and database/WAL permissions', () => {
  const b = sandbox(); try {
    const file = join(b.dir, 'dummy-secret'); writeFileSync(file, 'dummy-only', { mode: 0o600 });
    assert.equal(readPrivateFile(file), 'dummy-only'); chmodSync(file, 0o644); assert.throws(() => readPrivateFile(file));
    chmodSync(file, 0o600); const link = join(b.dir, 'link'); symlinkSync(file, link); assert.throws(() => readPrivateFile(link));
    b.store.set('dummy', { ok: true });
    assert.equal(statSync(b.store.file).mode & 0o777, 0o600);
    assert.equal(statSync(`${b.store.file}-wal`).mode & 0o777, 0o600);
    // SQLite EXCLUSIVE WAL keeps the shared index in memory, so -shm need not exist.
    if (existsSync(`${b.store.file}-shm`)) assert.equal(statSync(`${b.store.file}-shm`).mode & 0o777, 0o600);
    assert.equal(statSync(join(b.dir, 'runtime')).mode & 0o777, 0o700);
  } finally { b.cleanup(); }
});
test('malformed CSRF and encoded API paths cannot bypass authentication', async () => {
  const b = await setup(); try {
    const headers = await login(b.app);
    const logout = await b.app.inject({ method: 'POST', url: '/api/auth/logout', headers: { ...headers, 'x-csrf-token': 'é'.repeat(64) } });
    assert.equal(logout.statusCode, 403);
    assert.equal((await b.app.inject({ url: '/api%2Fsession', headers: { host: 'localhost:4310' } })).statusCode, 400);
    assert.equal((await b.app.inject({ url: '/api', headers: { host: 'localhost:4310' } })).statusCode, 401);
  } finally { await b.close(); }
});
test('second database process/connection cannot become another paid-job consumer', async () => {
  const b = sandbox();
  try {
    const { Store } = await import('../server/store.js');
    assert.throws(() => new Store(join(b.dir, 'runtime')), /locked/);
    assert.equal(b.store.get('missing'), null);
  } finally { b.cleanup(); }
});
test('calendar date bounds and malicious service paths rejected', () => {
  assert.equal(dateSchema.safeParse('2026-02-30').success, false);
  assert.equal(dateSchema.safeParse('2024-02-29').success, true);
  for (const path of ['//evil.test', '/a\\b', '/x?token=y']) assert.equal(servicePatchSchema.safeParse({ path }).success, false);
  assert.equal(mrFilterSchema.safeParse({ updatedAfter: '2026-10-21', updatedBefore: '2026-10-01' }).success, false);
});
test('HTTP redirects disabled and body limits/errors do not dump upstream', async () => {
  let redirect: unknown;
  const fetcher: typeof fetch = async (_u, init) => { redirect = init?.redirect; return new Response(JSON.stringify({ dummy: 'x'.repeat(100) })); };
  await assert.rejects(fetchJson(fetcher, 'https://example.test', {}, 1000, 10)); assert.equal(redirect, 'error');
  await assert.rejects(fetchJson(async () => new Response(DUMMY_KEY, { status: 403 }), 'https://example.test'), e => !JSON.stringify(e).includes(DUMMY_KEY));
});
