import { useCallback, useEffect, useRef, useState } from 'react';
import type { Capabilities, Report, ReportResponse } from '../../../shared/contracts';
import { errorMessage, RequestError, request } from '../client';
import type { useJob } from '../hooks';
import { jobLabels } from '../jobs';
import { Badge, dateTime, Loading, Notice, Panel } from '../presentation';
import { TextExport } from './ReportActions';
import { EvidenceView, Warnings } from './ReportEvidence';
import { ReportGeneration } from './ReportGeneration';

export function JobStatus({ job }: { job: ReturnType<typeof useJob> }) {
  const value = job.response?.job; if (!value) return null;
  return <div className="job-status" aria-live="polite"><div className="bucket-heading"><strong>{value.kind === 'collect' ? '采集任务' : '生成任务'}</strong><Badge tone={value.state === 'succeeded' ? 'good' : ['failed', 'outcome_unknown'].includes(value.state) ? 'bad' : 'warn'}>{jobLabels[value.state]}</Badge></div><small className="mono">任务 ID：{value.id}</small><small>服务端更新 {dateTime(value.updatedAt)}</small>{value.error && <Notice tone="error">{value.error.message}（{value.error.code}）</Notice>}{job.error && <Notice tone="error">状态查询失败：{job.error}。未重新提交任务。</Notice>}{job.active && <button type="button" className="button button-secondary" onClick={() => job.setPaused(!job.paused)}>{job.paused ? '继续查询状态（不恢复任务）' : '暂停状态查询'}</button>}{job.paused && job.active && <p className="helper">仅暂停浏览器轮询，服务器任务未取消；不代表退款。排队任务可能需在任务历史中明确恢复。</p>}{value.state === 'awaiting_resume' && <Notice tone="warn">任务已暂停，不再轮询，也不会自动调用模型。请在服务器任务历史中明确恢复并重新确认输入与预算。</Notice>}</div>;
}
export function ReportDetail({ id, capabilities, onDirty, onBusy, onHistoryChange, job, hasUnknown, hasPending }: {
  id: string; capabilities: Capabilities; onDirty: (v: boolean) => void; onBusy: (v: boolean) => void; onHistoryChange: () => void;
  job: ReturnType<typeof useJob>; hasUnknown: boolean; hasPending: boolean;
}) {
  const [report, setReport] = useState<Report | null>(null); const [text, setText] = useState('');
  const [incoming, setIncoming] = useState<Report | null>(null); const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false); const saveLock = useRef(false);
  const [generating, setGenerating] = useState(false); const [error, setError] = useState<string | null>(null); const [saved, setSaved] = useState(false);
  const dirty = report !== null && text !== report.text;
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const loadSequence = useRef(0);
  const historyRef = useRef(onHistoryChange); historyRef.current = onHistoryChange;
  const reload = useCallback(async () => {
    const sequence = ++loadSequence.current; setLoading(true); setError(null);
    try {
      const value = await request<ReportResponse>(`/api/reports/${encodeURIComponent(id)}`);
      if (sequence !== loadSequence.current) return;
      if (dirtyRef.current) setIncoming(value.report);
      else { setReport(value.report); setText(value.report.text); setIncoming(null); }
      historyRef.current();
    } catch (e) { if (sequence === loadSequence.current) setError(errorMessage(e)); }
    finally { if (sequence === loadSequence.current) setLoading(false); }
  }, [id]);
  useEffect(() => { void reload(); return () => { loadSequence.current++; }; }, [reload]);
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => { onBusy(saving || generating || loading); return () => onBusy(false); }, [saving, generating, loading, onBusy]);
  async function save() {
    if (!report || saving || saveLock.current || incoming) return;
    saveLock.current = true; setSaving(true); setError(null); setSaved(false);
    try {
      const value = await request<ReportResponse>(`/api/reports/${encodeURIComponent(id)}`, { method: 'PATCH', body: { text, revision: report.revision } });
      setReport(value.report); setText(value.report.text); setSaved(true); historyRef.current();
    } catch (e) {
      setError(errorMessage(e));
      if (e instanceof RequestError && e.status === 409) {
        try { const value = await request<ReportResponse>(`/api/reports/${encodeURIComponent(id)}`); setIncoming(value.report); }
        catch (loadError) { setError(`版本冲突；读取最新版本失败：${errorMessage(loadError)}。本地编辑已保留。`); }
      }
    } finally { saveLock.current = false; setSaving(false); }
  }
  if (!report) return <Panel title="02 · 审核、编辑与导出">{loading && <Loading label="正在读取周报…" />}{error && <Notice tone="error">{error}</Notice>}<button className="button button-secondary" disabled={loading} onClick={() => void reload()}>重新读取周报</button></Panel>;
  return <><Panel title={`02 · ${report.week} 周报`} aside={<Badge>{report.generation ? '已生成' : '已采集'} · v{report.revision}</Badge>}>
    <p className="helper">项目：{report.projectIds.join('、')} · 更新于 {dateTime(report.updatedAt)}</p><Warnings warnings={report.warnings} /><EvidenceView evidence={report.evidence} />
    <details className="report-inputs"><summary>审核本次采集的补充记录与上周 Todo</summary><h4>零碎记录</h4><pre className="context-text">{report.fragments || '无补充记录'}</pre><h4>上周 Todo</h4><pre className="context-text">{report.previousTodo || '无上周 Todo'}</pre></details>
    <div className="editor-heading"><h3>Markdown 正文</h3><Badge tone={dirty ? 'warn' : 'good'}>{dirty ? '本地未保存' : '与服务器版本一致'}</Badge></div><p className="helper">纯文本编辑，不渲染原始 HTML。手写内容不会被标记为模型生成；生成结果仍需核实事实。</p>
    {error && <Notice tone="error">{error}</Notice>}{loading && <Loading label="读取最新服务端版本…" />}
    {incoming && <div className="conflict"><Notice tone="warn">服务器已有 v{incoming.revision}，你的本地编辑被保留，未自动覆盖。请先复制备份，再明确选择恢复方式。</Notice><details open><summary>服务器最新正文（只读）</summary><textarea className="markdown-editor" readOnly rows={8} value={incoming.text} aria-label="服务器最新周报正文" /></details><div className="button-row"><button className="button button-secondary" disabled={saving || generating} onClick={() => { if (window.confirm('用服务器最新正文替换本地编辑？请确认已复制本地备份。')) { setReport(incoming); setText(incoming.text); setIncoming(null); setError(null); } }}>采用服务器版本</button><button className="button button-secondary" disabled={saving || generating} onClick={() => { if (window.confirm('保留本地正文并以服务器最新版本为基线？请先比对差异。仍需再次点击保存，不会自动覆盖服务器。')) { setReport(incoming); setIncoming(null); setError(null); } }}>保留本地正文，更新版本基线</button></div></div>}
    <label className="field"><span className="sr-only">周报 Markdown 正文</span><textarea className="markdown-editor" value={text} rows={18} maxLength={100000} disabled={saving} onChange={e => { setText(e.target.value); setSaved(false); }} spellCheck={false} /></label><div className="editor-footer"><div className="button-row"><button className="button button-primary" disabled={!dirty || saving || loading || !!incoming} onClick={() => void save()}>{saving ? '保存中…' : '保存正文'}</button><button className="button button-secondary" disabled={saving || loading} onClick={() => void reload()}>读取服务器最新版本</button></div><small>{text.length.toLocaleString('zh-CN')} / 100,000 字符</small></div>{saved && <Notice>正文已保存至服务器。</Notice>}<TextExport text={text} week={report.week} />
    {report.generation && <details className="report-inputs"><summary>模型生成来源与输入凭据</summary><dl><dt>模型</dt><dd>{report.generation.modelId}</dd><dt>账户 ID</dt><dd>{report.generation.accountId ?? '服务器配置的模型凭据'}</dd><dt>规则 Hash</dt><dd className="mono">{report.generation.skillHash}</dd><dt>输入 Hash</dt><dd className="mono">{report.generation.inputHash}</dd><dt>提示版本</dt><dd>{report.generation.promptVersion}</dd></dl></details>}
  </Panel><ReportGeneration report={report} capabilities={capabilities} dirty={dirty || !!incoming} onComplete={reload} onApply={value => { setText(value); setSaved(false); }} onBusy={setGenerating} job={job} hasUnknown={hasUnknown} hasPending={hasPending} /></>;
}
