import { useUiText } from "../../i18n/useUiText";

export type ContentDiagnostics = {
  generation_mode?: string;
  depth_diagnostics?: { sufficient?: boolean };
  narrative_diagnostics?: { status?: string };
  validations?: Array<{ omitted?: Array<{ reason?: string }> }>;
};

export function SectionContentNotice({ section }: { section?: ContentDiagnostics }) {
  const { text } = useUiText();
  if (!section) return null;
  const omitted = (section.validations || []).flatMap((row) => row.omitted || []);
  const binding = omitted.filter((row) => row.reason === "missing_or_invalid_source_span").length;
  const other = omitted.length - binding;
  const shallow = section.depth_diagnostics?.sufficient === false || section.narrative_diagnostics?.status === "shallow";
  const limited = ["limited_evidence", "pending_evidence"].includes(section.generation_mode || "");
  if (!binding && !other && !shallow && !limited) return null;
  return <div className="message message-warning" role="status">
    <strong>{text(limited || omitted.length ? "章节已生成，部分内容存在限制。" : "章节已生成。", limited || omitted.length ? "Section generated with content limitations." : "Section generated.")}</strong>
    <details><summary>{text("查看生成记录", "Generation details")}</summary>
    {binding ? <p>{text(`${binding} 条论述因来源绑定失败未纳入正文，不等于论文没有相应证据。`, `${binding} claims were omitted because source binding failed; this does not establish missing evidence in the papers.`)}</p> : null}
    {other ? <p>{text(`${other} 条论述因来源支持或内容检查问题被省略。`, `${other} claims were omitted after source-support or content checks.`)}</p> : null}
    {shallow ? <p>{text("篇幅或讨论结构与规划建议有差异，此项不代表事实错误。", "Length or discussion structure differs from planning guidance; this does not establish a factual error.")}</p> : null}
    {limited && !omitted.length ? <p>{text("部分讨论的证据或文献覆盖仍待补充。", "Some discussions still need evidence or paper coverage.")}</p> : null}
    <p>{text("以上为内容提示，不影响确认或继续后续操作。", "These content notices do not prevent confirmation or continuation.")}</p>
    </details>
  </div>;
}
