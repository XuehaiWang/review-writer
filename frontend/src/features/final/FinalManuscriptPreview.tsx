import { uiLocale } from "../../i18n/locale";
import { LocalizedError } from "../../components/LocalizedError";
import { useEffect, useRef, useState } from "react";
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
  const [focused, setFocused] = useState(false);
  const [headings, setHeadings] = useState<{ title: string; level: string }[]>([]);
  const document = useRef<HTMLDivElement>(null);
  const markdown = viewed?.markdown ?? props.markdown;
  useEffect(() => {
    setHeadings(Array.from(document.current?.querySelectorAll("h2, h3") || []).map(h => ({ title: h.textContent || "", level: h.tagName })));
  }, [markdown]);
  const jumpTo = (index: number) => {
    const heading = document.current?.querySelectorAll<HTMLElement>("h2, h3")[index];
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); heading.scrollIntoView({ block: "start" }); }
  };
  const inspect = useMutation({ mutationFn: async (id: string) => ({ id, markdown: await apiRequest<string>("/api/v1/artifacts/" + id + "/content") }), onSuccess: setViewed });
  return <div className={`final-reader${focused ? " is-focused" : ""}`}>
    <header className="final-reader-toolbar">
      <div><span className="step-label">{text("稿件阅读", "MANUSCRIPT")}</span><strong>{viewed ? text("历史版本 · 只读", "Historical version · Read-only") : text("当前终稿 · 只读", "Current final · Read-only")}</strong><p>{text("此处为阅读预览，实际出版版式以导出文件为准。", "Reading preview; exported files determine publication layout.")}</p></div>
      <div className="button-row"><button type="button" className="button button-secondary" aria-pressed={focused} onClick={() => setFocused(!focused)}>{focused ? text("退出专注阅读", "Exit focused reading") : text("专注阅读", "Focused reading")}</button>
      <Link className="button button-quiet" to={"/draft?project=" + encodeURIComponent(props.projectId)}>{text("前往初稿修改", "Edit in Draft")}</Link></div>
    </header>
    {viewed && <div className="final-reader-version-note" role="status"><span>{text("正在查看历史稿，不会替换当前终稿或改变导出内容。", "Viewing history does not replace the current final or change exports.")}</span><button type="button" className="button button-secondary" onClick={() => setViewed(null)}>{text("返回当前终稿", "Return to current final")}</button></div>}
    {headings.length > 0 && <details className="final-reader-contents"><summary>{text("章节目录", "Contents")} · {headings.length}</summary><nav aria-label={text("稿件章节目录", "Manuscript contents")}>{headings.map((h, index) => <button type="button" key={index} className={h.level === "H3" ? "is-subheading" : ""} onClick={() => jumpTo(index)}>{h.title}</button>)}</nav></details>}
    <div className="final-reader-paper" ref={document}><MarkdownView content={markdown} empty={text("尚未生成最终稿。", "Final draft not generated yet.")} /></div>
    {inspect.error ? <p role="alert"><LocalizedError error={inspect.error} /></p> : null}
    <details className="final-reader-history"><summary>{text("终稿版本历史", "Final version history")} · {props.versions.length}</summary>
      {props.versions.map(v => <article key={v.artifact_id}><span>{Number.isNaN(Date.parse(v.created_at)) ? v.created_at : new Date(v.created_at).toLocaleString(uiLocale())}{v.current ? text(" · 当前版本", " · Current version") : ""}</span><div className="button-row"><button type="button" className="button button-secondary" disabled={inspect.isPending} onClick={() => v.current ? setViewed(null) : inspect.mutate(v.artifact_id)}>{text("查看版本", "View version")}</button><a className="button button-quiet" href={"/api/v1/artifacts/" + v.artifact_id + "/content"}>{text("下载历史原稿", "Download saved manuscript")}</a>{v.downloads?.map(file => <a className="button button-quiet" key={file.artifact_id} href={"/api/v1/artifacts/" + file.artifact_id + "/content"}>{text("下载 ", "Download ")}{file.format}</a>)}</div></article>)}
    </details>
  </div>;
}
