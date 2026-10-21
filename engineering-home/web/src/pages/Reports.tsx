import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { Capabilities, CollectInput, JobResponse, JobsResponse, ReportsResponse } from '../../../shared/contracts';
import { errorMessage, request } from '../client';
import { useJob, useResource } from '../hooks';
import { Badge, Check, dateTime, Empty, Loading, Notice, PageHeading, Panel, RefreshButton } from '../presentation';
import { ReportDetail, JobStatus } from './ReportDetail';
import { ReportJobs } from './ReportJobs';
import { isActiveJob } from '../jobs';

function monday() { const date = new Date(); date.setDate(date.getDate() - (date.getDay() + 6) % 7); return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`; }
export function Reports({ capabilities }: { capabilities: Capabilities }) {
  const history = useResource<ReportsResponse>('/api/reports');
  const jobHistory = useResource<JobsResponse>('/api/jobs');
  const job = useJob();
  const [week, setWeek] = useState(monday); const [projectIds, setProjectIds] = useState<string[]>([]);
  const [fragments, setFragments] = useState(''); const [previousTodo, setPreviousTodo] = useState('');
  const [consentScan, setConsentScan] = useState(false); const [includeContext, setIncludeContext] = useState(false);
  const [selected, setSelected] = useState<string | null>(null); const [dirty, setDirty] = useState(false); const [detailBusy, setDetailBusy] = useState(false);
  const [pendingReport, setPendingReport] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); const lock = useRef(false); const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState(''); const completed = useRef<string | null>(null);
  const idem = useRef<{ params: string; key: string } | null>(null);
  const params = JSON.stringify({ week, projectIds, fragments, previousTodo, consentScan, includeContext });
  useEffect(() => { idem.current = null; }, [params]);
  const jobs = [...(jobHistory.data?.jobs.filter(value => value.id !== job.response?.job.id) ?? []), ...(job.response ? [job.response.job] : [])];
  const hasUnknown = jobs.some(value => value.reportId === selected && value.kind === 'generate' && value.state === 'outcome_unknown');
  const hasPending = jobHistory.loading || !!jobHistory.error || jobs.some(value => value.reportId === selected && value.kind === 'generate' && (isActiveJob(value.state) || value.state === 'awaiting_resume'));
  const disabled = busy || detailBusy || (job.busy && job.response?.job.kind === 'collect');
  function select(id: string) { if (id === selected) return; if (dirty && !window.confirm('当前周报有未保存编辑。确定离开并放弃这些本地编辑吗？')) return; setDirty(false); setSelected(id); }
  useEffect(() => {
    const value = job.response?.job;
    if (!value || !['succeeded', 'failed', 'outcome_unknown', 'awaiting_resume'].includes(value.state) || completed.current === `${value.id}:${value.state}`) return;
    completed.current = `${value.id}:${value.state}`; void history.load(); void jobHistory.load();
    if (value.kind !== 'collect' || value.state !== 'succeeded' || !value.reportId || job.paused) return;
    if (dirty) setPendingReport(value.reportId);
    else { setSelected(value.reportId); setDirty(false); }
  }, [job.response?.job, history.load, jobHistory.load, dirty, job.paused]);
  useEffect(() => {
    const guard = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest('a[href^="#"]') : null;
      const href = anchor?.getAttribute('href');
      if (!href || href === '#reports' || href === '#main-content' || (!dirty && !busy && !job.busy && !detailBusy)) return;
      const warning = dirty ? '有未保存的周报编辑，离页后会丢失。确定离开吗？' : '任务可能仍在服务器执行；离页仅停止状态查询，不取消任务或退款。确定离开吗？';
      if (!window.confirm(warning)) { event.preventDefault(); event.stopPropagation(); }
    };
    document.addEventListener('click', guard, true);
    return () => document.removeEventListener('click', guard, true);
  }, [dirty, busy, job.busy, detailBusy]);
  useEffect(() => { const warn = (e: BeforeUnloadEvent) => { if (dirty || busy || job.busy || detailBusy) e.preventDefault(); }; window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn); }, [dirty, busy, job.busy, detailBusy]);
  async function collect(e: FormEvent) {
    e.preventDefault(); if (disabled || lock.current) return;
    if (dirty && !window.confirm('采集成功后会打开新周报。放弃当前未保存的本地编辑并继续吗？')) return;
    if (!window.crypto?.randomUUID) { setError('安全随机 ID 不可用。请使用 HTTPS 或可信 localhost 后重试。'); return; }
    lock.current = true; setBusy(true); setError(null);
    if (!idem.current || idem.current.params !== params) idem.current = { params, key: crypto.randomUUID() };
    const input: CollectInput = { week, projectIds, fragments, previousTodo, consentScan: true, includeContext, idempotencyKey: idem.current.key };
    try { const value = await request<JobResponse>('/api/reports/collect', { method: 'POST', body: input }); job.start(value); void jobHistory.load(); }
    catch (e) { setError(errorMessage(e)); } finally { lock.current = false; setBusy(false); }
  }
  return <><PageHeading eyebrow="EVIDENCE → REVIEW → REPORT" title="周报工作台" description="先采集事实，再审核输入，最后一次性生成。没有定时任务，也不发布飞书。" />
    <div className="report-workspace"><div className="report-main"><Panel title="01 · 采集本周证据" aside={<Badge>显式同意 · 不调用模型</Badge>}>
      {!capabilities.reports && <Notice tone="warn">周报集成尚未配置。历史仍可查看和编辑，采集不可用。</Notice>}
      <form className="collect-form" onSubmit={e => void collect(e)}><label className="field">目标周起始日期<input type="date" value={week} onChange={e => setWeek(e.target.value)} required disabled={disabled} /></label>
        <fieldset disabled={disabled}><legend>允许采集的项目 <span className="muted">（至少一个）</span></legend><div className="project-options">{capabilities.projects.map(project => <Check key={project.id} checked={projectIds.includes(project.id)} label={<>{project.label}<small className="mono">{project.id}</small></>} onChange={checked => setProjectIds(ids => checked ? [...ids, project.id] : ids.filter(id => id !== project.id))} />)}</div>{!capabilities.projects.length && <p className="helper">未配置允许的项目根目录，请在服务器本机配置；网页不能添加任意路径。</p>}</fieldset>
        <div className="form-grid"><label className="field">本周零碎记录<textarea maxLength={30000} rows={5} value={fragments} onChange={e => setFragments(e.target.value)} placeholder="补充实际进展、验证结果、待测数字…" disabled={disabled} /></label><label className="field">上周未完成 Todo<textarea maxLength={15000} rows={5} value={previousTodo} onChange={e => setPreviousTodo(e.target.value)} placeholder="保留尚未完成的事项" disabled={disabled} /></label></div>
        <Notice tone="warn">采集会通过服务器配置的周报脚本扫描本地 Git 与现有 CLI 会话目录；仅保留选中允许项目的提交，默认不带会话摘录，不保存原始逐字记录。<details><summary>服务器扫描声明</summary><p>{capabilities.scanNotice}</p></details></Notice>
        <Check checked={consentScan} onChange={setConsentScan} disabled={disabled} label="我了解上述范围，同意本次扫描本地会话目录与所选项目。" />
        <Check checked={includeContext} onChange={setIncludeContext} disabled={disabled} label="额外保留脱敏 context 摘录（默认关闭）" />{includeContext && <Notice tone="warn">隐私提示：脱敏不保证消除所有敏感信息。摘录会显示在证据中，生成时可能发送给模型，请逐条审核。</Notice>}
        {error && <Notice tone="error">{error}</Notice>}<button className="button button-primary" disabled={!capabilities.reports || !week || !projectIds.length || !consentScan || disabled}>{busy ? '正在提交…' : job.busy && job.response?.job.kind === 'collect' ? '正在采集…' : '采集证据'}</button><p className="helper">同一组输入复用幂等键，提交失败可安全查询/重试；输入变化后创建新任务。切换页面会停止前端轮询，不取消服务器任务。</p>
      </form>{job.response && <JobStatus job={job} />}
    </Panel>{pendingReport && <Notice>新周报已采集，当前本地编辑没有被覆盖。<button className="button button-secondary" disabled={detailBusy} onClick={() => { select(pendingReport); setPendingReport(null); }}>审核新周报</button></Notice>}{selected ? <ReportDetail key={selected} id={selected} capabilities={capabilities} onDirty={setDirty} onBusy={setDetailBusy} onHistoryChange={() => { void history.load(); void jobHistory.load(); }} job={job} hasUnknown={hasUnknown} hasPending={hasPending} /> : <Panel title="02 · 审核与编辑"><Empty title="打开历史周报，或先采集证据">采集成功后在这里查看 SHA、MR 关联与所有警告。Markdown 只按纯文本编辑。</Empty></Panel>}<ReportJobs history={jobHistory} observer={job} capabilities={capabilities} onSelect={select} dirty={dirty} onChange={() => { void history.load(); void jobHistory.load(); }} /></div>
    <Panel title="历史周报" className="report-history" aside={<RefreshButton busy={history.loading} label="读取历史" onClick={() => void history.load()} />}><label className="field">搜索日期或项目<input type="search" value={search} onChange={e => setSearch(e.target.value)} placeholder="YYYY-MM-DD / 项目 ID" /></label>{history.error && <Notice tone="error">{history.error}</Notice>}{history.loading && <Loading label="读取服务器历史…" />}
      {history.data?.reports.filter(r => `${r.week} ${r.projectIds.join(' ')}`.includes(search)).map(report => <button key={report.id} className={`history-item ${selected === report.id ? 'selected' : ''}`} disabled={busy || detailBusy} onClick={() => select(report.id)} aria-pressed={selected === report.id}><div><strong>{report.week}</strong><Badge tone={report.generated ? 'good' : 'muted'}>{report.generated ? '已生成' : '已采集'}</Badge></div><small>{report.projectIds.length} 项目 · v{report.revision}</small><small>{dateTime(report.updatedAt)}</small>{report.warnings.length > 0 && <small className="warning-text">{report.warnings.length} 条警告</small>}</button>)}{!history.loading && !history.data?.reports.length && <Empty title="暂无历史" />}
      <p className="helper">持久化在服务器。刷新浏览器不会自动重发付费任务。下方任务历史可找回未完成任务与备选稿；暂停任务必须明确恢复，结果未知任务需重新确认重复计费风险。</p>
    </Panel></div></>;
}
