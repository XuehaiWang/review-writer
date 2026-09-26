import { useMutation } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";
import { useDraftScratch } from "./useDraftScratch";
import { useEffect, useState, type ReactNode } from "react";
import { InlineManuscriptText } from "./InlineManuscriptText";

export function DraftManuscriptFields({ userId, projectId, revision, fields, legacy, refresh, onEditingChange, renderFields }: {
  userId: string; projectId: string; revision: number; fields: { title: string; keywords: string[] }; refresh: () => Promise<unknown>;
  legacy?: { title: string; keywords: string[] } | null;
  onEditingChange?: (dirty: boolean) => void;
  renderFields?: (title: ReactNode, keywords: ReactNode) => ReactNode;
}) {
  const { text } = useUiText();
  const [edit, setEdit] = useDraftScratch<{ revision: number; title: string; keywords: string; baseTitle?: string; baseKeywords?: string } | null>(`${userId}:${projectId}:manuscript-fields`, null);
  const title = edit?.title ?? fields.title;
  const keywords = edit?.keywords ?? fields.keywords.join(", ");
  const [display, setDisplay] = useState({ title, keywords });
  const [epoch, setEpoch] = useState(0);
  useEffect(() => { if (!edit) setDisplay({ title: fields.title, keywords: fields.keywords.join(", ") }); }, [fields.title, fields.keywords.join(", "), Boolean(edit)]);
  useEffect(() => { onEditingChange?.(Boolean(edit)); }, [Boolean(edit), onEditingChange]);
  const reset = () => { setEdit(null); setDisplay({ title: fields.title, keywords: fields.keywords.join(", ") }); setEpoch(n => n + 1); };
  const change = (values: Partial<{ title: string; keywords: string }>) => setEdit({ revision: edit?.revision ?? revision,
    baseTitle: edit?.baseTitle ?? fields.title, baseKeywords: edit?.baseKeywords ?? fields.keywords.join(", "), title, keywords, ...values });
  const conflict = Boolean(edit && edit.revision !== revision && (edit.baseTitle !== fields.title || edit.baseKeywords !== fields.keywords.join(", ")));
  const save = useMutation({ mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/draft/manuscript-fields`, {
    method: "PUT", ...jsonBody({ revision: conflict ? edit!.revision : revision, title, keywords: keywords.split(/[,;，；]/).map(v => v.trim()).filter(Boolean) }),
  }), onSuccess: async () => { await refresh(); setEdit(null); } });
  const titleEditor = <div className="draft-inline-title"><InlineManuscriptText key={`title-${epoch}`} label={text("标题", "Title")} value={display.title} disabled={save.isPending} onChange={title => change({ title })} /></div>;
  const keywordsEditor = <div className="draft-inline-keywords"><strong>{text("关键词", "Keywords")}</strong><InlineManuscriptText key={`keywords-${epoch}`} label={text("关键词（逗号分隔，可留空）", "Keywords (comma-separated, optional)")} value={display.keywords || " "} disabled={save.isPending} onChange={keywords => change({ keywords })} /></div>;
  const controls = <>
    {edit ? <div className="button-row"><small role="status">{text("有未保存修改", "Unsaved changes")}</small><button className="button button-primary" disabled={!title.trim() || save.isPending || conflict} onClick={() => save.mutate()}>{text("保存修改", "Save changes")}</button><button className="button button-secondary" disabled={save.isPending} onClick={reset}>{text("撤销修改", "Undo changes")}</button></div> : null}
    {save.error ? <p role="alert">{save.error.message}</p> : null}
    {conflict ? <p role="status">{text("标题或关键词已有新版本，编辑内容已保留。请复制需要的文字后重新载入。", "Title or keywords changed; your edits are retained. Copy needed text before reloading.")}<button className="button button-quiet" onClick={reset}>{text("重新载入当前字段", "Reload current fields")}</button></p> : null}
  </>;
  return <section className="draft-inline-fields">
    {legacy && (legacy.title || legacy.keywords.length) ? <details><summary>{text("旧终稿字段（保留供选择，不自动覆盖）", "Legacy Final fields (not automatically adopted)")}</summary><p>{legacy.title}</p><p>{legacy.keywords.join(", ")}</p><button className="button button-quiet" disabled={Boolean(edit)} onClick={() => { const loaded = { title: legacy.title || title, keywords: legacy.keywords.join(", ") }; change(loaded); setDisplay(loaded); }}>{text("载入到编辑框", "Load into editor")}</button></details> : null}
    {renderFields ? renderFields(<>{titleEditor}{edit?.title !== undefined && edit.title !== fields.title ? controls : null}</>, <>{keywordsEditor}{!edit || edit.title === fields.title ? controls : null}</>) : <>{titleEditor}{keywordsEditor}{controls}</>}
  </section>;
}
