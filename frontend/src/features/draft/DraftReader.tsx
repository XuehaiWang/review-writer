import { useEffect, useRef, useState } from "react";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";

/** Render the original Markdown once; navigation never reconstructs manuscript content. */
export function DraftReader({ content, onChapter, onOverview, discussOnSelect = false }: { content: string; onChapter?: (title: string) => void; onOverview?: () => void; discussOnSelect?: boolean }) {
  const { text } = useUiText();
  const container = useRef<HTMLDivElement>(null);
  const [headings, setHeadings] = useState<HTMLElement[]>([]);
  const [active, setActive] = useState(0);
  const [contentsOpen, setContentsOpen] = useState(true);
  useEffect(() => { setHeadings(Array.from(container.current?.querySelectorAll<HTMLElement>("h2,h3") || [])); }, [content]);
  return <div className={`draft-review-split${contentsOpen ? "" : " draft-contents-collapsed"}`}>
    <div className="draft-reader-toolbar"><button type="button" className="button button-quiet" aria-expanded={contentsOpen} onClick={() => setContentsOpen(open => !open)}>{contentsOpen ? text("收起目录", "Hide contents") : text("展开目录", "Show contents")}</button>
      {!contentsOpen && onChapter ? <button className="button button-primary" onClick={() => onChapter(headings[active]?.textContent || "")}>{text("讨论当前章节", "Discuss current chapter")}</button> : null}
    </div>
    <nav hidden={!contentsOpen} className="draft-item-list" aria-label={text("正文目录", "Manuscript contents")}>
      <div className="draft-contents-heading">{text("文章目录", "Contents")}<small>{text("选择章节定位正文", "Select a chapter to navigate")}</small></div>
      {headings.map((h, i) => <button type="button" key={i} className={active === i ? "selected" : ""}
        onClick={() => { setActive(i); const panel = container.current; if (panel) panel.scrollTo({ top: panel.scrollTop + h.getBoundingClientRect().top - panel.getBoundingClientRect().top - 12, behavior: "smooth" }); if (discussOnSelect) onChapter?.(h.textContent || ""); }}>{h.textContent}</button>)}
      {onChapter ? <button className="button button-primary draft-discuss-button" onClick={() => onChapter(headings[active]?.textContent || "")}>{text("讨论当前章节", "Discuss current chapter")}</button> : null}
    </nav><div className="draft-long-document" ref={container} onScroll={() => {
      const top = container.current?.getBoundingClientRect().top || 0;
      const index = headings.reduce((last, h, i) => h.getBoundingClientRect().top <= top + 40 ? i : last, 0);
      setActive(Math.max(0, index));
    }} onClick={event => {
      const target = event.target as HTMLElement;
      const heading = target.closest("h2,h3");
      if (heading) onChapter?.(heading.textContent || "");
      if (target instanceof HTMLImageElement && target.alt === "Overview figure") onOverview?.();
    }}><MarkdownView content={content} /></div>
  </div>;
}
