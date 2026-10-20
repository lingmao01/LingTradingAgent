import { useId, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** A trusted server projection. The browser never recomputes investment signals. */
export interface AdvisorReport {
  schema_version: number;
  snapshot_sha256: string;
  summary_text: string;
  full_text: string;
  signal_cards: unknown[];
  action_cards: unknown[];
}
export function isAdvisorReport(value: unknown): value is AdvisorReport {
  if (!value || typeof value !== 'object') return false;
  const row = value as Record<string, unknown>;
  return row.schema_version === 2 && typeof row.snapshot_sha256 === 'string'
    && typeof row.summary_text === 'string' && typeof row.full_text === 'string'
    && Array.isArray(row.signal_cards) && Array.isArray(row.action_cards);
}
export function AdvisorAnswer({ report }: { report: AdvisorReport }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const id = useId();
  async function copy() {
    try { await navigator.clipboard.writeText(report.summary_text); setCopied(true); }
    catch { setCopied(false); }
  }
  function exportReport() {
    const url = URL.createObjectURL(new Blob([report.full_text], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = `K-ETF-${report.snapshot_sha256.slice(0, 12)}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  const markdown = (content: string) => <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    table: ({ children }) => <div className="max-w-full overflow-x-auto"><table>{children}</table></div>,
    a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
  }}>{content}</ReactMarkdown>;
  return <section aria-label="持仓研究结论" className="min-w-0 max-w-full break-words [overflow-wrap:anywhere]">
    <div className="prose prose-sm dark:prose-invert max-w-none">{markdown(report.summary_text)}</div>
    <div className="mt-3 flex flex-wrap gap-3 text-xs text-muted-foreground">
      <button type="button" onClick={copy}>{copied ? '已复制结论' : '复制结论'}</button>
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>{open ? '收起完整报告' : '展开完整报告'}</button>
      <button type="button" onClick={exportReport}>导出完整报告</button>
    </div>
    {open && <div id={id} className="prose prose-sm dark:prose-invert mt-4 max-w-full overflow-hidden">{markdown(report.full_text)}</div>}
  </section>;
}
