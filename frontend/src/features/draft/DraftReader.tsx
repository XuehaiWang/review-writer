import { useEffect, useRef, useState, type ReactNode } from "react";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";

const noDirtyParagraphs: ReadonlySet<string> = new Set();

/** Match edited paragraphs to the nearest subsection and its parent chapter in document order. */
export function pendingChapterIndices(root: HTMLElement, dirtyParagraphKeys: ReadonlySet<string>) {
  const chapterIndices = new Set<number>();
  const headings = Array.from(root.querySelectorAll<HTMLElement>("h2,h3"));
  const headingIndex = new Map(headings.map((heading, index) => [heading, index]));
  let chapter = -1;
  let subsection = -1;
  for (const element of root.querySelectorAll<HTMLElement>("h2,h3,[data-paragraph-key]")) {
    if (element.tagName === "H2") { chapter = headingIndex.get(element) ?? -1; subsection = -1; }
    else if (element.tagName === "H3") subsection = headingIndex.get(element) ?? -1;
    else if (dirtyParagraphKeys.has(element.dataset.paragraphKey || "")) {
      if (chapter >= 0) chapterIndices.add(chapter);
      if (subsection >= 0) chapterIndices.add(subsection);
    }
  }
  return { headings, chapterIndices };
}

/** Render the original Markdown once; navigation never reconstructs manuscript content. */
export function DraftReader({ content, children, toolbarActions, onChapter, onOverview, discussOnSelect = false, dirtyParagraphKeys = noDirtyParagraphs }: { content: string; children?: ReactNode; toolbarActions?: ReactNode; onChapter?: (title: string) => void; onOverview?: () => void; discussOnSelect?: boolean; dirtyParagraphKeys?: ReadonlySet<string> }) {
  const { text } = useUiText();
  const container = useRef<HTMLDivElement>(null);
  const [headings, setHeadings] = useState<HTMLElement[]>([]);
  const [pendingChapters, setPendingChapters] = useState<Set<number>>(new Set());
  const [active, setActive] = useState(0);
  const [contentsOpen, setContentsOpen] = useState(true);
  useEffect(() => {
    if (!container.current) return;
    const { headings: currentHeadings, chapterIndices } = pendingChapterIndices(container.current, dirtyParagraphKeys);
    setHeadings(currentHeadings);
    setPendingChapters(chapterIndices);
  }, [content, dirtyParagraphKeys]);
  return <div className={`draft-review-split${contentsOpen ? "" : " draft-contents-collapsed"}`}>
    <div className="draft-reader-toolbar"><button type="button" className="button button-quiet" aria-expanded={contentsOpen} onClick={() => setContentsOpen(open => !open)}>{contentsOpen ? text("收起目录", "Hide contents") : text("展开目录", "Show contents")}</button>
      {children ? <small className="muted">{text("点击文字直接修改，修改后在对应位置保存。", "Click text to edit, then save at that location.")}</small> : null}
      {toolbarActions ? <div className="draft-reader-actions">{toolbarActions}</div> : null}
      {!contentsOpen && onChapter ? <button className="button button-primary" onClick={() => onChapter(headings[active]?.textContent || "")}>{text("讨论当前章节", "Discuss current chapter")}</button> : null}
    </div>
    <nav hidden={!contentsOpen} className="draft-item-list" aria-label={text("正文目录", "Manuscript contents")}>
      <div className="draft-contents-heading">{text("文章目录", "Contents")}<small>{text("选择章节定位正文", "Select a chapter to navigate")}</small></div>
      {headings.map((h, i) => <button type="button" key={i} className={`${active === i ? "selected" : ""}${pendingChapters.has(i) ? " draft-toc-pending" : ""}`}
        aria-label={pendingChapters.has(i) ? `${h.textContent || ""} · ${text("有未保存修改", "Unsaved changes")}` : h.textContent || ""}
        onClick={() => { setActive(i); const panel = container.current; if (panel) panel.scrollTo({ top: panel.scrollTop + h.getBoundingClientRect().top - panel.getBoundingClientRect().top - 12, behavior: "smooth" }); if (discussOnSelect) onChapter?.(h.textContent || ""); }}>{h.textContent}</button>)}
      {onChapter ? <button className="button button-primary draft-discuss-button" onClick={() => onChapter(headings[active]?.textContent || "")}>{text("讨论当前章节", "Discuss current chapter")}</button> : null}
    </nav><div className="draft-long-document" ref={container} onScroll={() => {
      const top = container.current?.getBoundingClientRect().top || 0;
      const index = headings.reduce((last, h, i) => h.getBoundingClientRect().top <= top + 40 ? i : last, 0);
      setActive(Math.max(0, index));
    }} onClick={event => {
      const target = event.target as HTMLElement;
      const heading = target.closest("h2,h3");
      if (heading && !target.closest('[contenteditable="true"]')) onChapter?.(heading.textContent || "");
      if (target instanceof HTMLImageElement && target.alt === "Overview figure") onOverview?.();
    }}>{children ?? <MarkdownView content={content} />}</div>
  </div>;
}
