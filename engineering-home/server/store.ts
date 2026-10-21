import { DatabaseSync } from 'node:sqlite';
import { readFileSync, chmodSync, existsSync, lstatSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { privateDirectory, assertNoSymlinks } from './security/files.js';
import { SafeError } from './security/sanitize.js';
import type { ApiError, Job, JobState, Report } from '../shared/contracts.js';

export class Store {
  readonly db: DatabaseSync;
  readonly file: string;
  constructor(directory: string) {
    privateDirectory(directory); this.file = join(directory, 'home.sqlite'); assertNoSymlinks(this.file);
    for (const file of [this.file, `${this.file}-wal`, `${this.file}-shm`]) {
      assertNoSymlinks(file);
      if (!existsSync(file)) continue;
      const s = lstatSync(file);
      if (!s.isFile() || s.uid !== process.getuid!() || (s.mode & 0o077)) throw new Error('Unsafe database');
    }
    const old = process.umask(0o077);
    try {
      this.db = new DatabaseSync(this.file);
      // One host process owns recovery and the paid-job consumer for this database.
      this.db.exec('PRAGMA locking_mode=EXCLUSIVE; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=1000;');
      this.db.exec(readFileSync(new URL('../migrations/001.sql', import.meta.url), 'utf8'));
      this.secure();
    } finally { process.umask(old); }
  }
  secure() { for (const f of [this.file, `${this.file}-wal`, `${this.file}-shm`]) if (existsSync(f)) chmodSync(f, 0o600); }
  close() { this.db.close(); }
  get<T>(key: string): T | null { const row = this.db.prepare('SELECT value FROM kv WHERE key=?').get(key); return row ? JSON.parse(row.value as string) : null; }
  set(key: string, value: unknown) { this.db.prepare('INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, JSON.stringify(value)); this.secure(); }
  list<T>(prefix: string): T[] { return this.db.prepare('SELECT value FROM kv WHERE substr(key,1,?)=? ORDER BY key DESC').all(prefix.length, prefix).map(r => JSON.parse(r.value as string)); }
  saveReport(report: Report) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.set(`report:${report.id}`, report);
      this.db.prepare('INSERT OR REPLACE INTO report_versions VALUES (?,?,?)').run(report.id, report.revision, JSON.stringify(report));
      this.db.exec('COMMIT');
    } catch (e) { this.db.exec('ROLLBACK'); throw e; }
  }
  enqueue(kind: Job['kind'], idem: string, input: unknown, reportId: string | null): Job {
    const key = `${kind}:${idem}`;
    const existing = this.db.prepare('SELECT * FROM jobs WHERE idem=?').get(key);
    if (existing) {
      if (existing.input !== JSON.stringify(input) || existing.report_id !== reportId) throw new SafeError('idempotency_conflict', 409);
      return this.dto(existing);
    }
    const id = randomUUID(), now = new Date().toISOString();
    this.db.prepare('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,NULL)').run(id, kind, 'queued', reportId, key, JSON.stringify(input), now, now);
    this.secure(); return this.job(id)!;
  }
  dto(row: Record<string, unknown>): Job {
    return { id: row.id as string, kind: row.kind as Job['kind'], state: row.state as JobState,
      reportId: row.report_id as string | null, createdAt: row.created_at as string, updatedAt: row.updated_at as string,
      error: row.error ? JSON.parse(row.error as string) : null };
  }
  job(id: string): Job | null { const row = this.db.prepare('SELECT * FROM jobs WHERE id=?').get(id); return row ? this.dto(row) : null; }
  jobs(): Job[] { return this.db.prepare('SELECT * FROM jobs ORDER BY created_at DESC, id DESC').all().map(row => this.dto(row)); }
  replaceJobInput(id: string, input: unknown) { this.db.prepare('UPDATE jobs SET input=? WHERE id=?').run(JSON.stringify(input), id); this.secure(); }
  jobInput<T>(id: string): T { return JSON.parse(this.db.prepare('SELECT input FROM jobs WHERE id=?').get(id)!.input as string); }
  updateJob(id: string, state: JobState, error: ApiError | null = null, reportId?: string) {
    this.db.prepare('UPDATE jobs SET state=?,error=?,updated_at=?,report_id=COALESCE(?,report_id) WHERE id=?').run(state, error ? JSON.stringify(error) : null, new Date().toISOString(), reportId ?? null, id); this.secure();
  }
  nextJob(): Job | null { const row = this.db.prepare("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1").get(); return row ? this.dto(row) : null; }
  hasGeneration(reportId: string): boolean { return !!this.db.prepare("SELECT 1 FROM jobs WHERE report_id=? AND kind='generate' AND state IN ('queued','preparing','ready','awaiting_resume','sending')").get(reportId); }
  uncertainGeneration(reportId: string): boolean { return !!this.db.prepare("SELECT 1 FROM jobs WHERE report_id=? AND kind='generate' AND state='outcome_unknown'").get(reportId); }
  recover() {
    this.db.prepare("UPDATE jobs SET state='outcome_unknown',error=?,updated_at=? WHERE state='sending'").run(JSON.stringify({ code: 'outcome_unknown', message: 'Request may have been charged; explicit retry required' }), new Date().toISOString());
    this.db.prepare("UPDATE jobs SET state='awaiting_resume',error=NULL,updated_at=? WHERE kind='generate' AND state IN ('queued','preparing','ready')").run(new Date().toISOString());
    this.db.prepare("UPDATE jobs SET state='queued',updated_at=? WHERE kind='collect' AND state IN ('preparing','ready')").run(new Date().toISOString());
    this.secure();
  }
}
