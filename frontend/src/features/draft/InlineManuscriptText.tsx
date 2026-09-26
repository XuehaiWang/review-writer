import { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MarkdownView } from "../../components/MarkdownView";

const protectedPattern = String.raw`\b(?:Figures?\s+|Figs?\.\s*)\d+[a-z]?(?:\s*(?:,|and|[-–—])\s*\d+[a-z]?)*(?!\w)|\[(?:\d+[\s,;–-]*)+\]`;

/** Serialize our own small Markdown renderer, never persist pasted HTML. */
export function manuscriptText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
  if (!(node instanceof HTMLElement)) return "";
  if (node.dataset.source !== undefined) return node.dataset.source;
  const value = Array.from(node.childNodes).map(manuscriptText).join("");
  if (node.classList.contains("inline-math")) return `$${node.title}$`;
  if (node.tagName === "BR") return "\n";
  if (node.tagName === "STRONG") return `**${value}**`;
  if (node.tagName === "CODE") return `\`${value}\``;
  if (node.tagName === "A") return `[${value}](${node.getAttribute("href")})`;
  if (node.tagName === "P") return value + "\n";
  if (node.tagName === "DIV" && !node.classList.contains("draft-inline-text")) return "\n" + value + "\n";
  return value;
}

export function InlineManuscriptText({ value, label, disabled, onChange, canonical = value }: {
  value: string; canonical?: string; label: string; disabled?: boolean; onChange: (value: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [renderSource] = useState(() => document.createElement("div"));
  // Only reset the DOM when the caller explicitly supplies new saved/undo text.
  useLayoutEffect(() => {
    const root = ref.current!;
    // React must not reconcile descendants that the browser edits in place.
    // This HTML comes exclusively from our escaping Markdown renderer.
    root.replaceChildren(...Array.from(renderSource.childNodes).map(node => node.cloneNode(true)));
    root.querySelectorAll<HTMLElement>(".inline-math").forEach(node => { node.contentEditable = "false"; });
    const originals = canonical.match(new RegExp(protectedPattern, "gi")) || [];
    let index = 0;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes: Text[] = [];
    while (walker.nextNode()) nodes.push(walker.currentNode as Text);
    for (const node of nodes) {
      if (node.parentElement?.closest(".inline-math,code,a")) continue;
      const parts = node.data.split(new RegExp(`(${protectedPattern})`, "gi"));
      if (parts.length < 2) continue;
      const fragment = document.createDocumentFragment();
      parts.forEach((part, i) => {
        if (i % 2) {
          const token = document.createElement("span");
          token.contentEditable = "false"; token.textContent = part;
          token.dataset.source = originals[index++] || part;
          fragment.append(token);
        } else fragment.append(document.createTextNode(part));
      });
      node.replaceWith(fragment);
    }
  }, [value, canonical, renderSource]);
  return <>{createPortal(<MarkdownView content={value} empty=" " />, renderSource)}<div ref={ref} className="draft-inline-text" role="textbox" aria-label={label}
    aria-multiline="true" contentEditable={!disabled} suppressContentEditableWarning
    onDrop={e => e.preventDefault()}
    onInput={e => onChange(manuscriptText(e.currentTarget).trim().replace(/\n\s*\n/g, "\n"))}
    onPaste={e => {
      e.preventDefault();
      const selection = window.getSelection();
      if (!selection?.rangeCount) return;
      const range = selection.getRangeAt(0);
      if (!e.currentTarget.contains(range.commonAncestorContainer)) return;
      range.deleteContents();
      const node = document.createTextNode(e.clipboardData.getData("text/plain"));
      range.insertNode(node); range.setStartAfter(node); range.collapse(true);
      selection.removeAllRanges(); selection.addRange(range);
      onChange(manuscriptText(e.currentTarget).trim().replace(/\n\s*\n/g, "\n"));
    }} /></>;
}
