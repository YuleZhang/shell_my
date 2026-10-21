import type { ReactNode } from 'react';
import { AlertCircle, ArrowUpRight, Inbox, LoaderCircle, RefreshCw } from 'lucide-react';
import type { Snapshot } from '../../shared/contracts';
import { safeExternalUrl } from './client';

export function dateTime(value: string | null) {
  if (!value) return '尚无缓存';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '未知时间' : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
export function Badge({ children, tone = 'muted' }: { children: ReactNode; tone?: 'muted' | 'good' | 'warn' | 'bad' }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}
export function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' | 'error' }) {
  return <div className={`notice notice-${tone}`} role={tone === 'error' ? 'alert' : 'status'}><AlertCircle size={16} aria-hidden="true" /><div>{children}</div></div>;
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><Inbox size={28} aria-hidden="true" /><strong>{title}</strong>{children && <p>{children}</p>}</div>;
}
export function Loading({ label = '正在读取本地快照…' }: { label?: string }) {
  return <div className="loading" role="status"><LoaderCircle className="spin" size={18} aria-hidden="true" />{label}</div>;
}
export function RefreshButton({ busy, disabled, onClick, label = '刷新数据' }: { busy: boolean; disabled?: boolean; onClick: () => void; label?: string }) {
  return <button className="button button-secondary" disabled={busy || disabled} onClick={onClick}><RefreshCw size={15} className={busy ? 'spin' : ''} aria-hidden="true" />{busy ? '刷新中…' : label}</button>;
}
export function SnapshotStatus({ snapshot }: { snapshot: Pick<Snapshot<unknown>, 'updatedAt' | 'stale' | 'error'> }) {
  return <div className="snapshot-status"><Badge tone={snapshot.stale ? 'warn' : 'good'}>{snapshot.stale ? '缓存待刷新' : '已缓存'}</Badge><span>更新于 {dateTime(snapshot.updatedAt)}</span>{snapshot.error && <Notice tone="error">{snapshot.error.message}（{snapshot.error.code}） · 保留已有快照</Notice>}</div>;
}
export function Panel({ title, aside, children, className = '' }: { title: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}><div className="panel-header"><h2>{title}</h2>{aside}</div>{children}</section>;
}
export function PageHeading({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{actions && <div className="heading-actions">{actions}</div>}</div>;
}
export function ExternalLink({ url, children, service = false }: { url: string | null; children: ReactNode; service?: boolean }) {
  const href = safeExternalUrl(url, service ? ['http:', 'https:', 'ssh:', 'tcp:'] : undefined);
  return href ? <a className="external-link" href={href} target="_blank" rel="noopener noreferrer">{children}<ArrowUpRight size={14} aria-hidden="true" /><span className="sr-only">（新窗口）</span></a> : <span>{children}</span>;
}
export function Check({ label, checked, onChange, disabled = false }: { label: ReactNode; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean }) {
  return <label className="check"><input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} disabled={disabled} /><span>{label}</span></label>;
}
