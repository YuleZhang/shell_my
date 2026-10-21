import type { Account, Capabilities, GenerateInput, JobDiscovery, JobState, Report, ResumeJobInput } from '../../shared/contracts';

export const jobLabels: Record<JobState, string> = {
  queued: '已排队', preparing: '准备中', ready: '输入就绪', awaiting_resume: '等待明确恢复',
  sending: '已发送，等待模型', succeeded: '已完成', failed: '失败', outcome_unknown: '结果未知 · 可能已扣费',
};
export function isActiveJob(state: JobState) {
  return state === 'queued' || state === 'preparing' || state === 'ready' || state === 'sending';
}
export function generationDisabled(report: Report, capabilities: Capabilities): string | null {
  if (!capabilities.generation || !capabilities.models.length) return '未配置可用模型，生成已禁用。历史周报仍可查看、编辑和导出。';
  if (report.generationReadiness?.ready !== true) return `服务器规则或生成配置当前不可用，生成已禁用；历史正文和编辑不受影响。${report.generationReadiness?.error ? `（${report.generationReadiness.error.code}）` : '请读取最新周报以确认配置。'}`;
  return null;
}
export function resumeProblem(job: JobDiscovery, report: Report, capabilities: Capabilities, accounts: Account[]): string | null {
  if (job.state !== 'awaiting_resume' || job.resumeAction !== 'generate' || !job.request || job.reportId !== report.id) return '此任务当前不能恢复，请重新查看任务状态。';
  const disabled = generationDisabled(report, capabilities); if (disabled) return disabled;
  const saved = job.request, model = capabilities.models.find(m => m.id === saved.modelId);
  if (!model) return '原模型已不可用。不能自动改用其他模型。';
  if (report.inputChars > model.maxInputChars) return '当前输入超过原模型上限，不能恢复。';
  if (!Number.isInteger(saved.maxOutputTokens) || saved.maxOutputTokens < 100 || saved.maxOutputTokens > Math.min(16000, model.maxOutputTokens)) return '原输出上限超过当前模型限制，不能静默改动原任务。';
  if (model.usesAccount ? !saved.accountId || !accounts.some(a => a.id === saved.accountId && a.enabled && a.available) : !!saved.accountId) return '原账户或模型账户绑定已不可用，不能自动切换。';
  return null;
}
export function generationResumeInput(job: JobDiscovery, report: Report): ResumeJobInput {
  if (!job.request) throw new Error('Missing original generation binding');
  const saved = job.request;
  const input: GenerateInput = { modelId: saved.modelId, ...(saved.accountId ? { accountId: saved.accountId } : {}),
    maxOutputTokens: saved.maxOutputTokens, idempotencyKey: saved.idempotencyKey,
    confirmedInputChars: report.inputChars, confirmBudget: true, retryUnknown: false };
  return { kind: 'generate', confirmResume: true, ...input };
}
