import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { SafeError } from '../security/sanitize.js';
export type RunOptions = { cwd?: string; timeoutMs?: number; maxBytes?: number; env?: Record<string, string>; signal?: AbortSignal; acceptedExitCodes?: number[] };
export type CommandRunner = (command: string, args: string[], options?: RunOptions) => Promise<string>;
export const runCommand: CommandRunner = (command, args, options = {}) => new Promise((resolve, reject) => {
  const child = spawn(command, args, { cwd: options.cwd ?? homedir(), shell: false, detached: true,
    env: { PATH: '/usr/local/bin:/usr/bin:/bin', HOME: homedir(), LANG: 'C.UTF-8', LC_ALL: 'C.UTF-8', ...options.env }, stdio: ['ignore', 'pipe', 'pipe'] });
  const chunks: Buffer[] = []; let bytes = 0; let failure: SafeError | null = null;
  const kill = () => { if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* The child group has already exited. */ } } };
  const stop = (code: string) => { failure ??= new SafeError(code, 502); kill(); };
  const timer = setTimeout(() => stop('collector_timeout'), options.timeoutMs ?? 5000);
  const aborted = () => stop('collector_cancelled'); options.signal?.addEventListener('abort', aborted, { once: true });
  if (options.signal?.aborted) aborted();
  const receive = (data: Buffer, stdout: boolean) => { bytes += data.length; if (bytes > (options.maxBytes ?? 1000000)) stop('collector_output_limit'); else if (stdout) chunks.push(data); };
  child.stdout.on('data', data => receive(data, true)); child.stderr.on('data', data => receive(data, false));
  const cleanup = () => { clearTimeout(timer); options.signal?.removeEventListener('abort', aborted); };
  child.on('error', () => { cleanup(); kill(); reject(new SafeError('collector_unavailable', 502)); });
  child.on('close', code => { cleanup(); kill(); if (failure) reject(failure); else if (code !== 0 && !options.acceptedExitCodes?.includes(code ?? -1)) reject(new SafeError('collector_failed', 502)); else resolve(Buffer.concat(chunks).toString('utf8')); });
});
