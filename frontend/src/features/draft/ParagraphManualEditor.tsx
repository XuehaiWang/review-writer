import { useEffect } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";
import { useDraftScratch } from "./useDraftScratch";

type Props = {
  projectId: string; revision: number;
  paragraph: { paragraph_id: string; text: string; text_sha256?: string };
  disabled?: boolean; refresh: () => Promise<unknown>;
  scratchKey?: string; onEditingChange?: (editing: boolean) => void;
};

export function ParagraphManualEditor({ projectId, revision, paragraph, disabled, refresh, scratchKey, onEditingChange }: Props) {
  const { text } = useUiText();
  const [edit, setEdit, storageFailed] = useDraftScratch<{ text: string; hash: string; revision: number } | null>(scratchKey, null);
  useEffect(() => { onEditingChange?.(Boolean(edit)); }, [Boolean(edit), onEditingChange]);
  const save = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/draft/paragraphs/${encodeURIComponent(paragraph.paragraph_id)}`, {
      method: "PUT", ...jsonBody({ text: edit!.text, revision: edit!.revision, base_text_sha256: edit!.hash }),
    }),
    onSuccess: async () => { setEdit(null); await refresh(); },
  });
  if (!edit) return <button className="button button-secondary" disabled={disabled} onClick={() => {
    save.reset(); setEdit({ text: paragraph.text, hash: paragraph.text_sha256 || "", revision });
  }}>{text("手动编辑此段", "Edit this paragraph")}</button>;
  return <section>
    {storageFailed ? <p role="alert">{text("浏览器无法暂存，请先保存再离开页面。", "Browser storage is unavailable. Save before leaving.")}</p> : null}
    <textarea rows={9} aria-label={text("段落内容", "Paragraph text")} value={edit.text}
      disabled={save.isPending} onChange={event => setEdit({ ...edit, text: event.target.value })} />
    {edit.hash && edit.hash !== paragraph.text_sha256 ? <div role="alert"><p>{text("此段已有新版本，未保存文字已保留。请对照最新正文后再编辑。", "This paragraph changed. Your input is preserved; compare the latest text before editing again.")}</p><button className="button button-secondary" disabled={save.isPending} onClick={() => setEdit({ ...edit, hash: paragraph.text_sha256 || "", revision })}>{text("已核对，保留我的文字继续编辑", "Compared: keep my text and continue editing")}</button></div> : null}
    {save.error ? <p role="alert" className="message message-error">{save.error.message}</p> : null}
    <button className="button button-primary" disabled={disabled || save.isPending || !edit.text.trim()} onClick={() => save.mutate()}>{text("保存此段", "Save paragraph")}</button>
    <button className="button button-secondary" disabled={save.isPending} onClick={() => setEdit(null)}>{text("取消", "Cancel")}</button>
  </section>;
}
