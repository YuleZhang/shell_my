import type { Evidence } from '../../../shared/contracts';
import { Badge, dateTime, Empty, ExternalLink, Notice } from '../presentation';

export function Warnings({ warnings }: { warnings: string[] }) {
  return warnings.length ? <Notice tone="warn"><strong>请审核以下警告</strong><ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></Notice> : null;
}
export function EvidenceView({ evidence }: { evidence: Evidence }) {
  return <div className="evidence-view"><div className="bucket-heading"><h3>提交与 MR 证据</h3><Badge>{evidence.commits.length} 条提交</Badge></div><p className="helper">{evidence.weekStart} → {evidence.weekEnd} · 时间近似关联是弱证据，不代表 MR 已验证或任务已完成。</p><Warnings warnings={evidence.warnings} />
    {!evidence.commits.length && <Empty title="此周期没有保留的提交证据">检查项目/周期与采集警告，不将空结果伪称已完成进展。</Empty>}
    {evidence.commits.map((commit, index) => <details className="commit-evidence" key={`${commit.projectId}:${commit.sha}:${index}`}><summary><div><strong>{commit.title}</strong><small>{commit.projectId} · {dateTime(commit.time)}</small></div><Badge tone={commit.mrStatus === 'verified' ? 'good' : commit.mrStatus === 'unverified' ? 'warn' : 'muted'}>MR {commit.mrStatus === 'verified' ? '已验证' : commit.mrStatus === 'unverified' ? '未验证' : '未关联'} ({commit.mrStatus})</Badge></summary><div className="commit-body"><dl><dt>SHA</dt><dd className="mono">{commit.sha}</dd><dt>分支</dt><dd>{commit.branch || '未提供'}</dd><dt>匹配依据</dt><dd>{commit.match || '未提供'} {commit.match.toLowerCase().includes('time') && <Badge tone="warn">时间匹配 · 弱证据</Badge>}</dd><dt>提交正文</dt><dd className="pre-wrap">{commit.body || '无正文'}</dd></dl><h4>关联 MR</h4>{commit.mrs.map((mr, i) => <div className="evidence-mr" key={`${mr.url}:${i}`}><ExternalLink url={mr.url}>{mr.title}</ExternalLink><Badge>{mr.state}</Badge></div>)}{!commit.mrs.length && <p className="helper">没有关联 MR，不代表没有远端 MR。</p>}{commit.context !== null && <><h4>脱敏 context 摘录</h4><Notice tone="warn">此摘录可能在生成时发送给模型，请核实隐私与事实。</Notice><pre className="context-text">{commit.context}</pre></>}</div></details>)}
  </div>;
}
