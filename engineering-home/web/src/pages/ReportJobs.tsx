import { useRef, useState } from 'react';
import type { Capabilities, JobDiscovery, JobResponse, JobsResponse } from '../../../shared/contracts';
import { errorMessage, request } from '../client';
import type { Resource, useJob } from '../hooks';
import { jobLabels } from '../jobs';
import { Badge, dateTime, Empty, Loading, Notice, Panel, RefreshButton } from '../presentation';
import { JobStatus } from './ReportDetail';
import { ResumeGeneration } from './ResumeGeneration';
import { GenerationDraft } from './GenerationDraft';

export function ReportJobs({ history, observer, capabilities, onSelect, onChange, dirty }: {
  history: Resource<JobsResponse>; observer: ReturnType<typeof useJob>; capabilities: Capabilities;
  onSelect: (id: string) => void; onChange: () => void; dirty: boolean;
}) {
  const [resume, setResume] = useState<JobDiscovery | null>(null);
  const [busy, setBusy] = useState(false); const lock = useRef(false);
  const [error, setError] = useState<string | null>(null);
  async function resumeCollect(value: JobDiscovery) {
    if (lock.current || !window.confirm(`明确恢复采集任务 ${value.id}？\n将按原范围扫描本地项目与会话目录（含原先选择的摘录范围），不调用模型。读取或恢复其他任务不会代替本次确认。`)) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      const result = await request<JobResponse>(`/api/jobs/${encodeURIComponent(value.id)}/resume`, { method: 'POST', body: { kind: 'collect', confirmResume: true } });
      observer.start(result); onChange();
    } catch (e) { setError(errorMessage(e)); void history.load(); }
    finally { lock.current = false; setBusy(false); }
  }
  async function prepareResume(id: string) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      const value = await observer.inspect(id);
      if (value?.job.resumeAction === 'generate' && value.job.state === 'awaiting_resume') setResume(value.job);
      else setError('任务状态已变化或读取失败，请查看状态；没有恢复或重发任何任务。');
    } finally { lock.current = false; setBusy(false); }
  }
  return <Panel title="服务器任务历史" aside={<RefreshButton busy={history.loading} label="读取任务列表" onClick={() => void history.load()} />}>
    <p className="helper">刷新页面后从服务器重新发现任务。读取列表、查看状态、打开报告均不会恢复任务或刷新外部网络数据；只有下方明确恢复按钮才提交指定任务。</p>
    {history.error && <Notice tone="error">{history.error}</Notice>}{error && <Notice tone="error">{error}</Notice>}
    {history.loading && <Loading label="读取服务器任务记录…" />}
    <div className="job-history">{history.data?.jobs.map(saved => {
      const value = observer.response?.job.id === saved.id && 'resumeAction' in observer.response.job ? observer.response.job : saved;
      return <article className="job-history-item" key={value.id}>
        <div className="bucket-heading"><strong>{value.kind === 'collect' ? '证据采集' : '模型生成'}</strong><Badge tone={value.state === 'succeeded' ? 'good' : value.state === 'failed' || value.state === 'outcome_unknown' ? 'bad' : 'warn'}>{jobLabels[value.state]}</Badge></div>
        <small className="mono">{value.id}</small><small>更新 {dateTime(value.updatedAt)} · {value.resultAvailable ? '产物已持久保存' : '暂无产物'}</small>
        {value.request && <p className="helper">原模型 {value.request.modelId} · 账户 {value.request.accountId ?? '服务器模型凭据'} · 输出上限 {value.request.maxOutputTokens} tokens · 原输入 {value.request.confirmedInputChars} 字符 · v{value.request.reportRevision}</p>}
        {value.generation && <details><summary>任务生成来源</summary><pre className="context-text">{JSON.stringify(value.generation, null, 2)}</pre></details>}
        <div className="button-row"><button className="button button-secondary" disabled={busy || observer.reading} onClick={() => void observer.inspect(value.id)}>查看任务状态</button>
          {value.reportId && <button className="button button-secondary" onClick={() => onSelect(value.reportId!)}>打开报告</button>}
          {value.state === 'queued' && value.resumeAction === 'collect' && <button className="button button-primary" disabled={busy || !capabilities.reports} onClick={() => void resumeCollect(value)}>明确恢复此采集任务</button>}
          {value.resumeAction === 'generate' && <button className="button button-primary" disabled={busy || dirty} onClick={() => void prepareResume(value.id)}>重新确认并恢复生成</button>}
        </div>
        {value.state === 'outcome_unknown' && <Notice tone="error">此任务可能已扣费，绝不自动恢复或重试。请打开报告，在生成区重新确认重复计费风险；将创建新的幂等键，不重发旧任务。</Notice>}
      </article>;
    })}</div>
    {!history.loading && !history.data?.jobs.length && <Empty title="暂无服务器任务记录" />}
    {observer.reading && <Loading label="读取指定任务状态…" />}{observer.error && !observer.response && <Notice tone="error">{observer.error}</Notice>}
    {observer.response && <JobStatus job={observer} />}
    {observer.response?.result && <GenerationDraft result={observer.response.result} week="" />}
    {dirty && <p className="helper">有未保存的报告编辑，请先保存或处理版本冲突后再恢复生成。</p>}
    {resume && <ResumeGeneration key={resume.id} job={resume} capabilities={capabilities} dirty={dirty} onClose={() => setResume(null)} onStarted={value => { observer.start(value); setResume(null); onChange(); }} onChanged={() => void history.load()} />}
  </Panel>;
}
