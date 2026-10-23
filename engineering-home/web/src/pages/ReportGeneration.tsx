import { useEffect, useRef, useState } from 'react';
import type { AccountsResponse, Capabilities, JobResponse, Report } from '../../../shared/contracts';
import { errorMessage, RequestError, request } from '../client';
import { useResource } from '../hooks';
import type { useJob } from '../hooks';
import { generationDisabled } from '../jobs';
import { Check, Notice, Panel } from '../presentation';
import { BudgetCheck } from './ReportActions';
import { GenerationDraft } from './GenerationDraft';
import { JobStatus } from './ReportDetail';

export function ReportGeneration({ report, capabilities, dirty, onComplete, onApply, onBusy, job, hasUnknown, hasPending }: {
  report: Report; capabilities: Capabilities; dirty: boolean; onComplete: () => Promise<void>;
  onApply: (text: string) => void; onBusy: (v: boolean) => void; job: ReturnType<typeof useJob>;
  hasUnknown: boolean; hasPending: boolean;
}) {
  const accounts = useResource<AccountsResponse>('/api/accounts');
  const [modelId, setModelId] = useState(capabilities.models[0]?.id ?? '');
  const [accountId, setAccountId] = useState(''); const [tokens, setTokens] = useState(2000);
  const [confirmed, setConfirmed] = useState(false); const [retry, setRetry] = useState(false);
  const [unknown, setUnknown] = useState(false); const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false); const lock = useRef(false); const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const completed = useRef<string | null>(null);
  const selectedJob = job.response?.job.reportId === report.id && job.response.job.kind === 'generate' ? job.response : null;
  const uncertain = unknown || hasUnknown || selectedJob?.job.state === 'outcome_unknown';
  const disabledReason = generationDisabled(report, capabilities);
  const activeAccount = accounts.data?.accounts.some(a => a.id === accountId && a.enabled && a.available);
  const idem = useRef<{ params: string; key: string } | null>(null);
  const model = capabilities.models.find(m => m.id === modelId);
  const maxTokens = Math.min(16000, model?.maxOutputTokens ?? 16000);
  const params = JSON.stringify({ reportId: report.id, revision: report.revision, inputChars: report.inputChars, modelId, accountId: model?.usesAccount ? accountId : undefined, tokens, retry, attempt });
  useEffect(() => { setConfirmed(false); idem.current = null; }, [params]);
  useEffect(() => { setTokens(value => Math.min(value, maxTokens)); }, [maxTokens]);
  useEffect(() => { onBusy(busy || refreshing); return () => onBusy(false); }, [busy, refreshing, onBusy]);
  useEffect(() => { setConfirmed(false); }, [disabledReason, accounts.data, hasUnknown]);
  useEffect(() => {
    const value = selectedJob?.job;
    if (!value || !['succeeded', 'outcome_unknown', 'failed'].includes(value.state) || completed.current === value.id) return;
    completed.current = value.id;
    if (value.state === 'outcome_unknown') { setUnknown(true); setRetry(false); setConfirmed(false); setAttempt(v => v + 1); }
    if (value.state === 'succeeded') {
      setUnknown(false); setRetry(false); setConfirmed(false); setRefreshing(true);
      void onComplete().finally(() => setRefreshing(false));
    }
  }, [selectedJob?.job, onComplete]);
  async function generate() {
    if (lock.current || busy || job.busy || refreshing || hasPending || disabledReason || !model || !confirmed || dirty || (uncertain && !retry) || (model.usesAccount && !activeAccount) || report.inputChars > model.maxInputChars || !Number.isInteger(tokens) || tokens < 100 || tokens > maxTokens) return;
    if (!crypto.randomUUID) { setError('安全随机 ID 不可用，请使用 HTTPS 或 localhost。'); return; }
    lock.current = true; setBusy(true); setError(null);
    if (!idem.current || idem.current.params !== params) idem.current = { params, key: crypto.randomUUID() };
    try {
      const value = await request<JobResponse>(`/api/reports/${encodeURIComponent(report.id)}/generate`, { method: 'POST', body: {
        modelId, ...(model.usesAccount ? { accountId } : {}), maxOutputTokens: tokens,
        confirmedInputChars: report.inputChars, confirmBudget: true, retryUnknown: retry, idempotencyKey: idem.current.key,
      } }); job.start(value);
    } catch (e) {
      setError(errorMessage(e));
      if (e instanceof RequestError && e.code === 'explicit_retry_required') { setUnknown(true); setRetry(false); setConfirmed(false); idem.current = null; setAttempt(v => v + 1); }
      if (e instanceof RequestError && e.code === 'input_scale_changed') { setConfirmed(false); await onComplete(); }
    } finally { lock.current = false; setBusy(false); }
  }
  const blocked = busy || job.busy || refreshing;
  const overInput = !!model && report.inputChars > model.maxInputChars;
  return <Panel title="03 · 一次性生成" aside={<span className="muted">无工具 · 不发布</span>}>
    {disabledReason && <Notice tone="warn">{disabledReason}</Notice>}
    {hasPending && <Notice tone="warn">服务器已有未完成或等待恢复的生成任务。请在任务历史中查看状态或明确恢复，不创建另一个生成任务。</Notice>}
    <fieldset disabled={blocked || !!disabledReason || hasPending} className="generation-fields"><legend className="sr-only">生成选项</legend><div className="form-grid"><label className="field">模型<select value={modelId} onChange={e => setModelId(e.target.value)}><option value="" disabled>请选择配置的模型</option>{capabilities.models.map(m => <option key={m.id} value={m.id}>{m.label}</option>)}</select></label>
      {model?.usesAccount && <label className="field">用于本次调用的账户<select value={accountId} onChange={e => setAccountId(e.target.value)}><option value="">请选择可用账户</option>{accounts.data?.accounts.filter(a => a.enabled && a.available).map(a => <option key={a.id} value={a.id}>{a.alias || a.originalName} · {a.originalName}</option>)}</select></label>}
      <label className="field">最大输出 tokens<input type="number" min={100} max={maxTokens} step={1} value={tokens} onChange={e => setTokens(Number(e.target.value))} /></label></div>
      {accounts.error && model?.usesAccount && <Notice tone="error">{accounts.error}</Notice>}
      <div className="budget-summary"><div><small>服务端确认的输入规模</small><strong>{disabledReason ? '当前不可确认' : `${report.inputChars.toLocaleString('zh-CN')} 字符`}</strong></div><div><small>模型输入上限</small><strong>{model?.maxInputChars.toLocaleString('zh-CN') ?? '—'} 字符</strong></div><div><small>模型输出上限</small><strong>{model?.maxOutputTokens.toLocaleString('zh-CN') ?? '—'} tokens</strong></div></div>
      <p className="helper">不虚构精确价格。计费由模型/账户提供方决定；输出 token 上限不是金额预算。账户标签不强制路由。发送的是已审核的证据、记录、上周 Todo 与服务器规则，不含原始会话全文。</p>
      {dirty && <Notice tone="warn">有未保存编辑，请先保存再生成。任务发送期间的新编辑会保留，结果可能成为备选稿。</Notice>}{overInput && <Notice tone="error">输入超过所选模型上限，请选择容量更大的已配置模型，或重新采集更小范围。</Notice>}
      {uncertain && <><Notice tone="error">服务器记录了生成结果未知的任务，供应商可能已扣费。不会自动重试；必须使用新的幂等键，这次调用仍可能重复计费。</Notice><Check checked={retry} onChange={setRetry} label="我确认对同一报告手动重试，理解上一次可能已扣费，再次调用也可能收费。" /></>}
      <BudgetCheck checked={confirmed} onChange={setConfirmed} />
    </fieldset>{error && <Notice tone="error">{error}</Notice>}
    <button className="button button-primary" disabled={blocked || hasPending || !!disabledReason || dirty || !model || overInput || !confirmed || (uncertain && !retry) || (model.usesAccount && !activeAccount) || !Number.isInteger(tokens) || tokens < 100 || tokens > maxTokens} onClick={() => void generate()}>{busy ? '正在提交…' : job.busy ? '任务状态查询中…' : uncertain ? '确认可能重复计费，创建新任务重试' : '确认并生成'}</button>
    {selectedJob && <JobStatus job={job} />}
    {selectedJob?.result && <GenerationDraft result={selectedJob.result} week={report.week} onApply={onApply} />}
  </Panel>;
}
