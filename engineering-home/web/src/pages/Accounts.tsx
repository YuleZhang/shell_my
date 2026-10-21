import { useState } from 'react';
import type { FormEvent } from 'react';
import { Pencil, Search } from 'lucide-react';
import type { Account, AccountResponse, AccountsResponse, BudgetBucket, Capabilities } from '../../../shared/contracts';
import { errorMessage, request } from '../client';
import { useResource } from '../hooks';
import { Badge, Check, dateTime, Empty, Loading, Notice, PageHeading, Panel, RefreshButton, SnapshotStatus } from '../presentation';

const amount = (value: number | null) => value === null ? '未提供' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 6 }).format(value);
export function Bucket({ bucket }: { bucket: BudgetBucket }) {
  const valid = bucket.total !== null && bucket.total > 0 && bucket.used !== null;
  return <div className="bucket"><div className="bucket-heading"><strong>{bucket.label}</strong><Badge>{bucket.source}</Badge></div><div className="bucket-values"><span>剩余<strong>{amount(bucket.remaining)}</strong></span><span>已用<strong>{amount(bucket.used)}</strong></span><span>总额<strong>{amount(bucket.total)}</strong></span></div>{valid && <progress aria-label={`${bucket.label} 已用比例`} value={Math.max(0, Math.min(bucket.used!, bucket.total!))} max={bucket.total!} />}<div className="bucket-meta"><span>币种：{bucket.currency ?? '未提供'}</span><span>周期：{bucket.period ?? '未提供'}</span><span>到期：{bucket.expiresAt ? dateTime(bucket.expiresAt) : '未提供'}</span></div><small className="mono">桶 ID：{bucket.id}</small></div>;
}
export function Accounts({ capabilities }: { capabilities: Capabilities }) {
  const resource = useResource<AccountsResponse>('/api/accounts');
  const [search, setSearch] = useState(''); const [tag, setTag] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState<string | null>(null); const [error, setError] = useState<string | null>(null);
  const accounts = resource.data?.accounts ?? [];
  const tags = [...new Set(['code-review', 'weekly-report', ...accounts.flatMap(a => a.tags)])];
  const filtered = accounts.filter(a => (!tag || a.tags.includes(tag)) && [a.id, a.originalName, a.alias, a.notes, ...a.tags].join(' ').toLowerCase().includes(search.toLowerCase()));
  async function refresh(id?: string) {
    if (refreshing) return; setRefreshing(id ?? 'all'); setError(null);
    try { const value = await request<AccountsResponse>('/api/accounts/refresh', { method: 'POST', body: { ...(id ? { id } : {}), force: true } }); resource.setData(value); }
    catch (e) { setError(errorMessage(e)); } finally { setRefreshing(null); }
  }
  function replace(account: Account) { resource.setData(value => value ? { accounts: value.accounts.map(a => a.id === account.id ? account : a) } : value); setEditing(null); }
  return <><PageHeading eyebrow="ACCOUNTS & USAGE" title="API 账户" description="账户标记与原始用量桶。凭据不进入浏览器，余额不跨池相加。" actions={<RefreshButton busy={refreshing === 'all'} disabled={!capabilities.accounts || !!refreshing} label="刷新全部余额" onClick={() => void refresh()} />} />
    <Notice>凭据来源由服务器本机配置；新增或替换 Key 请在仓库外完成本机导入。此页面不查看、不请求、不复制明文 Key。{!capabilities.credentialImport && ' 网页凭据导入未开放。'}</Notice>
    {!capabilities.accounts && <Notice tone="warn">账户集成未配置。可查看已有元数据，无法获取新的余额。</Notice>}
    <div className="toolbar"><label className="search-field"><Search size={17} aria-hidden="true" /><input type="search" aria-label="搜索账户" value={search} onChange={e => setSearch(e.target.value)} placeholder="别名、原账号名、标签或备注" /></label><label className="field inline-field">标签<select value={tag} onChange={e => setTag(e.target.value)}><option value="">全部标签</option>{tags.map(t => <option key={t}>{t}</option>)}</select></label><span className="muted">{filtered.length} 个账户</span></div>
    {(error || resource.error) && <Notice tone="error">{error ?? resource.error}</Notice>}{resource.loading && <Loading />}
    {!resource.loading && !filtered.length && <Empty title="没有匹配的账户">检查筛选，或在仓库外配置本机 Key 来源后刷新。</Empty>}
    <div className="account-grid">{filtered.map(account => <Panel key={account.id} title={account.alias || account.originalName} aside={<Badge tone={!account.enabled ? 'muted' : account.available ? 'good' : 'bad'}>{!account.enabled ? '已停用' : account.available ? '凭据可用' : '凭据不可用'}</Badge>}>
      <div className="account-identity"><span>原账号名：<strong>{account.originalName}</strong></span><small className="mono">稳定 ID：{account.id}</small></div><div className="tag-list">{account.tags.map(t => <Badge key={t}>{t}</Badge>)}{!account.tags.length && <span className="muted">未标记用途</span>}</div>
      <SnapshotStatus snapshot={account.usage} />{account.usage.data.map(bucket => <Bucket key={`${bucket.source}:${bucket.id}`} bucket={bucket} />)}{!account.usage.data.length && <p className="helper">尚无用量数据；不把未知余额显示为 0。</p>}
      {account.notes && <p className="account-notes">{account.notes}</p>}
      {editing === account.id ? <AccountEditor account={account} onSave={replace} onCancel={() => setEditing(null)} /> : <div className="card-actions"><button className="button button-secondary" onClick={() => setEditing(account.id)}><Pencil size={14} aria-hidden="true" />编辑标记</button><RefreshButton busy={refreshing === account.id} disabled={!capabilities.accounts || !account.available || !!refreshing} label="刷新余额" onClick={() => void refresh(account.id)} /></div>}
    </Panel>)}</div><p className="helper">code-review / weekly-report 等标签仅为用法标记，不强制路由或权限，不代表实际消费归因。usage 与 budgetPool 可能重复，逐桶展示，不合计。</p></>;
}
function AccountEditor({ account, onSave, onCancel }: { account: Account; onSave: (a: Account) => void; onCancel: () => void }) {
  const [alias, setAlias] = useState(account.alias); const [tags, setTags] = useState(account.tags.join(', ')); const [notes, setNotes] = useState(account.notes); const [enabled, setEnabled] = useState(account.enabled);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  async function save(e: FormEvent) {
    e.preventDefault(); if (busy) return;
    const parsedTags = [...new Set(tags.split(/[,，\n]/).map(t => t.trim()).filter(Boolean))];
    if (parsedTags.length > 20 || parsedTags.some(t => t.length > 40)) { setError('标签最多 20 个，每个不超过 40 字符。'); return; }
    setBusy(true); setError(null);
    try { const value = await request<AccountResponse>(`/api/accounts/${encodeURIComponent(account.id)}`, { method: 'PATCH', body: { alias, tags: parsedTags, notes, enabled } }); onSave(value.account); }
    catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <form className="editor" onSubmit={e => void save(e)}>{error && <Notice tone="error">{error}</Notice>}<label className="field">显示别名<input value={alias} onChange={e => setAlias(e.target.value)} maxLength={120} /></label><label className="field">用途标签（逗号分隔）<input value={tags} onChange={e => setTags(e.target.value)} placeholder="code-review, weekly-report" /></label><label className="field">备注<textarea value={notes} onChange={e => setNotes(e.target.value)} maxLength={4000} rows={3} /></label><Check label="启用此账户" checked={enabled} onChange={setEnabled} /><p className="helper">别名不会更改原 CLI 账号名。标签不会强制路由。</p><div className="button-row"><button className="button button-primary" disabled={busy}>{busy ? '保存中…' : '保存标记'}</button><button type="button" className="button button-secondary" disabled={busy} onClick={onCancel}>取消</button></div></form>;
}
