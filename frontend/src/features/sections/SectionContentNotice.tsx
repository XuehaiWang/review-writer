import { useUiText } from "../../i18n/useUiText";

export type ContentDiagnostics = {
  generation_mode?: string;
  paper_presentation?: Array<{ paper_id: string; requested: string; actual: string; table_fallback?: boolean }>;
  depth_diagnostics?: { sufficient?: boolean };
  narrative_diagnostics?: { status?: string; review_status?: string; issues?: string[] };
  primary_papers?: string[];
  paragraphs?: Array<{ cited_paper_ids?: string[]; evidence?: Array<{ paper_id?: string }> }>;
  validations?: Array<{ rule_id?: string; status?: string; target_id?: string; omitted?: Array<{ reason?: string }>; unresolved?: unknown[] }>;
};

export function SectionContentNotice({ section }: { section?: ContentDiagnostics }) {
  const { text } = useUiText();
  if (!section) return null;
  const omitted = (section.validations || []).flatMap((row) => row.omitted || []);
  const binding = omitted.filter((row) => row.reason === "missing_or_invalid_source_span").length;
  const other = omitted.length - binding;
  const shallow = section.depth_diagnostics?.sufficient === false || section.narrative_diagnostics?.status === "shallow";
  const limited = ["limited_evidence", "pending_evidence"].includes(section.generation_mode || "");
  const needsPolish = section.narrative_diagnostics?.review_status === "needs_revision";
  const covered = new Set((section.paragraphs || []).flatMap(p => [
    ...(p.cited_paper_ids || []), ...(p.evidence || []).map(e => e.paper_id).filter((id): id is string => Boolean(id)),
  ]));
  const missing = section.paragraphs && section.primary_papers
    ? [...new Set(section.primary_papers)].filter(id => !covered.has(id)).length : 0;
  const unresolved = (section.validations || []).reduce((n, row) => n + (row.unresolved?.length || 0), 0);
  const pending = section.generation_mode === "pending_evidence";
  const unknownCoverage = limited && !omitted.length && !pending && !missing && !unresolved
    && (!section.paragraphs || !section.primary_papers);
  const evidenceGap = pending || missing > 0 || unresolved > 0 || unknownCoverage;
  const tableFallback = (section.paper_presentation || []).filter((paper) => paper.table_fallback);
  if (!binding && !other && !shallow && !evidenceGap && !needsPolish && !tableFallback.length) return null;
  return <div className={`message${evidenceGap ? " message-warning" : ""}`} role="status">
    <strong>{pending ? text("本章尚缺可用于成文的证据。", "This section still needs evidence for prose.")
      : evidenceGap ? text("章节已生成，仍有证据覆盖待核对。", "Section generated; evidence coverage needs checking.")
        : text("章节已生成。", "Section generated.")}</strong>
    {missing > 0 ? <p>{text(`${missing} 篇主要论文尚未在正文中引用；建议核对是否遗漏关键工作。`, `${missing} primary papers are not cited in the prose; check for missing key studies.`)}</p> : null}
    {unresolved > 0 ? <p>{text(`${unresolved} 项来源支持仍未确认，建议查看证据记录。`, `${unresolved} source-support items remain unresolved; review the evidence records.`)}</p> : null}
    {pending ? <p>{text("可先继续其他章节，再补充本章资料。", "Continue with other sections and add evidence for this one later.")}</p> : null}
    {unknownCoverage ? <p>{text("历史记录未区分自动处理与证据缺口，请结合正文核对文献覆盖。", "This older record does not distinguish automatic handling from evidence gaps; check coverage against the prose.")}</p> : null}
    {needsPolish ? <p>{text("建议优化表达与论证组织，可在初稿中修改；不表示整章不可用。", "Consider refining expression and argument structure in Draft; this does not make the section unusable.")}</p> : null}
    {omitted.length > 0 ? <p>{text(`已自动排除 ${omitted.length} 条未通过核验的表述；记录不等于当前正文仍有这些错误。`, `${omitted.length} statements that did not pass checks were excluded automatically; these records are not errors remaining in the prose.`)}</p> : null}
    <details><summary>{text("查看生成记录", "Generation details")}</summary>
    {tableFallback.length ? <p>{text("部分论文暂未形成可导出的比较表记录；已有正文引用仍保留，不视为表格覆盖完成。", "Some papers do not yet have exportable comparison rows; existing prose citations are retained, without claiming table coverage.")}</p> : null}
    {binding ? <p>{text(`${binding} 条论述因来源绑定失败未纳入正文，不等于论文没有相应证据。`, `${binding} claims were omitted because source binding failed; this does not establish missing evidence in the papers.`)}</p> : null}
    {other ? <p>{text(`${other} 条论述因来源支持或内容检查问题被省略。`, `${other} claims were omitted after source-support or content checks.`)}</p> : null}
    {shallow ? <p>{text("篇幅或讨论结构与规划建议有差异，此项不代表事实错误。", "Length or discussion structure differs from planning guidance; this does not establish a factual error.")}</p> : null}
    {section.narrative_diagnostics?.issues?.length ? <>
      <p>{text("模型审阅记录（可能包含已处理项，需结合当前正文判断）：", "Model review notes (may include resolved items; compare with the current prose):")}</p>
      <ul>{section.narrative_diagnostics.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>
    </> : null}
    <p>{text("以上为内容提示，不影响确认或继续后续操作。", "These content notices do not prevent confirmation or continuation.")}</p>
    </details>
  </div>;
}
