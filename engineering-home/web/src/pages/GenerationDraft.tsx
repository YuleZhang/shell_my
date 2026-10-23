import type { GenerationResult } from '../../../shared/contracts';
import { Notice } from '../presentation';
import { TextExport } from './ReportActions';
import { Warnings } from './ReportEvidence';

export function GenerationDraft({ result, week, onApply }: { result: GenerationResult; week: string; onApply?: (text: string) => void }) {
  return <div className="alternative-result"><Notice tone="warn">这是服务器持久保存的模型结果，刷新页面后仍可从任务历史找回。它可能已应用到报告，也可能因生成期间的编辑而保留为备选稿；不会自动覆盖你的本地正文。</Notice>
    <Warnings warnings={result.warnings} /><textarea className="markdown-editor" readOnly value={result.text} rows={10} aria-label="持久保存的生成结果" />
    <TextExport text={result.text} week={week} label="生成结果" />
    {onApply && <button className="button button-secondary" disabled={result.text.length > 100000} onClick={() => { if (window.confirm('用此生成结果替换本地编辑区？请先复制现有编辑。此操作不会自动保存到服务器。')) onApply(result.text); }}>手动应用到编辑区</button>}
    <details><summary>生成来源</summary><pre className="context-text">{JSON.stringify(result.generation, null, 2)}</pre></details>
  </div>;
}
