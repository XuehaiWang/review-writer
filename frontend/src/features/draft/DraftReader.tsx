import { useEffect, useRef, useState } from "react";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";

/** Render the original Markdown once; navigation never reconstructs manuscript content. */
export function DraftReader({ content }: { content: string }) {
  const { text } = useUiText();
  const container = useRef<HTMLDivElement>(null);
  const [headings, setHeadings] = useState<HTMLElement[]>([]);
  const [active, setActive] = useState(0);
  useEffect(() => { setHeadings(Array.from(container.current?.querySelectorAll<HTMLElement>("h1,h2,h3") || [])); }, [content]);
  return <div className="draft-review-split">
    <nav className="draft-item-list" aria-label={text("正文目录", "Manuscript contents")}>
      {headings.map((h, i) => <button type="button" key={i} className={active === i ? "selected" : ""}
        onClick={() => { setActive(i); const panel = container.current; if (panel) panel.scrollTo({ top: panel.scrollTop + h.getBoundingClientRect().top - panel.getBoundingClientRect().top - 12, behavior: "smooth" }); }}>{h.textContent}</button>)}
    </nav><div className="draft-long-document" ref={container} onScroll={() => {
      const top = container.current?.getBoundingClientRect().top || 0;
      const index = headings.reduce((last, h, i) => h.getBoundingClientRect().top <= top + 40 ? i : last, 0);
      setActive(Math.max(0, index));
    }}><MarkdownView content={content} /></div>
  </div>;
}
