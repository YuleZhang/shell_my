import { randomBytes, scryptSync, timingSafeEqual, createHash } from 'node:crypto';
import type { FastifyInstance, FastifyRequest } from 'fastify';
import cookie from '@fastify/cookie';
import type { Config } from '../config.js';
import { SafeError } from './sanitize.js';
import { loginSchema } from '../../shared/contracts.js';

export function hashPassword(password: string, salt = randomBytes(16).toString('hex')) {
  return `scrypt$${salt}$${scryptSync(password, salt, 64).toString('hex')}`;
}
export function verifyPassword(password: string, hash: string): boolean {
  const [, salt, value] = hash.split('$');
  try { return timingSafeEqual(scryptSync(password, salt, 64), Buffer.from(value, 'hex')); } catch { return false; }
}
export type AuthSession = { csrfToken: string; expires: number };
const digest = (token: string) => createHash('sha256').update(token).digest('hex');
export async function installAuth(app: FastifyInstance, config: Config) {
  const sessions = new Map<string, AuthSession>();
  const attempts = new Map<string, { count: number; start: number }>();
  await app.register(cookie, { secret: config.auth.cookieSecret });
  const expected = new URL(config.publicOrigin);
  function session(req: FastifyRequest): AuthSession | null {
    const raw = req.cookies.eh_session;
    if (!raw) return null;
    const unsigned = req.unsignCookie(raw);
    const s = unsigned.valid && unsigned.value ? sessions.get(digest(unsigned.value)) : undefined;
    return s && s.expires > Date.now() ? s : null;
  }
  app.addHook('onRequest', async (req) => {
    if (req.headers.host !== expected.host) throw new SafeError('invalid_host', 403);
    const origin = req.headers.origin;
    if (origin !== undefined && origin !== config.publicOrigin) throw new SafeError('invalid_origin', 403);
    if (req.headers['sec-fetch-site'] === 'cross-site') throw new SafeError('cross_site_forbidden', 403);
    const write = !['GET', 'HEAD', 'OPTIONS'].includes(req.method);
    if (write && origin !== config.publicOrigin) throw new SafeError('origin_required', 403);
    const pathname = req.url.split('?')[0];
    let decoded: string;
    try { decoded = decodeURIComponent(pathname); } catch { throw new SafeError('invalid_path', 400); }
    if (decoded !== pathname || pathname.includes('//') || pathname.includes('\\')) throw new SafeError('invalid_path', 400);
    const login = pathname === '/api/auth/login';
    if ((pathname === '/api' || pathname.startsWith('/api/')) && !login) {
      const s = session(req);
      if (!s) throw new SafeError('authentication_required', 401);
      if (write) {
        const token = req.headers['x-csrf-token'];
        if (typeof token !== 'string' || !/^[a-f0-9]{64}$/.test(token) || !timingSafeEqual(Buffer.from(token), Buffer.from(s.csrfToken))) throw new SafeError('invalid_csrf', 403);
      }
    }
  });
  const cookieOptions = { path: '/', httpOnly: true, sameSite: 'strict' as const, secure: config.auth.secureCookie, signed: true };
  app.post('/api/auth/login', async (req, reply) => {
    const now = Date.now();
    for (const [k, v] of attempts) if (now - v.start > 600000) attempts.delete(k);
    const rate = attempts.get(req.ip) ?? { count: 0, start: now };
    if (rate.count >= 10) throw new SafeError('login_rate_limited', 429);
    rate.count++; attempts.set(req.ip, rate);
    const input = loginSchema.parse(req.body);
    if (!verifyPassword(input.password, config.auth.passwordHash)) throw new SafeError('invalid_credentials', 401);
    attempts.delete(req.ip);
    for (const [k, s] of sessions) if (s.expires <= now) sessions.delete(k);
    if (sessions.size >= 100) sessions.delete(sessions.keys().next().value!);
    const token = randomBytes(32).toString('base64url');
    const s = { csrfToken: randomBytes(32).toString('hex'), expires: now + config.auth.sessionHours * 3600000 };
    sessions.set(digest(token), s);
    reply.setCookie('eh_session', token, { ...cookieOptions, maxAge: config.auth.sessionHours * 3600 });
    return { authenticated: true, csrfToken: s.csrfToken };
  });
  app.post('/api/auth/logout', async (req, reply) => {
    const raw = req.cookies.eh_session;
    if (raw) { const u = req.unsignCookie(raw); if (u.value) sessions.delete(digest(u.value)); }
    reply.clearCookie('eh_session', cookieOptions); return { ok: true };
  });
  return { session };
}
