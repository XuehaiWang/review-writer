import { Fragment, type ReactNode } from "react";
import { MarkdownView } from "../../components/MarkdownView";
import { ParagraphManualEditor } from "./ParagraphManualEditor";
import type { DialogueParagraph } from "./ParagraphDialogue";

/** Paragraph markers, not duplicate prose or heading text, identify save targets. */
export function manuscriptSegments(content: string, paragraphs: DialogueParagraph[]) {
  const known = new Map(paragraphs.map(p => [p.paragraph_id, p]));
  const segments: { text: string; paragraph?: DialogueParagraph }[] = [];
  let cursor = 0;
  for (const marker of content.matchAll(/<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->/g)) {
    const paragraph = known.get(marker[1]);
    if (!paragraph) continue;
    const end = content.slice(0, marker.index).trimEnd().length;
    const start = content.lastIndexOf("\n\n", end - 1) + 2;
    if (start < cursor || end <= start) continue;
    segments.push({ text: content.slice(cursor, start) });
    segments.push({ text: content.slice(start, end), paragraph });
    cursor = marker.index! + marker[0].length;
  }
  segments.push({ text: content.slice(cursor) });
  return segments;
}

const KEYWORDS_SLOT = "<!-- editable_keywords -->";
export function placeKeywordsAfterAbstract(content: string) {
  const clean = content.replace(/^\*\*(?:Keywords|关键词)[:：]\*\*[^\n]*\n?/gim, "");
  const abstract = /^##\s+(?:Abstract|摘要)\s*$/im.exec(clean);
  const start = abstract ? abstract.index + abstract[0].length : 0;
  const boundary = /^(?:#{1,2}\s+|!\[Overview figure\])/m.exec(clean.slice(start));
  const at = boundary ? start + boundary.index : clean.length;
  return clean.slice(0, at).trimEnd() + "\n\n" + KEYWORDS_SLOT + "\n\n" + clean.slice(at);
}

export function EditableManuscript({ content, keywords, paragraphs, userId, projectId, revision, disabled, refresh, onEditingChange }: {
  content: string; paragraphs: DialogueParagraph[]; userId: string; projectId: string; revision: number;
  keywords?: ReactNode;
  disabled?: boolean; refresh: () => Promise<unknown>; onEditingChange: (key: string, dirty: boolean) => void;
}) {
  return <div className="draft-editable-manuscript">{manuscriptSegments(keywords ? placeKeywordsAfterAbstract(content) : content, paragraphs).map((part, i) =>
    part.paragraph ? <ParagraphManualEditor key={part.paragraph.paragraph_key} paragraph={part.paragraph}
      displayText={part.text} projectId={projectId} revision={revision} disabled={disabled} refresh={refresh}
      scratchKey={`${userId}:${projectId}:${part.paragraph.paragraph_key}:manual`}
      onEditingChange={dirty => onEditingChange(part.paragraph!.paragraph_key, dirty)} /> :
      <Fragment key={`static-${i}`}>{part.text.split(KEYWORDS_SLOT).map((chunk, index) => <Fragment key={index}>
        {index > 0 ? keywords : null}
        {chunk.replace(/<!--[\s\S]*?-->/g, "").trim() ? <MarkdownView content={chunk} /> : null}
      </Fragment>)}</Fragment>)}</div>;
}
