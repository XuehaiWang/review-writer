import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useUiText } from "../../i18n/useUiText";

export type DialogueSource = { ref: string; paper_id: string; title: string; page: number | null; text: string };

export function EvidenceLinks({ sources = [], legacyRefs = [] }: { sources?: DialogueSource[]; legacyRefs?: string[] }) {
  const { text } = useUiText();
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<DialogueSource | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const unique = [...new Map(sources.filter(s => s.paper_id && s.ref).map(s => [s.ref, s])).values()];
  useEffect(() => {
    if (selected) dialog.current?.showModal();
  }, [selected]);
  if (!unique.length) return legacyRefs.length ? <details className="chat-evidence"><summary>{text("历史证据编号", "Historical source references")}</summary>
    <p>{text("此旧回复未保存证据片段，无法可靠定位链接。", "This older reply has no saved passages to resolve reliable links.")}</p>
    {legacyRefs.map(ref => <small key={ref}>{ref} </small>)}</details> : null;
  const page = selected && Number.isInteger(selected.page) && Number(selected.page) > 0 ? selected.page : null;
  return <div className="chat-evidence">
    <span>{text("参考证据：", "Evidence: ")}</span>
    {(expanded ? unique : unique.slice(0, 3)).map((source, i) => <button type="button" className="evidence-link" key={source.ref}
      title={source.title || source.paper_id} onClick={event => { trigger.current = event.currentTarget; setSelected(source); }}>
      {text(`证据 ${i + 1}`, `Source ${i + 1}`)} · {source.title || source.paper_id}
      {Number.isInteger(source.page) && Number(source.page) > 0 ? text(` · 第 ${source.page} 页`, ` · p. ${source.page}`) : ""}
    </button>)}
    {unique.length > 3 ? <button className="button button-quiet" onClick={() => setExpanded(!expanded)}>{expanded ? text("收起", "Less") : text(`展开更多（${unique.length - 3}）`, `More (${unique.length - 3})`)}</button> : null}
    {selected ? createPortal(<dialog ref={dialog} className="evidence-drawer" aria-label={text("原文证据", "Source evidence")}
      onClose={() => { setSelected(null); trigger.current?.focus(); }}>
      <header><h2>{text("原文证据", "Source evidence")}</h2><button className="button button-secondary" onClick={() => dialog.current?.close()}>{text("关闭", "Close")}</button></header>
      <h3>{selected.title || selected.paper_id}</h3>
      <p className="muted">{page ? text(`原始 PDF 第 ${page} 页`, `Original PDF page ${page}`) : text("未记录可靠页码", "No reliable page number recorded")}</p>
      <blockquote>{selected.text || text("此记录没有可展示的原文片段。", "No source passage was recorded.")}</blockquote>
      <p className="muted">{text("这是生成回复时保存的证据片段；原文件若已删除或无访问权限，PDF 将无法打开。", "This passage was saved with the reply. The PDF may be unavailable if deleted or no longer accessible.")}</p>
      <a className="button button-primary" target="_blank" rel="noopener noreferrer" href={`/api/v1/library/papers/${encodeURIComponent(selected.paper_id)}/pdf${page ? `#page=${page}` : ""}`}>{text("打开原始 PDF", "Open original PDF")}</a>
    </dialog>, document.body) : null}
  </div>;
}
