import { constants, closeSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, chmodSync } from 'node:fs';
import { dirname, resolve, parse } from 'node:path';

export function assertNoSymlinks(path: string): void {
  const full = resolve(path); let current = parse(full).root;
  for (const part of full.slice(current.length).split('/').filter(Boolean)) {
    current = resolve(current, part);
    try { if (lstatSync(current).isSymbolicLink()) throw new Error('Symlink forbidden'); }
    catch (e) { if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e; }
  }
}
export function privateDirectory(path: string): void {
  assertNoSymlinks(path); mkdirSync(path, { recursive: true, mode: 0o700 });
  const s = lstatSync(path);
  if (!s.isDirectory() || s.uid !== process.getuid!()) throw new Error('Unsafe directory ownership');
  chmodSync(path, 0o700);
}
export function readPrivateFile(path: string, maxBytes = 1024 * 1024): string {
  assertNoSymlinks(path);
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const s = fstatSync(fd);
    if (!s.isFile() || s.uid !== process.getuid!() || (s.mode & 0o077) !== 0 || s.size > maxBytes) throw new Error('Unsafe private file');
    const p = lstatSync(dirname(path));
    if (p.uid !== process.getuid!() || (p.mode & 0o022) !== 0) throw new Error('Unsafe parent directory');
    return readFileSync(fd, 'utf8');
  } finally { closeSync(fd); }
}
