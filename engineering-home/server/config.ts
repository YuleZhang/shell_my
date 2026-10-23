import { z } from 'zod';
import { homedir } from 'node:os';
import { resolve, join, isAbsolute } from 'node:path';
import { readPrivateFile, privateDirectory } from './security/files.js';
import { isIP } from 'node:net';

const endpoint = z.string().url().refine(value => {
  const u = new URL(value);
  return !u.username && !u.password && !u.search && !u.hash &&
    (u.protocol === 'https:' || (u.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(u.hostname)));
}, 'HTTPS or loopback endpoint required');
const absolute = z.string().refine(isAbsolute, 'Absolute path required');
export const configSchema = z.object({
  publicOrigin: endpoint.refine(v => new URL(v).origin === v, 'Exact origin without trailing slash required'),
  listenHost: z.enum(['127.0.0.1', '::1', '0.0.0.0']).default('127.0.0.1'), port: z.number().int().min(1024).max(65535).default(4310),
  auth: z.object({ passwordHash: z.string().regex(/^scrypt\$[a-f0-9]{32}\$[a-f0-9]{128}$/),
    cookieSecret: z.string().min(32), secureCookie: z.boolean().default(true), sessionHours: z.number().min(0.1).max(168).default(12) }).strict(),
  stepcode: z.object({ baseUrl: endpoint, concurrency: z.number().int().min(1).max(8).default(4),
    timeoutMs: z.number().min(100).max(10000).default(4000), batchTimeoutMs: z.number().min(100).max(60000).default(20000), ttlMs: z.number().min(0).max(3600000).default(60000) }).strict().optional(),
  gitlab: z.object({ baseUrl: endpoint, token: z.string().min(1), authorId: z.number().int().positive().optional(),
    botBindingExplicit: z.boolean().default(false), perPageLimit: z.number().int().min(1).max(100).default(100),
    timeoutMs: z.number().min(100).max(30000).default(5000), ttlMs: z.number().min(0).max(3600000).default(60000),
    headers: z.record(z.string().regex(/^(?:X-[A-Za-z0-9-]+|User-Agent)$/), z.string().max(1000)).default({}) }).strict().optional(),
  services: z.object({ enabled: z.boolean().default(true), ttlMs: z.number().min(0).max(3600000).default(15000),
    dockerCommand: absolute.default('/usr/bin/docker'),
    accessAddress: z.string().refine(v => isIP(v) !== 0 && !['0.0.0.0', '::'].includes(v), 'Concrete IP address required').optional(),
    manual: z.array(z.object({ id: z.string().regex(/^[\w-]+$/), name: z.string().max(120),
      address: z.string().refine(v => isIP(v) !== 0, 'IP address required'), port: z.number().int().min(1).max(65535),
      protocol: z.enum(['http', 'https', 'ssh', 'tcp', 'none']).default('none'), path: z.string().regex(/^\/(?!\/)[^\s\\?#]*$/).default('/') }).strict()).default([]) }).strict().default({ enabled: true, ttlMs: 15000, manual: [], dockerCommand: '/usr/bin/docker' }),
  projectRoots: z.array(z.object({ id: z.string().regex(/^[\w-]+$/), label: z.string().max(120), root: absolute }).strict()).max(30).default([]),
  reports: z.object({ skillPath: absolute, python: absolute, timeoutMs: z.number().min(100).max(300000).default(120000),
    maxBytes: z.number().min(1024).max(10000000).default(4000000),
    models: z.array(z.object({ id: z.string().regex(/^[\w-]+$/), label: z.string().max(120), endpoint,
      model: z.string().max(200), apiKey: z.string().min(1).optional(), useStepcodeAccount: z.boolean().default(false),
      maxOutputTokens: z.number().int().min(100).max(16000).default(4000), maxInputChars: z.number().int().min(1000).max(200000).default(60000),
      timeoutMs: z.number().min(100).max(180000).default(60000) }).strict()).max(10).default([]) }).strict().optional(),
}).strict();
export type Config = z.infer<typeof configSchema>;
export const CONFIG_DIR = join(homedir(), '.config/engineering-home');
export const DATA_DIR = join(homedir(), '.local/share/engineering-home');
export const KEY_FILE = join(homedir(), '.stepcode/local-key.txt');
export function validateConfig(input: unknown): Config {
  const c = configSchema.parse(input);
  if (!c.auth.secureCookie && !['localhost', '127.0.0.1', '[::1]'].includes(new URL(c.publicOrigin).hostname)) throw new Error('Secure cookie required');
  if (new Set(c.projectRoots.map(p => p.id)).size !== c.projectRoots.length) throw new Error('Duplicate project IDs');
  if (new Set(c.reports?.models.map(m => m.id)).size !== (c.reports?.models.length ?? 0)) throw new Error('Duplicate model IDs');
  if (new Set(c.services.manual.map(m => m.id)).size !== c.services.manual.length) throw new Error('Duplicate manual service IDs');
  return c;
}
export function loadConfig(): Config {
  privateDirectory(CONFIG_DIR);
  const raw = process.env.EH_CONFIG_FILE;
  if (!raw) throw new Error('EH_CONFIG_FILE required');
  const file = resolve(raw.replace(/^~\//, `${homedir()}/`));
  if (!file.startsWith(`${CONFIG_DIR}/`)) throw new Error('Configuration must be in private config directory');
  return validateConfig(JSON.parse(readPrivateFile(file)));
}
