import { useEffect, useRef, useState } from 'react';
import type { AccountsResponse, Capabilities, JobDiscovery, JobResponse, ReportResponse } from '../../../shared/contracts';
import { errorMessage, RequestError, request } from '../client';
import { useResource } from '../hooks';
import { generationResumeInput, resumeProblem } from '../jobs';
import { Loading, Notice } from '../presentation';
import { BudgetCheck } from './ReportActions';

export function ResumeGeneration({ job, capabilities, dirty, onClose, onStarted, onChanged }: {
  job: JobDiscovery; capabilities: Capabilities; dirty: boolean; onClose: () => void;
  onStarted: (value: JobResponse) => void; onChanged: () => void;
}) {
  const report = useResource<ReportResponse>(`/api/reports/${encodeURIComponent(job.reportId!)}`);
  const accounts = useResource<AccountsResponse>('/api/accounts');
  const [confirmed, setConfirmed] = useState(false); const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null); const lock = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => { setConfirmed(false); }, [report.data, accounts.data, dirty]);
  const current = report.data?.report, saved = job.request;
  const model = capabilities.models.find(m => m.id === saved?.modelId);
  const account = accounts.data?.accounts.find(a => a.id === saved?.accountId);
  const problem = current ? resumeProblem(job, current, capabilities, accounts.data?.accounts ?? []) : '请先读取当前周报输入规模。';
  async function resume() {
    if (lock.current || !current || problem || !confirmed || dirty) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      const value = await request<JobResponse>(`/api/jobs/${encodeURIComponent(job.id)}/resume`, { method: 'POST', body: generationResumeInput(job, current) });
      onStarted(value);
    } catch (e) {
      setError(errorMessage(e)); setConfirmed(false); onChanged();
      if (e instanceof RequestError && e.code === 'input_scale_changed') await report.load();
    } finally { lock.current = false; setBusy(false); }
  }
  return <dialog ref={dialog} className="resume-dialog" aria-labelledby="resume-title" onCancel={e => { e.preventDefault(); if (!busy) onClose(); }}>
    <h2 id="resume-title">明确恢复指定生成任务</h2><p className="mono">{job.id}</p>
    <Notice tone="warn">服务器重启后任务已暂停，尚未自动调用模型。本次确认只恢复此任务，不改用其他账户或模型。将重新读取规则并使用当前报告版本；不代表旧输入凭据仍有效。</Notice>
    {(report.loading || accounts.loading) && <Loading label="读取当前报告与缓存账户预算…" />}
    {report.error && <Notice tone="error">{report.error}</Notice>}{accounts.error && model?.usesAccount && <Notice tone="error">{accounts.error}</Notice>}
    <dl><dt>原模型</dt><dd>{model?.label ?? saved?.modelId ?? '未知'}</dd><dt>原账户</dt><dd>{account ? `${account.alias || account.originalName}（${account.id}）` : saved?.accountId ?? '服务器模型凭据'}</dd><dt>原输出上限</dt><dd>{saved?.maxOutputTokens} tokens（当前上限 {Math.min(16000, model?.maxOutputTokens ?? 0)}）</dd><dt>当前输入规模</dt><dd>{current?.generationReadiness?.ready ? `${current.inputChars} 字符 / 上限 ${model?.maxInputChars ?? '—'}` : '当前不可确认'}</dd><dt>报告版本</dt><dd>原 v{saved?.reportRevision} → 当前 v{current?.revision ?? '—'}</dd><dt>幂等键</dt><dd>复用原任务幂等键，不创建新的付费任务</dd></dl>
    {account && <div className="resume-budget"><strong>原账户缓存预算（不刷新供应商）</strong><small>更新 {account.usage.updatedAt ?? '未知'} · {account.usage.stale ? '可能过期' : '已缓存'}</small>{account.usage.error && <Notice tone="warn">{account.usage.error.message}</Notice>}{account.usage.data.map(bucket => <p key={bucket.id} className="helper">{bucket.label} · 剩余 {bucket.remaining ?? '未知'} {bucket.currency ?? ''} · 到期 {bucket.expiresAt ?? '未知'}</p>)}{!account.usage.data.length && <p className="helper">暂无预算快照，请自行核实；不能保证具体费用。</p>}</div>}
    {problem && <Notice tone="warn">{problem}</Notice>}{dirty && <Notice tone="warn">存在未保存编辑，请关闭弹窗并先处理编辑。</Notice>}
    <BudgetCheck checked={confirmed} onChange={setConfirmed} />{error && <Notice tone="error">{error}。没有自动重试；可先关闭并重新查看任务状态。</Notice>}
    <div className="button-row"><button className="button button-secondary" disabled={busy} onClick={onClose}>取消，不恢复</button><button className="button button-secondary" disabled={busy || report.loading || accounts.loading} onClick={() => { setConfirmed(false); void report.load(); void accounts.load(); }}>重新读取规模与缓存预算</button><button className="button button-primary" disabled={busy || report.loading || accounts.loading || !!problem || dirty || !confirmed || (!!accounts.error && !!model?.usesAccount)} onClick={() => void resume()}>{busy ? '正在提交恢复…' : '承担调用费用，恢复此生成任务'}</button></div>
  </dialog>;
}
