import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { hashPassword } from '../server/security/auth.js';
import { validateConfig, type Config } from '../server/config.js';
import { Store } from '../server/store.js';
import { buildApp, type AppOptions } from '../server/app.js';
import type { Evidence, CollectInput } from '../shared/contracts.js';

export const PASSWORD = 'dummy-test-password-only';
export const DUMMY_KEY = 'ak-dummy_key_not_real_12345678';
const PASSWORD_HASH = hashPassword(PASSWORD, '00000000000000000000000000000000');
export function sandbox() {
  const dir = mkdtempSync(join(tmpdir(), 'eh-backend-')); chmodSync(dir, 0o700);
  const skill = join(dir, 'skill'); mkdirSync(join(skill, 'references'), { recursive: true, mode: 0o700 });
  writeFileSync(join(skill, 'references/style-guide.md'), 'Mirror Situation/Action/Result. Preserve fragments and unfinished Todo; never invent facts.');
  writeFileSync(join(skill, 'references/template.md'), 'Situation\nAction\nOthers:\nResult\nTodo\n');
  const root = join(dir, 'project'); mkdirSync(root, { mode: 0o700 });
  const config = validateConfig({ publicOrigin: 'http://localhost:4310', auth: { passwordHash: PASSWORD_HASH,
    cookieSecret: 'dummy_cookie_secret_12345678901234567890', secureCookie: false },
    stepcode: { baseUrl: 'https://usage.example.test', ttlMs: 60000 },
    gitlab: { baseUrl: 'https://gitlab.example.test', token: 'dummy_gitlab_token_not_real', ttlMs: 60000 },
    projectRoots: [{ id: 'demo', label: 'Demo project', root }],
    reports: { skillPath: skill, python: '/usr/bin/python3.10', models: [{ id: 'dummy', label: 'Dummy model', model: 'dummy-model',
      endpoint: 'https://model.example.test/v1/chat/completions', apiKey: 'dummy_model_key_not_real' }] },
  });
  const store = new Store(join(dir, 'runtime'));
  return { dir, config, store, cleanup: () => { store.close(); rmSync(dir, { recursive: true, force: true }); } };
}
export async function setup(options: AppOptions = {}, alter?: (config: Config) => void) {
  const box = sandbox(); alter?.(box.config);
  const app = await buildApp(box.config, box.store, { fetcher: async () => { throw new Error('dummy upstream failure'); }, readKeys: () => `demo ${DUMMY_KEY}\n`, ...options });
  return { ...box, app, close: async () => { await app.close(); box.cleanup(); } };
}
export async function login(app: Awaited<ReturnType<typeof buildApp>>) {
  const response = await app.inject({ method: 'POST', url: '/api/auth/login', headers: { host: 'localhost:4310', origin: 'http://localhost:4310' }, payload: { password: PASSWORD } });
  if (response.statusCode !== 200) throw new Error(`Dummy login failed ${response.statusCode}`);
  const cookie = String(response.headers['set-cookie']).split(';')[0];
  return { host: 'localhost:4310', origin: 'http://localhost:4310', cookie, 'x-csrf-token': response.json().csrfToken as string };
}
export const collectInput: CollectInput = { week: '2026-10-21', projectIds: ['demo'], consentScan: true, includeContext: false,
  fragments: 'Keep this fragment', previousTodo: 'Carry forward this Todo', idempotencyKey: 'collect-1' };
export const emptyEvidence: Evidence = { weekStart: '2026-10-19', weekEnd: '2026-10-26', commits: [], warnings: [] };
export function jsonResponse(data: unknown, headers: Record<string, string> = {}) { return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json', ...headers } }); }
