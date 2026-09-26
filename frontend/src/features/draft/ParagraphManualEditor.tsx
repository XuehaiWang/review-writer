import { LocalizedError } from "../../components/LocalizedError";
import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";
import { useDraftScratch } from "./useDraftScratch";
import { InlineManuscriptText } from "./InlineManuscriptText";

type Props = {
  projectId: string; revision: number;
  paragraph: { paragraph_key: string; paragraph_id: string; text: string; text_sha256?: string };
  disabled?: boolean; refresh: () => Promise<unknown>;
  scratchKey?: string; onEditingChange?: (editing: boolean) => void;
  displayText?: string;
};

export function ParagraphManualEditor({ projectId, revision, paragraph, disabled, refresh, scratchKey, onEditingChange, displayText }: Props) {
  const { text } = useUiText();
  const [edit, setEdit, storageFailed] = useDraftScratch<{ text: string; hash: string; revision: number } | null>(scratchKey, null);
  const [initial, setInitial] = useState(edit ? { display: edit.text, canonical: edit.text } : undefined);
  const [epoch, setEpoch] = useState(0);
  const reset = () => { setEdit(null); setInitial(undefined); setEpoch(n => n + 1); };
  useEffect(() => { onEditingChange?.(Boolean(edit)); }, [Boolean(edit), onEditingChange]);
  useEffect(() => () => onEditingChange?.(false), []);
  useEffect(() => {
    if (!edit) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [Boolean(edit)]);
  const save = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/draft/paragraphs/${encodeURIComponent(paragraph.paragraph_id)}`, {
      method: "PUT", ...jsonBody({ text: edit!.text, revision: edit!.revision, base_text_sha256: edit!.hash }),
    }),
    onSuccess: async () => { await refresh(); reset(); },
  });
  return <section className="draft-inline-paragraph" data-paragraph-key={paragraph.paragraph_key}>
    <InlineManuscriptText key={epoch} value={initial?.display ?? displayText ?? paragraph.text}
      canonical={initial?.canonical ?? paragraph.text} label={text("段落内容", "Paragraph text") + ` · ${paragraph.paragraph_id}`}
      disabled={disabled || save.isPending} onChange={value => {
        if (!edit) setInitial({ display: displayText ?? paragraph.text, canonical: paragraph.text });
        save.reset(); setEdit({ text: value, hash: edit?.hash ?? paragraph.text_sha256 ?? "", revision: edit?.revision ?? revision });
      }} />
    {edit ? <>
    <small role="status">{text("有未保存修改", "Unsaved changes")}</small>
    {storageFailed ? <p role="alert">{text("浏览器无法暂存，请先保存再离开页面。", "Browser storage is unavailable. Save before leaving.")}</p> : null}
    {edit.hash && edit.hash !== paragraph.text_sha256 ? <div role="alert"><p>{text("此段已有新版本，未保存文字已保留。请对照最新正文后再编辑。", "This paragraph changed. Your input is preserved; compare the latest text before editing again.")}</p><button className="button button-secondary" disabled={save.isPending} onClick={() => setEdit({ ...edit, hash: paragraph.text_sha256 || "", revision })}>{text("已核对，保留我的文字继续编辑", "Compared: keep my text and continue editing")}</button></div> : null}
    {save.error ? <p role="alert" className="message message-error"><LocalizedError error={save.error} /></p> : null}
    {!edit.text.trim() ? <p className="muted">{text("删除仅移除此段正文，图片、图注及参考文献会保留，请检查它们的位置和引用。历史版本仍可恢复。", "Deletion removes only this paragraph. Figures, captions and references remain; check their placement and citations. Previous versions remain available.")}</p> : null}
    <button className="button button-primary" disabled={disabled || save.isPending} onClick={() => {
      if (!edit.text.trim() && !window.confirm(text("确定删除此段？关联图片和图注将保留。", "Delete this paragraph? Associated figures and captions will remain."))) return;
      save.mutate();
    }}>{edit.text.trim() ? text("保存此段", "Save paragraph") : text("删除此段", "Delete paragraph")}</button>
    <button className="button button-secondary" disabled={save.isPending} onClick={reset}>{text("撤销修改", "Undo changes")}</button>
    </> : null}
  </section>;
}
