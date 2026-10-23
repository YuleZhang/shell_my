import { useRef, useState } from 'react';
import { Check, Notice } from '../presentation';
import { Clipboard, Download } from 'lucide-react';

export function TextExport({ text, week, label = '周报' }: { text: string; week: string; label?: string }) {
  const [fallback, setFallback] = useState(false); const [copied, setCopied] = useState(false);
  const selection = useRef<HTMLTextAreaElement>(null);
  async function copy() {
    setCopied(false);
    try { if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable'); await navigator.clipboard.writeText(text); setCopied(true); setFallback(false); }
    catch { setFallback(true); requestAnimationFrame(() => { selection.current?.focus(); selection.current?.select(); }); }
  }
  function download() {
    const date = new Date(`${week}T00:00:00Z`);
    const valid = /^\d{4}-\d{2}-\d{2}$/.test(week) && Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === week;
    if (!valid) { setFallback(true); return; }
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `weekly-report-${week}.md`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <div className="text-export"><div className="button-row"><button type="button" className="button button-secondary" disabled={!text} onClick={() => void copy()}><Clipboard size={15} aria-hidden="true" />复制{label}</button><button type="button" className="button button-secondary" disabled={!text || !week} onClick={download}><Download size={15} aria-hidden="true" />下载 .md</button></div>{copied && <Notice>已复制纯文本。</Notice>}{fallback && <><Notice tone="warn">剪贴板不可用（远程 HTTP 可能受限）。文本已选中，请使用 Ctrl/Cmd+C 或长按复制。</Notice><textarea ref={selection} className="markdown-editor" readOnly value={text} rows={8} aria-label="手动复制纯文本" onFocus={e => e.target.select()} /></>}</div>;
}
export function BudgetCheck({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return <Check checked={checked} onChange={onChange} label="我已审核证据和输入规模，同意向所选模型发送这些数据并承担调用费用。输出 token 上限不是货币预算，也不能保证具体费用。" />;
}
