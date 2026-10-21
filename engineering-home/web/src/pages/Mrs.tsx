import { useState } from 'react';
import type { FormEvent } from 'react';
import type { MrFilter, MrPage, Snapshot } from '../../../shared/contracts';
import { useResource } from '../hooks';
import { Badge, dateTime, Empty, ExternalLink, Loading, Notice, PageHeading, Panel, RefreshButton, SnapshotStatus } from '../presentation';

const initial: MrFilter = { state: 'opened', page: 1, perPage: 20 };
export function Mrs({ enabled }: { enabled: boolean }) {
  const [filter, setFilter] = useState<MrFilter>(initial);
  const [draft, setDraft] = useState({ state: 'opened', search: '', projectId: '', updatedAfter: '', updatedBefore: '' });
  const query = new URLSearchParams(Object.entries(filter).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]));
  const resource = useResource<Snapshot<MrPage>>(`/api/mrs?${query}`);
  function apply(e: FormEvent) {
    e.preventDefault();
    setFilter({ state: draft.state as MrFilter['state'], page: 1, perPage: filter.perPage,
      ...(draft.search.trim() ? { search: draft.search.trim() } : {}), ...(draft.projectId ? { projectId: Number(draft.projectId) } : {}),
      ...(draft.updatedAfter ? { updatedAfter: draft.updatedAfter } : {}), ...(draft.updatedBefore ? { updatedBefore: draft.updatedBefore } : {}) });
  }
  const page = resource.data?.data;
  return <><PageHeading eyebrow="CODE & REVIEW" title="我的 MR" description="本人创建的合并请求。只读查看，不合并、不审批、不推送。" actions={<RefreshButton busy={resource.loading} disabled={!enabled} label="从 GitLab 刷新" onClick={() => void resource.load(filter, '/api/mrs/refresh')} />} />
    {!enabled && <Notice tone="warn">GitLab 集成未配置。可读取已有缓存，但不能查询新的远端数据；未查到不代表没有 MR。</Notice>}
    <Panel title="筛选合并请求" aside={<Badge>按本人 author 查询</Badge>}><form className="filter-grid" onSubmit={apply}>
      <label className="field">状态<select value={draft.state} onChange={e => setDraft({ ...draft, state: e.target.value })}><option value="opened">待合并</option><option value="merged">已合并</option><option value="closed">已关闭</option><option value="all">全部</option></select></label>
      <label className="field field-wide">标题搜索<input type="search" maxLength={200} value={draft.search} placeholder="搜索合并请求" onChange={e => setDraft({ ...draft, search: e.target.value })} /></label>
      <label className="field">项目 ID<input type="number" min="1" step="1" placeholder="全部项目" value={draft.projectId} onChange={e => setDraft({ ...draft, projectId: e.target.value })} /></label>
      <label className="field">更新起始<input type="date" value={draft.updatedAfter} max={draft.updatedBefore || undefined} onChange={e => setDraft({ ...draft, updatedAfter: e.target.value })} /></label>
      <label className="field">更新截止<input type="date" value={draft.updatedBefore} min={draft.updatedAfter || undefined} onChange={e => setDraft({ ...draft, updatedBefore: e.target.value })} /></label>
      <button className="button button-secondary" disabled={resource.loading}>查看缓存</button>
    </form><p className="helper">筛选仅查询服务器快照；更换条件后请按需点击「从 GitLab 刷新」。</p></Panel>
    {resource.error && <Notice tone="error">{resource.error}</Notice>}
    <Panel title="合并请求" aside={page && <span className="muted">本页 {page.items.length} 条 · 第 {page.page} 页</span>}>
      {resource.data && <SnapshotStatus snapshot={resource.data} />}{resource.loading && <Loading />}
      {!resource.loading && !page?.items.length && <Empty title={resource.data?.stale ? '此条件尚无可用快照' : '此页没有合并请求'}>可调整状态、日期和搜索范围，或显式刷新 GitLab。</Empty>}
      {page && page.items.length > 0 && <div className="table-scroll"><table><thead><tr><th>合并请求 / 项目</th><th>状态</th><th>分支</th><th>Review / Pipeline</th><th>更新于</th></tr></thead><tbody>{page.items.map(mr => <tr key={mr.id}><td><ExternalLink url={mr.url}>{mr.title}</ExternalLink><small>{mr.project} · !{mr.iid} · 项目 {mr.projectId}</small></td><td><Badge tone={mr.state === 'merged' ? 'good' : mr.state === 'opened' ? 'warn' : 'muted'}>{({ opened: '待合并', merged: '已合并', closed: '已关闭' } as Record<string, string>)[mr.state] ?? mr.state}</Badge></td><td><span className="mono">{mr.sourceBranch}</span><small>→ {mr.targetBranch}</small></td><td>{mr.reviewers.length ? mr.reviewers.join('、') : '无 reviewer'}<small>Pipeline: {mr.pipelineStatus ?? '未提供'}</small></td><td>{dateTime(mr.updatedAt)}</td></tr>)}</tbody></table></div>}
      <div className="pagination"><label className="field inline-field">每页<select value={filter.perPage} onChange={e => setFilter({ ...filter, perPage: Number(e.target.value), page: 1 })}><option>20</option><option>50</option><option>100</option></select></label><div className="button-row"><button className="button button-secondary" disabled={filter.page <= 1 || resource.loading} onClick={() => setFilter({ ...filter, page: filter.page - 1 })}>上一页</button><button className="button button-secondary" disabled={!page?.hasMore || resource.loading} onClick={() => setFilter({ ...filter, page: page?.nextPage ?? filter.page + 1 })}>下一页</button></div></div>
      <p className="helper">身份 ID：{page?.authorId ?? '未绑定或尚未缓存'} · 不把本地提交推断为全部 MR。</p>
    </Panel></>;
}
