import { useState } from 'react';
import type { FormEvent } from 'react';
import { Pencil, Search, Star } from 'lucide-react';
import type { Endpoint, Service, ServiceResponse, Snapshot } from '../../../shared/contracts';
import { errorMessage, request } from '../client';
import { useResource } from '../hooks';
import { Badge, Check, dateTime, Empty, ExternalLink, Loading, Notice, PageHeading, Panel, RefreshButton, SnapshotStatus } from '../presentation';

export function serviceTone(state: string): 'good' | 'bad' | 'muted' {
  const primary = state.split('/')[0];
  return ['active', 'running', 'listening', 'tcp_listening'].includes(primary) ? 'good' : primary === 'failed' ? 'bad' : 'muted';
}
export function endpointAddress(endpoint: Endpoint) { const address = endpoint.address.replace(/^\[|\]$/g, ''); return `${address.includes(':') ? `[${address}]` : address}:${endpoint.port}`; }
export function EndpointView({ endpoint }: { endpoint: Endpoint }) {
  return <div className="endpoint"><div><span className="mono">{endpointAddress(endpoint)}</span><Badge>{endpoint.transport.toUpperCase()}</Badge></div>{endpoint.url && <ExternalLink url={endpoint.url} service>打开配置入口</ExternalLink>}<small>{endpoint.localOnly ? '仅本机可用 · 远程需 SSH 隧道' : '远程可达性未验证'} · 监听不代表 HTTP 健康</small></div>;
}
export function Services({ enabled }: { enabled: boolean }) {
  const resource = useResource<Snapshot<Service[]>>('/api/services');
  const [search, setSearch] = useState(''); const [category, setCategory] = useState(''); const [favorites, setFavorites] = useState(false);
  const [editing, setEditing] = useState<string | null>(null); const [saving, setSaving] = useState<string | null>(null); const [error, setError] = useState<string | null>(null);
  const services = resource.data?.data ?? [];
  const categories = [...new Set(services.map(s => s.category).filter(Boolean))].sort();
  const filtered = services.filter(s => (!favorites || s.favorite) && (!category || s.category === category) && [s.alias, s.name, s.notes, s.category, ...s.sources, ...s.endpoints.map(endpointAddress)].join(' ').toLowerCase().includes(search.toLowerCase()));
  function replace(service: Service) { resource.setData(value => value ? { ...value, data: value.data.map(s => s.id === service.id ? service : s) } : value); }
  async function favorite(service: Service) {
    if (saving) return; setSaving(service.id); setError(null);
    try { const value = await request<ServiceResponse>(`/api/services/${encodeURIComponent(service.id)}`, { method: 'PATCH', body: { favorite: !service.favorite } }); replace(value.service); }
    catch (e) { setError(errorMessage(e)); } finally { setSaving(null); }
  }
  return <><PageHeading eyebrow="LOCAL SERVICE DIRECTORY" title="服务目录" description="当前 UID 的 systemd、监听进程与 rootless Docker。发现与访问是两回事。" actions={<RefreshButton busy={resource.loading} disabled={!enabled} label="重新发现服务" onClick={() => void resource.load({}, '/api/services/refresh')} />} />
    <Notice>只读目录，不提供启动、停止、重启、日志或代理。协议由你配置，链接仅来自后端 endpoint.url，不按端口猜测网页。</Notice>{!enabled && <Notice tone="warn">服务发现未启用。可查看已有缓存与说明。</Notice>}
    <div className="toolbar"><label className="search-field"><Search size={17} aria-hidden="true" /><input type="search" aria-label="搜索服务" placeholder="名称、说明、来源或 IP:port" value={search} onChange={e => setSearch(e.target.value)} /></label><label className="field inline-field">分类<select value={category} onChange={e => setCategory(e.target.value)}><option value="">全部分类</option>{categories.map(c => <option key={c}>{c}</option>)}</select></label><Check label="仅收藏" checked={favorites} onChange={setFavorites} /></div>
    {(error || resource.error) && <Notice tone="error">{error ?? resource.error}</Notice>}{resource.data && <SnapshotStatus snapshot={resource.data} />}{resource.loading && <Loading />}
    {!resource.loading && !filtered.length && <Empty title="没有匹配的服务">调整筛选或显式发现服务。未启用、无网络端点的条目也会保留。</Empty>}
    <div className="service-grid">{filtered.map(service => <Panel key={service.id} title={service.alias || service.name} aside={<button className={`icon-button favorite ${service.favorite ? 'is-favorite' : ''}`} aria-label={`${service.favorite ? '取消收藏' : '收藏'} ${service.alias || service.name}`} aria-pressed={service.favorite} disabled={!!saving} onClick={() => void favorite(service)}><Star size={18} fill={service.favorite ? 'currentColor' : 'none'} /></button>}>
      <div className="service-summary"><span className="muted">{service.name}</span><Badge tone={serviceTone(service.state)}>{service.state}</Badge></div><div className="tag-list">{service.sources.map(source => <Badge key={source}>{source}</Badge>)}<Badge>{service.category || '未分类'}</Badge><Badge>{service.protocol === 'none' ? '未配置协议' : service.protocol.toUpperCase()}</Badge></div>
      {service.endpoints.map(endpoint => <EndpointView key={`${endpoint.address}:${endpoint.port}`} endpoint={endpoint} />)}{!service.endpoints.length && <div className="endpoint"><strong>后台服务，无网络端点</strong><small>保留 inactive / failed / 无端口条目。</small></div>}
      {service.notes && <p className="account-notes">{service.notes}</p>}<p className="helper">最后发现 {dateTime(service.discoveredAt)}</p>
      {editing === service.id ? <ServiceEditor service={service} onSave={s => { replace(s); setEditing(null); }} onCancel={() => setEditing(null)} /> : <button className="button button-secondary" onClick={() => setEditing(service.id)}><Pencil size={14} aria-hidden="true" />编辑说明与入口</button>}
    </Panel>)}</div></>;
}
function ServiceEditor({ service, onSave, onCancel }: { service: Service; onSave: (s: Service) => void; onCancel: () => void }) {
  const [fields, setFields] = useState({ alias: service.alias, notes: service.notes, category: service.category, favorite: service.favorite, protocol: service.protocol, path: service.path || '/' });
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  async function save(e: FormEvent) {
    e.preventDefault(); if (busy) return; setBusy(true); setError(null);
    try { const value = await request<ServiceResponse>(`/api/services/${encodeURIComponent(service.id)}`, { method: 'PATCH', body: fields }); onSave(value.service); }
    catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  }
  return <form className="editor" onSubmit={e => void save(e)}>{error && <Notice tone="error">{error}</Notice>}<label className="field">显示别名<input maxLength={120} value={fields.alias} onChange={e => setFields({ ...fields, alias: e.target.value })} /></label><label className="field">分类<input maxLength={80} value={fields.category} onChange={e => setFields({ ...fields, category: e.target.value })} placeholder="开发 / API / 数据库 / 后台" /></label><div className="form-grid"><label className="field">协议<select value={fields.protocol} onChange={e => setFields({ ...fields, protocol: e.target.value as Service['protocol'] })}>{['none', 'http', 'https', 'ssh', 'tcp'].map(p => <option key={p} value={p}>{p === 'none' ? '不提供链接' : p.toUpperCase()}</option>)}</select></label><label className="field">路径<input maxLength={500} value={fields.path} onChange={e => setFields({ ...fields, path: e.target.value })} pattern="/(?!/)[^\s\\?#]*" required /></label></div><label className="field">说明<textarea maxLength={4000} rows={3} value={fields.notes} onChange={e => setFields({ ...fields, notes: e.target.value })} /></label><Check label="收藏此服务" checked={fields.favorite} onChange={favorite => setFields({ ...fields, favorite })} /><p className="helper">路径从 / 开始，不含查询、片段或反斜杠。保存只修改元数据，不能证明入口可达。</p><div className="button-row"><button className="button button-primary" disabled={busy}>{busy ? '保存中…' : '保存说明'}</button><button type="button" className="button button-secondary" disabled={busy} onClick={onCancel}>取消</button></div></form>;
}
