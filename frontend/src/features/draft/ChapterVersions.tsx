import { uiLocale } from "../../i18n/locale";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../../api/client";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";
import { ParagraphManualEditor } from "./ParagraphManualEditor";
import type { DialogueSection } from "./SectionDialogue";
import { ParagraphDialogue, type DialogueCandidate, type DialogueParagraph } from "./ParagraphDialogue";

type Version = DialogueSection & { artifact_id: string; created_at: string };
type Versions = { current: Version; initial: Version | null; versions: { artifact_id: string; created_at: string }[] };

export function ChapterVersions({ section, projectId, userId, revision, disabled, candidates, turns, close, restart, refresh }: {
  section: DialogueSection; projectId: string; userId: string; revision: number; disabled: boolean;
  candidates: DialogueCandidate[]; turns: { id: string; message: string }[];
  close: () => void; restart: (artifactId: string) => void; refresh: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const dialog = useRef<HTMLDialogElement>(null);
  const [version, setVersion] = useState("current");
  const endpoint = `/api/v1/projects/${encodeURIComponent(projectId)}/draft/section-dialogues/${encodeURIComponent(section.section_id)}/versions`;
  const versions = useQuery({ queryKey: ["draft-dialogue", projectId, "versions", section.section_id], queryFn: () => apiRequest<Versions>(endpoint) });
  const historical = useQuery({ queryKey: ["draft-dialogue", projectId, "version", section.section_id, version],
    queryFn: () => apiRequest<Version>(`${endpoint}/${encodeURIComponent(version)}`), enabled: !["current", "initial", "history"].includes(version) });
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = dialog.current!;
    if (typeof node.showModal === "function") node.showModal(); else node.setAttribute("open", "");
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  const selected = version === "initial" ? versions.data?.initial : historical.data;
  return createPortal(<dialog ref={dialog} className="chapter-version-dialog" aria-label={text("章节正文与版本", "Chapter text and versions")}
    onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2>{section.title}</h2><button type="button" className="button button-quiet" onClick={close}>{text("关闭", "Close")}</button></header>
    <label>{text("查看版本", "View version")}<select value={version} onChange={event => setVersion(event.target.value)}>
      <option value="current">{text("当前正文", "Current text")}</option>
      <option value="initial">{text("初始版本", "Initial version")}</option>
      <option value="history">{text("对话与候选记录", "Conversation and candidate history")}</option>
      {(versions.data?.versions || []).filter(v => v.artifact_id !== versions.data?.current.artifact_id).map(v => <option key={v.artifact_id} value={v.artifact_id}>{text("历史版本", "Saved version")} · {new Date(v.created_at).toLocaleString(uiLocale())}</option>)}
    </select></label>
    <div className="chapter-version-content">
      {version === "current" ? section.paragraphs.map(p => <article key={p.paragraph_key}><MarkdownView content={p.text} />
        <ParagraphManualEditor scratchKey={`${userId}:${projectId}:${p.paragraph_key}:manual`} projectId={projectId} paragraph={p}
          revision={revision} disabled={disabled} refresh={refresh} /></article>) : version === "history" ? <>
        {turns.map(t => <details key={t.id}><summary>{t.message}</summary>{candidates.filter(c => (c as DialogueCandidate & { batch_job_id?: string }).batch_job_id === t.id).map(c => <article key={c.candidate_id}><MarkdownView content={c.reply || ""} /><MarkdownView content={c.candidate_text || c.original_text} /></article>)}</details>)}
        {candidates.filter(c => !turns.some(t => t.id === (c as DialogueCandidate & { batch_job_id?: string }).batch_job_id)).map(c => <details key={c.candidate_id}><summary>{c.paragraph_id}</summary><MarkdownView content={c.reply || ""} /><MarkdownView content={c.candidate_text || c.original_text} /></details>)}
        {section.paragraphs.map(p => <ParagraphHistory key={p.paragraph_key} projectId={projectId} paragraph={p} />)}
      </> : selected ? <MarkdownView content={selected.paragraphs.map(p => p.text).join("\n\n")} /> : <p>{versions.isPending || historical.isFetching ? text("正在加载…", "Loading…") : text("未能确认此章节的初始版本或历史内容，当前正文不会被当作初始版本。", "This chapter version could not be verified. Current text is not treated as its initial version.")}</p>}
      {versions.error || historical.error ? <p role="alert">{text("版本加载失败，请重新打开。", "Could not load versions. Please reopen this dialog.")}</p> : null}
    </div>
    {version === "initial" && versions.data?.initial ? <footer><p>{text("基于初始正文开启新讨论，保存修改前不会改变当前正文。", "Start a fresh discussion from the initial text. Current text stays unchanged until you save a revision.")}</p>
      <button type="button" className="button button-primary" disabled={disabled} onClick={() => restart(versions.data!.initial!.artifact_id)}>{text("基于此版本重新修改", "Revise from this version")}</button></footer> : null}
  </dialog>, document.body);
}

function ParagraphHistory({ projectId, paragraph }: { projectId: string; paragraph: DialogueParagraph }) {
  const [open, setOpen] = useState(false);
  const { text } = useUiText();
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>{text("旧段落对话", "Previous paragraph conversation")} · {paragraph.paragraph_id}</summary>
    {open ? <ParagraphDialogue projectId={projectId} paragraph={paragraph} /> : null}
  </details>;
}
