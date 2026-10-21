import test from 'node:test';
import assert from 'node:assert/strict';
import { parseBuckets } from '../server/adapters/stepcode.js';
import { GitLab } from '../server/adapters/gitlab.js';
import { serviceLinks, mergeServices, parseShow, Services } from '../server/adapters/services.js';
import { runCommand } from '../server/adapters/process.js';
import { sandbox, jsonResponse } from './helpers.js';
import type { Service } from '../shared/contracts.js';

test('actual budget_pool_usages stays separate from channel usages and summary', () => {
  const buckets = parseBuckets({ code: 200, data: {
    usages: [{ channel_name: 'Promo', used: 2, total: 10 }],
    budget_pool_usages: [{ channel_name: 'Shared pool', used: 5, total: 20 }],
    usage_summary: { used: 7, total: 30 },
  } });
  assert.equal(buckets.length, 2);
  assert.equal(buckets[1].source, 'budgetPool');
  assert.equal(buckets[1].remaining, 15);
  assert.equal(parseBuckets({ usages: [{ unlimited: true, remaining: 99 }] })[0].remaining, null);
});

test('project-filtered MRs use documented project endpoint with own author', async () => {
  const box = sandbox(); const urls: URL[] = [];
  try {
    const gitlab = new GitLab(box.config, box.store, async value => {
      const url = new URL(String(value)); urls.push(url);
      return url.pathname.endsWith('/user') ? jsonResponse({ id: 7 }) : jsonResponse([], { 'x-next-page': '' });
    });
    const snapshot = await gitlab.refresh({ projectId: 10, state: 'all' }, true);
    assert.equal(snapshot.error, null);
    assert.equal(urls[1].pathname, '/api/v4/projects/10/merge_requests');
    assert.equal(urls[1].searchParams.get('author_id'), '7');
    assert.equal(urls[1].searchParams.has('project_id'), false);
  } finally { box.cleanup(); }
});

test('explicit access address makes wildcard candidate link without rewriting loopback', () => {
  const service: Service = { id: 'demo', name: 'Demo', alias: '', notes: '', category: '', favorite: false,
    protocol: 'http', path: '/ui', sources: ['docker'], state: 'running', discoveredAt: '', endpoints: [
      { address: '0.0.0.0', port: 8090, transport: 'tcp', url: null, localOnly: false, reachability: 'unverified' },
      { address: '127.0.0.1', port: 8080, transport: 'tcp', url: null, localOnly: true, reachability: 'unverified' },
      { address: '::', port: 8090, transport: 'tcp', url: null, localOnly: false, reachability: 'unverified' },
    ] };
  const linked = serviceLinks(service, '192.0.2.10').endpoints;
  assert.equal(linked[0].address, '0.0.0.0');
  assert.equal(linked[0].url, 'http://192.0.2.10:8090/ui');
  assert.equal(linked[0].localOnly, false); assert.equal(linked[0].reachability, 'unverified');
  assert.equal(linked[1].url, 'http://127.0.0.1:8080/ui'); assert.equal(linked[1].localOnly, true);
  assert.equal(linked[2].url, 'http://[::1]:8090/ui'); assert.equal(linked[2].localOnly, true);
  assert.equal(serviceLinks(service, '2001:db8::1').endpoints[2].url, 'http://[2001:db8::1]:8090/ui');
});

test('rootless forwarding preserves individual container IDs and daemon no-endpoint row', () => {
  const base: Service = { id: 'daemon', name: 'Rootless Docker', alias: '', notes: '', category: '', favorite: false,
    protocol: 'none', path: '/', sources: ['systemd'], state: 'active/running', discoveredAt: '', endpoints: [] };
  const endpoint = (port: number) => ({ address: '0.0.0.0', port, transport: 'tcp' as const, url: null, localOnly: false, reachability: 'unverified' as const });
  const daemon = { ...base, endpoints: [endpoint(8080), endpoint(5432)] };
  const web = { ...base, id: 'web', name: 'web', sources: ['docker'], endpoints: [endpoint(8080)] };
  const db = { ...base, id: 'db', name: 'postgres', sources: ['docker'], endpoints: [endpoint(5432)] };
  const merged = mergeServices([daemon, web, db]);
  assert.equal(merged.length, 3); assert.equal(merged.find(s => s.id === 'daemon')!.endpoints.length, 0);
  assert.equal(merged.find(s => s.id === 'web')!.name, 'web'); assert.equal(merged.find(s => s.id === 'db')!.name, 'postgres');
});

test('escaped systemd names survive listing and show parsing as literal argv', async () => {
  const id = String.raw`worker@project\x2dalpha.service`;
  const show = `Id=${id}\nDescription=Worker\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlGroup=\n`;
  assert.equal(parseShow(show)[0].Id, id);
  const box = sandbox(); let showIds: string[] = [];
  try {
    const services = new Services(box.config, box.store, async (command, args) => {
      if (command !== '/usr/bin/systemctl') return '';
      if (args.includes('list-unit-files')) return `${id} disabled\n`;
      if (args.includes('list-units')) return '';
      showIds = args.slice(args.indexOf('--') + 1); return show;
    });
    const snapshot = await services.refresh(true);
    assert.deepEqual(showIds, [id]); assert.equal(snapshot.data[0].state, 'inactive/dead');
  } finally { box.cleanup(); }
});

test('systemd template and missing details preserve concrete records on partial show exit', async () => {
  const box = sandbox(); let showIds: string[] = [];
  try {
    const services = new Services(box.config, box.store, async (command, args, options) => {
      if (command !== '/usr/bin/systemctl') return '';
      if (args.includes('list-units')) return '';
      if (args.includes('list-unit-files')) return 'worker@.service disabled\nworker@one.service enabled\nmissing.service enabled\n';
      showIds = args.slice(args.indexOf('--') + 1);
      assert.deepEqual(options?.acceptedExitCodes, [1]);
      return 'Id=worker@one.service\nDescription=Concrete worker\nActiveState=active\nSubState=running\nMainPID=0\nControlGroup=\n';
    });
    const result = await services.collect();
    assert.ok(!showIds.includes('worker@.service'));
    assert.equal(result.data.length, 3);
    assert.ok(result.data.some(s => s.state === 'template/not-instantiated'));
    assert.ok(result.data.some(s => s.name === 'Concrete worker'));
    assert.ok(result.warnings.includes('some_unit_details_unavailable'));
  } finally { box.cleanup(); }
});

test('fixed collector may accept explicitly expected nonzero exit without weakening defaults', async () => {
  const args = ['-c', 'import sys;print("partial-result");sys.exit(1)'];
  await assert.rejects(runCommand('/usr/bin/python3', args));
  assert.equal((await runCommand('/usr/bin/python3', args, { acceptedExitCodes: [1] })).trim(), 'partial-result');
});
