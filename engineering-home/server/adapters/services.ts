import { readFileSync, lstatSync } from 'node:fs';
import { isIP } from 'node:net';
import type { Endpoint, Service, Snapshot } from '../../shared/contracts.js';
import { servicePatchSchema } from '../../shared/contracts.js';
import type { Config } from '../config.js';
import type { Store } from '../store.js';
import { Cache } from '../cache.js';
import { assertNoSymlinks } from '../security/files.js';
import { redact, safeError, SafeError, stableId } from '../security/sanitize.js';
import { configSecrets } from './weekly-report.js';
import { runCommand, type CommandRunner } from './process.js';

export type Listener = { address: string; port: number; pids: number[]; name: string };
export type Unit = { Id: string; Description: string; ActiveState: string; SubState: string; MainPID: string; ControlGroup: string };
const blank = (id: string, name: string, source: string, state: string): Service => ({
  id, name: redact(name), alias: '', notes: '', category: '', favorite: false, protocol: 'none', path: '/',
  sources: [source], state, endpoints: [], discoveredAt: new Date().toISOString(),
});
export function parseAddress(value: string, family?: 4 | 6): { address: string; port: number } | null {
  const m = /^(?:\[([^\]]+)\]|(.+)):(\d+)$/.exec(value);
  if (!m) return null;
  const raw = (m[1] ?? m[2]).replace(/%[\w.-]+$/, ''); const port = Number(m[3]);
  const address = raw === '*' && family ? family === 4 ? '0.0.0.0' : '::' : raw;
  if (!isIP(address) || family && isIP(address) !== family || port < 1 || port > 65535) return null;
  return { address, port };
}
export function parseSs(text: string, family?: 4 | 6): Listener[] {
  return text.split('\n').flatMap(line => {
    const parts = line.trim().split(/\s+/); if (parts[0] !== 'LISTEN' || parts.length < 5) return [];
    const endpoint = parseAddress(parts[3], family); if (!endpoint) return [];
    const pids = [...line.matchAll(/pid=(\d+)/g)].map(m => Number(m[1]));
    return [{ ...endpoint, pids: [...new Set(pids)], name: /\(\("([^"\n]{1,120})"/.exec(line)?.[1] ?? 'Unlabelled process' }];
  });
}
const unitId = /^(?:[\w@.:+-]|\\x[0-9a-fA-F]{2})+\.(?:service|socket)$/;
export function parseShow(text: string): Unit[] {
  return text.trim().split(/\n\s*\n/).flatMap(block => {
    const u: Unit = { Id: '', Description: '', ActiveState: 'unknown', SubState: '', MainPID: '0', ControlGroup: '' };
    for (const line of block.split('\n')) { const i = line.indexOf('='); const key = line.slice(0, i); if (Object.hasOwn(u, key)) u[key as keyof Unit] = line.slice(i + 1); }
    return unitId.test(u.Id) ? [u] : [];
  });
}
export function ownedProc(status: string, uid: number): boolean {
  const m = /^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$/m.exec(status);
  return !!m && m.slice(1).every(v => Number(v) === uid);
}
export function associatedUnit(cgroup: string, units: Unit[]): Unit | undefined {
  const paths = cgroup.split('\n').map(v => v.slice(v.indexOf(':', v.indexOf(':') + 1) + 1));
  return units.find(u => u.ControlGroup && paths.some(p => p === u.ControlGroup || p.startsWith(`${u.ControlGroup}/`)));
}
function endpoint(address: string, port: number): Endpoint {
  return { address, port, transport: 'tcp', url: null, localOnly: address === '::1' || address.startsWith('127.'), reachability: 'unverified' };
}
export function serviceLinks(service: Service, accessAddress?: string): Service {
  return { ...service, endpoints: service.endpoints.map(e => {
    let url: string | null = null;
    const wildcard = ['0.0.0.0', '::'].includes(e.address);
    const candidate = accessAddress && isIP(accessAddress) === (e.address === '0.0.0.0' ? 4 : 6) ? accessAddress : undefined;
    const a = wildcard ? candidate ?? (e.address === '0.0.0.0' ? '127.0.0.1' : '::1') : e.address;
    if (isIP(a) && ['http', 'https', 'ssh', 'tcp'].includes(service.protocol)) {
      const host = isIP(a) === 6 ? `[${a}]` : a;
      url = `${service.protocol}://${host}:${e.port}${['http', 'https'].includes(service.protocol) ? service.path : ''}`;
    }
    return { ...e, url, localOnly: a === '::1' || a.startsWith('127.') };
  }) };
}
export function parseDockerPorts(text: string): Endpoint[] {
  const out: Endpoint[] = [];
  for (const token of text.split(/,\s*/)) {
    const m = /^(.+):(\d+)->\d+\/tcp$/.exec(token.trim()); if (!m) continue;
    const p = parseAddress(`${m[1]}:${m[2]}`); if (p) out.push(endpoint(p.address, p.port));
  }
  return out;
}
export function mergeServices(services: Service[]): Service[] {
  const result: Service[] = [];
  const matches = (a: Endpoint, b: Endpoint) => a.port === b.port &&
    (a.address === b.address || ['0.0.0.0', '::'].includes(a.address) && ['0.0.0.0', '::'].includes(b.address));
  for (const service of services) {
    const duplicate = result.find(s => s.id === service.id);
    if (duplicate) { duplicate.sources = [...new Set([...duplicate.sources, ...service.sources])]; duplicate.endpoints.push(...service.endpoints); }
    else result.push({ ...service, sources: [...service.sources], endpoints: [...service.endpoints] });
  }
  const containers = result.filter(s => s.sources.includes('docker'));
  for (const service of result) {
    if (service.sources.includes('docker') || service.sources.includes('manual')) continue;
    service.endpoints = service.endpoints.filter(endpoint => {
      const container = containers.find(c => c.endpoints.some(e => matches(endpoint, e)));
      if (!container) return true;
      if (service.sources.includes('process')) container.sources = [...new Set([...container.sources, 'process'])];
      return false;
    });
  }
  return result.filter(s => !s.sources.includes('process') || s.sources.includes('systemd') || s.endpoints.length > 0)
    .map(s => ({ ...s, endpoints: s.endpoints.filter((e, i, all) => all.findIndex(p => p.port === e.port && p.address === e.address) === i) }));
}
export class Services {
  private cache: Cache;
  private inflight?: Promise<Snapshot<Service[]>>;
  constructor(private config: Config, private store: Store, private run: CommandRunner = runCommand,
    private procRead = (path: string) => readFileSync(path, 'utf8'), private uid = process.getuid!()) { this.cache = new Cache(store); }
  read(): Snapshot<Service[]> {
    const snap = this.cache.read<Service[]>('services', [], this.config.services.ttlMs);
    return { ...snap, data: snap.data.map(s => serviceLinks({ ...s, ...(this.store.get(`service:${s.id}`) ?? {}) }, this.config.services.accessAddress)) };
  }
  patch(id: string, body: unknown): Service {
    const item = this.read().data.find(s => s.id === id); if (!item) throw new SafeError('service_not_found', 404);
    const meta = servicePatchSchema.parse(body);
    for (const key of ['alias', 'notes', 'category'] as const) if (meta[key] !== undefined) meta[key] = redact(meta[key]!, configSecrets(this.config));
    this.store.set(`service:${id}`, { ...(this.store.get(`service:${id}`) ?? {}), ...meta });
    return this.read().data.find(s => s.id === id)!;
  }
  async collect(): Promise<{ data: Service[]; warnings: string[] }> {
    const warnings: string[] = [], data: Service[] = [], units: Unit[] = [];
    const env = { XDG_RUNTIME_DIR: `/run/user/${this.uid}`, DBUS_SESSION_BUS_ADDRESS: `unix:path=/run/user/${this.uid}/bus` };
    try {
      const [active, installed] = await Promise.all([
        this.run('/usr/bin/systemctl', ['--user', 'list-units', '--all', '--no-legend', '--plain', '--no-pager', '--type=service,socket'], { env }),
        this.run('/usr/bin/systemctl', ['--user', 'list-unit-files', '--no-legend', '--no-pager', '--type=service,socket'], { env }),
      ]);
      const ids = [...new Set([...active.split('\n'), ...installed.split('\n')].flatMap(line => { const id = line.trim().split(/\s+/)[0]; return unitId.test(id) ? [id] : []; }))];
      if (ids.length > 500) throw new SafeError('unit_limit');
      const concrete = ids.filter(id => !/@\.(?:service|socket)$/.test(id));
      for (let i = 0; i < concrete.length; i += 50) units.push(...parseShow(await this.run('/usr/bin/systemctl', ['--user', 'show', '--no-pager', '--property=Id,Description,ActiveState,SubState,MainPID,ControlGroup', '--', ...concrete.slice(i, i + 50)], { env, acceptedExitCodes: [1] })));
      for (const id of ids) if (!units.some(unit => unit.Id === id)) {
        const template = /@\.(?:service|socket)$/.test(id);
        units.push({ Id: id, Description: id, ActiveState: template ? 'template' : 'unknown', SubState: template ? 'not-instantiated' : 'unavailable', MainPID: '0', ControlGroup: '' });
        if (!template) warnings.push('some_unit_details_unavailable');
      }
      units.forEach(u => data.push(blank(stableId('unit', u.Id), u.Description || u.Id, 'systemd', `${u.ActiveState}/${u.SubState}`)));
    } catch { warnings.push('systemd_unavailable'); data.push(...this.read().data.filter(s => s.sources.includes('systemd'))); }
    const listeners: Service[] = [];
    for (const family of [4, 6] as const) try {
      for (const l of parseSs(await this.run('/usr/bin/ss', ['-H', `-${family}`, '-ltnp']), family)) {
        for (const pid of l.pids) {
          try {
            if (!ownedProc(this.procRead(`/proc/${pid}/status`), this.uid)) continue;
            const unit = associatedUnit(this.procRead(`/proc/${pid}/cgroup`), units);
            const s = unit ? data.find(v => v.id === stableId('unit', unit.Id))! : blank(stableId('process', `${pid}:${l.name}`), l.name, 'process', 'tcp_listening');
            s.endpoints.push(endpoint(l.address, l.port)); if (!unit && !listeners.includes(s)) listeners.push(s); break;
          } catch { continue; }
        }
      }
    } catch { warnings.push('listeners_unavailable'); }
    const socket = `/run/user/${this.uid}/docker.sock`;
    try {
      assertNoSymlinks(socket); const stat = lstatSync(socket);
      if (!stat.isSocket() || stat.uid !== this.uid || (stat.mode & 0o007)) throw new SafeError('unsafe_docker_socket');
      const args = ['--host', `unix://${socket}`];
      const info = await this.run(this.config.services.dockerCommand, [...args, 'info', '--format', '{{json .SecurityOptions}}']);
      if (!Array.isArray(JSON.parse(info)) || !JSON.parse(info).some((s: unknown) => typeof s === 'string' && s.includes('rootless'))) throw new SafeError('docker_not_rootless');
      const lines = await this.run(this.config.services.dockerCommand, [...args, 'ps', '--all', '--format', '{{json .ID}}\t{{json .Names}}\t{{json .State}}\t{{json .Ports}}']);
      for (const line of lines.split('\n').filter(Boolean).slice(0, 300)) {
        const [id, name, state, ports] = line.split('\t').map(v => JSON.parse(v));
        if (![id, name, state, ports].every(v => typeof v === 'string')) continue;
        const s = blank(stableId('docker', id), name, 'docker', state); s.endpoints = parseDockerPorts(ports); data.push(s);
      }
    } catch (e) { if ((e as NodeJS.ErrnoException).code !== 'ENOENT') warnings.push('docker_unavailable'); }
    for (const m of this.config.services.manual) { const s = blank(`manual_${m.id}`, m.name, 'manual', 'manual_unverified'); s.protocol = m.protocol; s.path = m.path; s.endpoints = [endpoint(m.address, m.port)]; data.push(s); }
    return { data: mergeServices([...data, ...listeners]), warnings: [...new Set(warnings)] };
  }
  async refresh(force = false): Promise<Snapshot<Service[]>> {
    if (!this.config.services.enabled) throw new SafeError('services_disabled', 503);
    if (!force && !this.read().stale) return this.read();
    if (this.inflight) return this.inflight;
    this.inflight = (async () => {
      try {
        const result = await this.collect();
        this.store.set('cache:services', { data: result.data, updatedAt: new Date().toISOString(), stale: !!result.warnings.length,
          error: result.warnings.length ? { code: result.warnings.join(','), message: 'Partial discovery; some sources unavailable' } : null });
      } catch (e) { this.store.set('cache:services', { ...this.read(), stale: true, error: safeError(e) }); }
      return this.read();
    })();
    try { return await this.inflight; } finally { this.inflight = undefined; }
  }
}
