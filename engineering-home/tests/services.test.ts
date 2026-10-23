import test from 'node:test';
import assert from 'node:assert/strict';
import { parseSs, parseShow, parseDockerPorts, ownedProc, associatedUnit, mergeServices, serviceLinks, Services } from '../server/adapters/services.js';
import { runCommand } from '../server/adapters/process.js';
import { sandbox } from './helpers.js';
import type { Service } from '../shared/contracts.js';

const show = 'Id=demo.service\nDescription=Demo backend\nActiveState=active\nSubState=running\nMainPID=12\nControlGroup=/user.slice/user-1000.slice/demo.service\n\nId=idle.socket\nDescription=Idle socket\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlGroup=\n';
const baseService = (): Service => ({ id: 'demo', name: 'Demo', alias: '', notes: '', category: '', favorite: false, sources: ['systemd'],
  state: 'active', endpoints: [], discoveredAt: '2026-10-21T00:00:00Z', protocol: 'none', path: '/' });
test('systemd installed inactive socket and no-endpoint units survive parsing', () => {
  const units = parseShow(show); assert.equal(units.length, 2); assert.equal(units[1].Id, 'idle.socket');
  assert.equal(units[1].ActiveState, 'inactive'); assert.equal(units[1].ControlGroup, '');
});
test('ss IPv4/IPv6 wildcard and loopback parsing rejects unowned/unknown process association', () => {
  const listeners = parseSs('LISTEN 0 128 127.0.0.1:4310 0.0.0.0:* users:(("node",pid=12,fd=20))\nLISTEN 0 128 [::1]:18889 [::]:* users:(("node",pid=13,fd=2))\nLISTEN 0 128 [::]:8090 [::]:*\nLISTEN 0 128 evil.test:80 *:*');
  assert.equal(listeners.length, 3); assert.equal(listeners[1].address, '::1'); assert.deepEqual(listeners[2].pids, []);
  assert.equal(ownedProc('Uid:\t1000\t1000\t1000\t1000\n', 1000), true);
  assert.equal(ownedProc('Uid:\t1001\t1001\t1001\t1001\n', 1000), false);
  const units = parseShow(show);
  assert.equal(associatedUnit('0::/user.slice/user-1000.slice/demo.service/child', units)?.Id, 'demo.service');
  assert.equal(associatedUnit('0::/user.slice/user-1000.slice/demo.serviceevil', units), undefined);
});
test('rootless Docker publishes only explicit TCP mappings, retains IPv6 and no guessed exposed ports', () => {
  const ports = parseDockerPorts('0.0.0.0:7909->3000/tcp, [::]:7909->3000/tcp, 3306/tcp, 127.0.0.1:8000->8080/udp');
  assert.equal(ports.length, 2); assert.equal(ports[0].port, 7909); assert.equal(ports[1].address, '::');
});
test('links require explicit protocol; loopback stays local and wildcard never remote rewrite', () => {
  const s = baseService(); s.endpoints = [{ address: '::1', port: 18889, transport: 'tcp', url: null, localOnly: true, reachability: 'unverified' },
    { address: '0.0.0.0', port: 7909, transport: 'tcp', url: null, localOnly: false, reachability: 'unverified' }];
  assert.equal(serviceLinks(s).endpoints[0].url, null);
  s.protocol = 'http'; s.path = '/ui'; const links = serviceLinks(s).endpoints;
  assert.equal(links[0].url, 'http://[::1]:18889/ui'); assert.equal(links[1].url, 'http://127.0.0.1:7909/ui');
  assert.equal(links[1].localOnly, true); assert.equal(links[1].reachability, 'unverified');
});
test('duplicate process/Docker endpoints merge, unnetworked units remain', () => {
  const docker = baseService(); docker.id = 'docker'; docker.sources = ['docker']; docker.endpoints = parseDockerPorts('0.0.0.0:7909->3000/tcp, [::]:7909->3000/tcp');
  const process = { ...docker, id: 'process', sources: ['process'] };
  const result = mergeServices([baseService(), docker, process]); assert.equal(result.length, 2);
  assert.equal(result[0].endpoints.length, 0); assert.deepEqual(result[1].sources, ['docker', 'process']); assert.equal(result[1].endpoints.length, 2);
});
test('real service collector uses fixed systemctl argv, UID/cgroup ownership and metadata persists', async () => {
  const b = sandbox(); const commands: { command: string; args: string[] }[] = [];
  try {
    const services = new Services(b.config, b.store, async (command, args) => {
      commands.push({ command, args });
      if (command === '/usr/bin/systemctl') {
        if (args.includes('list-units')) return 'demo.service loaded active running Demo\n';
        if (args.includes('list-unit-files')) return 'demo.service enabled\nidle.socket disabled\n';
        return show;
      }
      if (command === '/usr/bin/ss') return args.includes('-4') ? 'LISTEN 0 128 127.0.0.1:4310 0.0.0.0:* users:(("node",pid=12,fd=2))' : '';
      throw new Error('dummy unavailable');
    }, path => path.endsWith('/status') ? 'Uid:\t1000\t1000\t1000\t1000' : '0::/user.slice/user-1000.slice/demo.service/child', 1000);
    assert.equal(services.read().data.length, 0);
    const result = await services.refresh(true); assert.equal(result.data.length, 2);
    const active = result.data.find(s => s.state === 'active/running')!; assert.equal(active.endpoints.length, 1);
    services.patch(active.id, { alias: 'Friendly', protocol: 'http', notes: 'Keep me' }); await services.refresh(true);
    assert.equal(services.read().data.find(s => s.id === active.id)!.notes, 'Keep me');
    assert.ok(commands.every(c => !c.args.includes('inspect')));
    const showArgs = commands.find(c => c.args.includes('show'))!.args;
    assert.ok(showArgs.includes('--property=Id,Description,ActiveState,SubState,MainPID,ControlGroup'));
  } finally { b.cleanup(); }
});
test('family hinted star listeners map deterministically without guessed remote hosts', async () => {
  const line = 'LISTEN 0 128 *:8090 *:* users:(("node",pid=12,fd=2))';
  assert.equal(parseSs(line, 4)[0].address, '0.0.0.0'); assert.equal(parseSs(line, 6)[0].address, '::');
  assert.equal(parseSs(line).length, 0);
  const b = sandbox(); const calls: string[][] = [];
  try {
    const services = new Services(b.config, b.store, async (command, args) => {
      if (command === '/usr/bin/ss') { calls.push(args); return `${line}\n${line}\nLISTEN 0 128 *:8091 *:* users:(("foreign",pid=13,fd=2))`; }
      return '';
    }, path => path.endsWith('/status') ? path.includes('/13/') ? 'Uid:\t1001\t1001\t1001\t1001' : 'Uid:\t1000\t1000\t1000\t1000' : '0::/unknown', 1000);
    const result = await services.refresh(true);
    assert.deepEqual(calls, [['-H', '-4', '-ltnp'], ['-H', '-6', '-ltnp']]);
    assert.equal(result.data.length, 1); assert.equal(result.data[0].endpoints.length, 2);
    const linked = serviceLinks({ ...result.data[0], protocol: 'http' });
    assert.deepEqual(linked.endpoints.map(e => e.url).sort(), ['http://127.0.0.1:8090/', 'http://[::1]:8090/'].sort());
    assert.ok(linked.endpoints.every(e => e.localOnly && e.reachability === 'unverified'));
  } finally { b.cleanup(); }
});
test('fixed child runner output and timeout bounds safely stop process groups', async () => {
  await assert.rejects(runCommand('/usr/bin/python3', ['-c', 'print("x" * 10000)'], { maxBytes: 20 }), e => (e as { code: string }).code === 'collector_output_limit');
  await assert.rejects(runCommand('/usr/bin/python3', ['-c', 'import time; time.sleep(10)'], { timeoutMs: 50 }), e => (e as { code: string }).code === 'collector_timeout');
  assert.equal((await runCommand('/usr/bin/python3', ['-c', 'print("dummy")'])).trim(), 'dummy');
});
