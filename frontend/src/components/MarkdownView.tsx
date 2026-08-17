import type { ReactNode } from "react";
import { useUiText } from "../i18n/useUiText";

function inlineContent(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /!\[([^\]]*)\]\(([^)]+)\)|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) parts.push(text.slice(cursor, match.index));
    if (match[1] !== undefined) {
      const src = match[2].startsWith("/api/v1/artifacts/") ? match[2] : "";
      parts.push(src ? <img key={`${match.index}-img`} alt={match[1]} src={src} loading="lazy" /> : match[0]);
    } else if (match[3] !== undefined) {
      const href = match[4];
      const safeHref = /^(?:https?:\/\/|\/api\/v1\/)/i.test(href) ? href : "";
      parts.push(safeHref ? <a key={`${match.index}-link`} href={safeHref} target="_blank" rel="noreferrer">{match[3]}</a> : match[0]);
    } else if (match[5] !== undefined) {
      parts.push(<code key={`${match.index}-code`}>{match[5]}</code>);
    } else {
      parts.push(<strong key={`${match.index}-strong`}>{match[6]}</strong>);
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

export function MarkdownView({ content, empty }: { content?: string | null; empty?: string }) {
  const { text } = useUiText();
  const emptyMessage = empty || text("暂无内容。", "No content yet.");
  const source = String(content || "").replace(/<!--[\s\S]*?-->/g, "").trim();
  if (!source) return <div className="empty-state">{emptyMessage}</div>;
  const blocks = source.split(/\n{2,}/);
  return (
    <article className="markdown-view">
      {blocks.map((block, index) => {
        const value = block.trim();
        const heading = /^(#{1,3})\s+(.+)$/.exec(value);
        if (heading) {
          const children = inlineContent(heading[2]);
          if (heading[1].length === 1) return <h1 key={index}>{children}</h1>;
          if (heading[1].length === 2) return <h2 key={index}>{children}</h2>;
          return <h3 key={index}>{children}</h3>;
        }
        if (value.split("\n").every((line) => /^\s*[-*]\s+/.test(line))) {
          return <ul key={index}>{value.split("\n").map((line, itemIndex) => <li key={itemIndex}>{inlineContent(line.replace(/^\s*[-*]\s+/, ""))}</li>)}</ul>;
        }
        if (value.split("\n").every((line) => /^\s*\d+[.)]\s+/.test(line))) {
          return <ol key={index}>{value.split("\n").map((line, itemIndex) => <li key={itemIndex}>{inlineContent(line.replace(/^\s*\d+[.)]\s+/, ""))}</li>)}</ol>;
        }
        return <p key={index}>{value.split("\n").map((line, lineIndex) => <span key={lineIndex}>{lineIndex ? <br /> : null}{inlineContent(line)}</span>)}</p>;
      })}
    </article>
  );
}
