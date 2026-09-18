import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiRequest } from "../../api/client";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";

export type FinalVersion = { artifact_id: string; created_at: string; operation: string; current: boolean; downloads?: { artifact_id: string; format: string }[] };
type Props = { projectId: string; markdown: string; versions: FinalVersion[] };

export function FinalManuscriptPreview(props: Props) {
  const { text } = useUiText();
  const [viewed, setViewed] = useState<{ id: string; markdown: string } | null>(null);
  const inspect = useMutation({ mutationFn: async (id: string) => ({ id, markdown: await apiRequest<string>("/api/v1/artifacts/" + id + "/content") }), onSuccess: setViewed });
  return <>
    <p>{text("终稿只读。正文请在初稿修改并确认，再同步到这里。历史版本仍可查看和下载。", "Final is read-only. Edit and approve in Draft, then sync here. Historical versions remain available.")}</p>
    <Link className="button button-secondary" to={"/draft?project=" + props.projectId}>{text("前往初稿修改", "Edit in Draft")}</Link>
    <MarkdownView content={props.markdown} empty={text("尚未生成最终稿。", "Final draft not generated yet.")} />
    {inspect.error ? <p role="alert">{inspect.error.message}</p> : null}
    <details className="advanced-panel"><summary>{text("终稿版本历史", "Final version history")}</summary>
      {props.versions.map(v => <article key={v.artifact_id}><span>{v.created_at}</span><button disabled={inspect.isPending} onClick={() => inspect.mutate(v.artifact_id)}>{text("查看版本", "View version")}</button><a href={"/api/v1/artifacts/" + v.artifact_id + "/content"}>{text("下载历史原稿", "Download saved manuscript")}</a>{v.downloads?.map(file => <a key={file.artifact_id} href={"/api/v1/artifacts/" + file.artifact_id + "/content"}>{text("下载 ", "Download ")}{file.format}</a>)}</article>)}
      {viewed ? <MarkdownView content={viewed.markdown} /> : null}
    </details>
  </>;
}
