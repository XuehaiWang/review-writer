import { useMutation } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";
import { useDraftScratch } from "./useDraftScratch";

export function DraftManuscriptFields({ userId, projectId, revision, fields, legacy, refresh }: {
  userId: string; projectId: string; revision: number; fields: { title: string; keywords: string[] }; refresh: () => Promise<unknown>;
  legacy?: { title: string; keywords: string[] } | null;
}) {
  const { text } = useUiText();
  const [edit, setEdit] = useDraftScratch<{ revision: number; title: string; keywords: string } | null>(`${userId}:${projectId}:manuscript-fields`, null);
  const title = edit?.title ?? fields.title;
  const keywords = edit?.keywords ?? fields.keywords.join(", ");
  const change = (values: Partial<{ title: string; keywords: string }>) => setEdit({ revision: edit?.revision ?? revision, title, keywords, ...values });
  const save = useMutation({ mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/draft/manuscript-fields`, {
    method: "PUT", ...jsonBody({ revision: edit?.revision ?? revision, title, keywords: keywords.split(/[,;，；]/).map(v => v.trim()).filter(Boolean) }),
  }), onSuccess: async () => { await refresh(); setEdit(null); } });
  return <details><summary>{text("文章标题与关键词", "Manuscript title and keywords")}</summary>
    {legacy && (legacy.title || legacy.keywords.length) ? <details><summary>{text("旧终稿字段（保留供选择，不自动覆盖）", "Legacy Final fields (not automatically adopted)")}</summary><p>{legacy.title}</p><p>{legacy.keywords.join(", ")}</p><button className="button button-quiet" disabled={Boolean(edit)} onClick={() => change({ title: legacy.title || title, keywords: legacy.keywords.join(", ") })}>{text("载入到编辑框", "Load into editor")}</button></details> : null}
    <label>{text("标题", "Title")}<input value={title} onChange={e => change({ title: e.target.value })} /></label>
    <label>{text("关键词（逗号分隔，可留空）", "Keywords (comma-separated, optional)")}<input value={keywords} onChange={e => change({ keywords: e.target.value })} /></label>
    <button className="button button-secondary" disabled={!title.trim() || save.isPending} onClick={() => save.mutate()}>{text("保存修改", "Save changes")}</button>
    {save.error ? <p role="alert">{save.error.message}</p> : null}
    {edit && edit.revision !== revision ? <p role="status">{text("正文版本已变化，编辑内容已保留。请复制需要的文字后重新载入当前字段。", "The manuscript changed; your edits are retained. Copy needed text before reloading current fields.")}<button className="button button-quiet" onClick={() => setEdit(null)}>{text("重新载入当前字段", "Reload current fields")}</button></p> : null}
  </details>;
}
