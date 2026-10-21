import test from 'node:test';
import assert from 'node:assert/strict';
import { sandbox, DUMMY_KEY, jsonResponse } from './helpers.js';
import { Accounts, parseCredentials, parseBuckets } from '../server/adapters/stepcode.js';
import { GitLab } from '../server/adapters/gitlab.js';
import { Cache } from '../server/cache.js';
import { mrFilterSchema } from '../shared/contracts.js';

const mr = { id: 1, iid: 3, project_id: 10, title: 'Dummy MR', state: 'merged', source_branch: 'feat', target_branch: 'main',
  updated_at: '2026-10-21T00:00:00Z', web_url: 'https://gitlab.example.test/team/demo/-/merge_requests/3', author: { id: 7 },
  references: { full: 'team/demo!3' }, reviewers: [{ name: 'Dummy reviewer' }], head_pipeline: { status: 'success' } };

test('credential parsing matches existing optional-colon/comment regex and stable name identity', () => {
  const text = `# ignored\ndemo: ${DUMMY_KEY} # dummy\nsecond ${DUMMY_KEY}\ninvalid line`;
  assert.deepEqual([...parseCredentials(text).keys()], ['demo', 'second']);
  assert.throws(() => parseCredentials(`demo ${DUMMY_KEY}\ndemo ${DUMMY_KEY}`));
});
test('all usage and budget pool buckets preserved without nonfinite or fabricated values', () => {
  const buckets = parseBuckets({ data: { usages: [{ name: 'promo', used: 2, total: 10, remaining: 8, currency: 'CNY', period: 'one-time' },
    { name: 'personal', used: '4.5', total: 'Infinity', remaining: NaN }], budgetPool: { bonus: { usedBudget: 1, totalBudget: 3, remainingBudget: 2, expires_at: '2026-11-01' } } } });
  assert.equal(buckets.length, 3); assert.equal(buckets[1].used, 4.5); assert.equal(buckets[1].total, null);
  assert.equal(buckets[1].remaining, null); assert.equal(buckets[2].source, 'budgetPool');
  assert.throws(() => parseBuckets({ usages: [{ total: Infinity }] }));
});
test('account direct API refresh uses fixed URL and per-account cache; no secret DTO', async () => {
  const b = sandbox(); let calls = 0;
  try {
    const a = new Accounts(b.config, b.store, async (url, init) => {
      calls++; assert.equal(String(url), 'https://usage.example.test/desk/v1/stepcode/user/info');
      assert.equal((init?.headers as Record<string, string>).Authorization, `Bearer ${DUMMY_KEY}`);
      return jsonResponse({ usages: [{ name: 'promo', used: 3, total: 10, remaining: 7 }] });
    }, () => `demo ${DUMMY_KEY}`);
    const original = a.list()[0]; assert.equal(calls, 0);
    a.patch(original.id, { alias: 'Weekly account', tags: ['weekly-report'], notes: 'Dummy notes' });
    const updated = (await a.refresh())[0]; await a.refresh();
    assert.equal(calls, 1); assert.equal(updated.id, original.id); assert.equal(updated.originalName, 'demo');
    assert.equal(updated.alias, 'Weekly account'); assert.equal(updated.usage.data[0].remaining, 7);
    assert.ok(!JSON.stringify(updated).includes(DUMMY_KEY));
  } finally { b.cleanup(); }
});
test('bounded account concurrency, independent failure and retained stale balance', async () => {
  const b = sandbox(); let active = 0, max = 0, fail = false;
  try {
    b.config.stepcode!.concurrency = 2;
    const a = new Accounts(b.config, b.store, async (_url, init) => {
      active++; max = Math.max(max, active); await new Promise(r => setTimeout(r, 5)); active--;
      if (fail && (init?.headers as Record<string, string>).Authorization.includes('dummy_key')) throw new Error(DUMMY_KEY);
      return jsonResponse({ usages: [{ name: 'promo', remaining: 4 }] });
    }, () => `demo ${DUMMY_KEY}\na ak-other_dummy_value_123456\nb ak-another_dummy_value_123456`);
    await a.refresh(undefined, true); fail = true; const list = await a.refresh(undefined, true);
    assert.ok(max <= 2); assert.equal(list[0].usage.stale, true); assert.equal(list[0].usage.data[0].remaining, 4);
    assert.equal(list[1].usage.stale, false); assert.ok(!JSON.stringify(list).includes(DUMMY_KEY));
  } finally { b.cleanup(); }
});
test('cache deduplicates overlapping refresh and preserves stale data/error on permission failure', async () => {
  const b = sandbox(); try {
    const cache = new Cache(b.store); let calls = 0;
    const load = async () => { calls++; await new Promise(r => setTimeout(r, 10)); return [1]; };
    await Promise.all([cache.refresh('x', [], 1000, load, true), cache.refresh('x', [], 1000, load, true)]); assert.equal(calls, 1);
    const old = await cache.refresh('x', [], 1000, async () => { throw new Error(DUMMY_KEY); }, true);
    assert.deepEqual(old.data, [1]); assert.equal(old.stale, true); assert.equal(old.error!.code, 'integration_failed');
  } finally { b.cleanup(); }
});
test('installed Stepcode channel/cycle schema derives finite remaining and truthful unlimited', () => {
  const buckets = parseBuckets({ code: 200, data: { usages: [
    { channel_name: 'Promo', channel_id: 'promo-id', usage_type: 'monthly', reset_cycle: 'monthly', used: 2, total: 10, used_percent: 20 },
    { channel_id: 'unlimited', unlimited: true, used: 100, total: 0, used_percent: 0 },
    { channel_name: 'Over', used: 11, total: 10 },
    { channel_id: 'explicit', used: 2, total: 10, remaining: 5 },
  ], budget_pool: [{ channel_name: 'Pool', usage_type: 'one-time', used: '1.5', total: '3', currency: 'USD' }] } });
  assert.equal(buckets.length, 5); assert.equal(buckets[0].label, 'Promo'); assert.equal(buckets[0].period, 'monthly');
  assert.equal(buckets[0].remaining, 8); assert.equal(buckets[0].currency, null);
  assert.equal(buckets[1].label, 'unlimited'); assert.equal(buckets[1].remaining, null); assert.equal(buckets[1].total, null);
  assert.equal(buckets[2].remaining, 0); assert.equal(buckets[3].remaining, 5); assert.equal(buckets[4].remaining, 1.5);
  assert.equal(parseBuckets({ usages: [{ unlimited: true }] })[0].remaining, null);
  assert.equal(parseBuckets({ usages: [{ used: 0, total: 0, used_percent: 0 }] })[0].remaining, null);
  assert.equal(parseBuckets({ usages: [{ used: -Number.MAX_VALUE, total: Number.MAX_VALUE }] })[0].remaining, null);
  assert.throws(() => parseBuckets({ code: 403, data: { usages: [{ remaining: 100 }] } }));
});
test('rotated keys and normalized endpoints isolate usage cache without changing account metadata', async () => {
  const b = sandbox(); let key = DUMMY_KEY, calls = 0;
  const fetcher: typeof fetch = async () => { calls++; return jsonResponse({ usages: [{ used: calls, total: 10 }] }); };
  try {
    const a = new Accounts(b.config, b.store, fetcher, () => `demo ${key}`);
    const initial = (await a.refresh())[0]; a.patch(initial.id, { alias: 'stable alias' });
    key = 'ak-rotated_dummy_value_123456'; const changed = a.list()[0];
    assert.equal(changed.id, initial.id); assert.equal(changed.alias, 'stable alias'); assert.equal(changed.usage.stale, true);
    assert.equal(changed.usage.updatedAt, null); assert.equal(changed.usage.data.length, 0);
    await a.refresh(); assert.equal(calls, 2);
    const normalized = new Accounts({ ...b.config, stepcode: { ...b.config.stepcode!, baseUrl: 'https://USAGE.example.test:443/' } }, b.store, fetcher, () => `demo ${key}`);
    await normalized.refresh(); assert.equal(calls, 2);
    const different = new Accounts({ ...b.config, stepcode: { ...b.config.stepcode!, baseUrl: 'https://other.example.test' } }, b.store, fetcher, () => `demo ${key}`);
    assert.equal(different.list()[0].usage.stale, true); await different.refresh(); assert.equal(calls, 3);
    for (const text of [JSON.stringify(a.list()), JSON.stringify(different.list())]) {
      assert.ok(!text.includes(key)); assert.ok(!text.includes('fingerprint')); assert.ok(!text.includes('usage.example.test'));
    }
  } finally { b.cleanup(); }
});

test('GitLab identifies own author and uses global scope=all with exact server pagination', async () => {
  const b = sandbox(); const urls: URL[] = [];
  try {
    const gitlab = new GitLab(b.config, b.store, async url => {
      const u = new URL(String(url)); urls.push(u);
      return u.pathname.endsWith('/user') ? jsonResponse({ id: 7, bot: false }) : jsonResponse([mr], { 'x-next-page': '3' });
    });
    const result = await gitlab.refresh({ state: 'merged', page: 2, perPage: 1 });
    const q = urls[1].searchParams;
    assert.equal(q.get('scope'), 'all'); assert.equal(q.get('author_id'), '7'); assert.equal(q.get('state'), 'merged');
    assert.equal(q.get('page'), '2'); assert.equal(result.data.hasMore, true); assert.equal(result.data.nextPage, 3);
    assert.equal(result.data.items[0].pipelineStatus, 'success'); assert.equal(result.error, null);
    assert.equal(gitlab.read(mrFilterSchema.parse({ state: 'opened' })).updatedAt, null);
  } finally { b.cleanup(); }
});
test('bot token requires explicitly configured human author binding', async () => {
  const b = sandbox(); try {
    const fetcher: typeof fetch = async url => String(url).endsWith('/user') ? jsonResponse({ id: 999, bot: true }) : jsonResponse([mr], { 'x-next-page': '' });
    const bot = new GitLab(b.config, b.store, fetcher);
    const denied = await bot.refresh({}); assert.equal(denied.error!.code, 'bot_author_binding_required'); assert.equal(denied.stale, true);
    b.config.gitlab!.authorId = 7; b.config.gitlab!.botBindingExplicit = true;
    const result = await bot.refresh({}, true); assert.equal(result.data.authorId, 7); assert.equal(result.error, null);
  } finally { b.cleanup(); }
});
test('MR pagination empty next header is authoritative and malicious link DTO dropped', async () => {
  const b = sandbox(); try {
    const gitlab = new GitLab(b.config, b.store, async url => String(url).endsWith('/user') ? jsonResponse({ id: 7 }) : jsonResponse([{ ...mr, web_url: 'javascript:alert(1)' }], { 'x-next-page': '' }));
    const page = await gitlab.refresh({ perPage: 1 });
    assert.equal(page.data.hasMore, false); assert.equal(page.data.nextPage, null); assert.equal(page.data.items[0].url, null);
  } finally { b.cleanup(); }
});
test('permission failure is not represented as authoritative no MRs and cache stays stale', async () => {
  const b = sandbox(); let denied = false;
  try {
    const gitlab = new GitLab(b.config, b.store, async url => String(url).endsWith('/user') ? jsonResponse({ id: 7 }) : denied ? new Response(DUMMY_KEY, { status: 403 }) : jsonResponse([mr], { 'x-next-page': '' }));
    await gitlab.refresh({}); denied = true; const next = await gitlab.refresh({}, true);
    assert.equal(next.error!.code, 'upstream_permission_denied'); assert.equal(next.stale, true); assert.equal(next.data.items.length, 1);
    assert.ok(!JSON.stringify(next).includes(DUMMY_KEY));
  } finally { b.cleanup(); }
});
test('MR wrong-author data rejected and full page without pagination is not guessed', async () => {
  const b = sandbox(); let wrong = true; try {
    const gitlab = new GitLab(b.config, b.store, async url => String(url).endsWith('/user') ? jsonResponse({ id: 7 }) : jsonResponse([{ ...mr, author: { id: wrong ? 9 : 7 } }]));
    assert.equal((await gitlab.refresh({ perPage: 1 })).error!.code, 'upstream_author_mismatch');
    wrong = false; assert.equal((await gitlab.refresh({ perPage: 1 }, true)).error!.code, 'pagination_unavailable');
  } finally { b.cleanup(); }
});
