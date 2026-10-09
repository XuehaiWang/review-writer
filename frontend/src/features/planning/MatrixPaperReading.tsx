import { useUiText } from "../../i18n/useUiText";
import { analysisState } from "./MatrixAnalysisStatus";
import { factDisplayGroup, readingCompleted, type MatrixFact, type PaperAnalysis, type ReadingPaper } from "./matrixReading";

type Paper = Omit<ReadingPaper, "scientific_facts"> & { paper_id: string; paper_analysis?: PaperAnalysis; scientific_facts?: MatrixFact[] };

export function MatrixReadingStatus({ paper }: { paper: Paper & { fact_enrichment?: ReadingPaper["fact_enrichment"] & {
  verified_fact_count?: number; pending_fact_count?: number;
} } }) {
  const { text } = useUiText();
  const enrichment = paper.fact_enrichment;
  if (!enrichment?.processing) return null;
  const read = readingCompleted(paper) && ["complete", "limited"].includes(analysisState(paper));
  return <p className="message message-info">
    {text(`已核验 ${enrichment.verified_fact_count || 0} 条 · 尚未核验 ${enrichment.pending_fact_count || 0} 条`, `${enrichment.verified_fact_count || 0} checked · ${enrichment.pending_fact_count || 0} not yet verified`)}
    {enrichment.processing.verification === "pending" ? read
      ? text("。初读已完成，未采用的发现不必逐条核验；实际使用时会核对对应原文。", ". First reading is complete. Findings are checked against sources when used, without reviewing every unused record.")
      : text("。已请求的核验尚未完成，继续处理会保留已有结果。", ". Requested verification is unfinished; resuming preserves completed results.") : null}
  </p>;
}

function sourceUrl(paperId: string, page?: number | null): string {
  return `/api/v1/library/papers/${encodeURIComponent(paperId)}/pdf${Number.isInteger(page) && Number(page) > 0 ? `#page=${page}` : ""}`;
}

export function MatrixPaperGuide({ paper }: { paper: Paper }) {
  const { text } = useUiText();
  const analysis = paper.paper_analysis;
  const fields = [
    [text("研究问题", "Research question"), analysis?.research_question],
    [text("主要贡献", "Main contribution"), analysis?.contribution],
    [text("与综述的关系", "Relevance to this review"), analysis?.topic_relation],
  ].filter(([, value]) => typeof value === "string" && value.trim());
  if (!fields.length) return null;
  return <section className="reading-field matrix-paper-guide" aria-label={text("论文导读", "Paper guide")}>
    <h3>{text("论文导读", "Paper guide")}</h3>
    {fields.map(([label, value]) => <div key={label}><strong>{label}</strong><p>{value}</p></div>)}
    <p className="muted">{text("导读用于理解论文与定位来源，不代表其中的科学关系已经核验。", "This guide helps explain the paper and locate sources; it does not certify its scientific claims.")}</p>
    <a href={sourceUrl(paper.paper_id)} target="_blank" rel="noopener noreferrer">{text("查看本篇原文", "View this paper's source")}</a>
  </section>;
}

export function MatrixFactRecords({ paper, emptyMessage }: { paper: Paper; emptyMessage: string }) {
  const { text } = useUiText();
  const groups = [
    { id: "verified", title: text("已核验依据", "Verified evidence"), description: "" },
    { id: "candidate", title: text("初读发现", "First-reading findings"), description: text("这些记录用于阅读导航，尚未作为可靠论据；实际采用时再核验对应关系。", "These records guide reading and are not yet verified evidence. Relevant relationships are checked when used.") },
    { id: "limited", title: text("受限记录", "Records with limitations"), description: text("这些记录尚不能作为可靠论据，请结合原文和核验结果使用。", "These records cannot currently support reliable claims; consult their sources and verification results.") },
    { id: "historical", title: text("历史来源记录", "Historical source records"), description: text("保留原有来源记录；未记录核验状态的内容不会显示为已核验。", "Existing source records are retained. Records without a verification status are not labeled verified.") },
  ] as const;
  const facts = paper.scientific_facts || [];
  if (!facts.length) return <p className="muted">{emptyMessage}</p>;
  return <>{groups.map(group => {
    const rows = facts.filter(fact => factDisplayGroup(fact, readingCompleted(paper)
      || paper.fact_enrichment?.fact_extraction_profile?.reading_strategy === "review-oriented/1") === group.id);
    if (!rows.length) return null;
    return <section key={group.id} className="matrix-fact-records" aria-label={group.title}>
      <h3>{group.title}</h3>
      {group.description ? <p className="muted">{group.description}</p> : null}
      <div className="matrix-fact-list">{rows.map((fact, index) => {
        const ref = fact.evidence_refs?.[0];
        const page = ref?.page_start;
        const sourceId = ref?.source_file_id || ref?.paper_id || paper.paper_id;
        const abstractOnly = fact.source_channel === "abstract" || fact.epistemic_status === "abstract_level_report" || fact.support_level === "abstract_limited";
        const status = fact.verification?.status;
        return <article key={fact.fact_id || `${fact.field_id}-${index}`}>
          <div><strong>{String(fact.field_id || "fact").replaceAll("_", " ")}</strong><span>
            {fact.revision_of_fact_id && group.id === "candidate" ? text("修订待核验", "Revision awaiting verification")
              : status === "rejected" ? text("核验未通过", "Not supported by verification")
              : status === "uncertain" ? text("核验仍不确定", "Verification inconclusive")
              : group.id === "candidate" ? text("尚未核验", "Not yet verified")
              : group.id === "verified" ? text("已核验", "Verified") : text("保留来源记录", "Source record retained")}
            {abstractOnly ? text(" · 仅摘要", " · Abstract only") : ""}
            {Number.isInteger(page) && Number(page) > 0 ? text(` · 第 ${page} 页`, ` · Page ${page}`) : ""}
          </span></div>
          <p>{fact.value}</p>
          <details><summary>{text("查看原文依据与证据上限", "View source support and evidence ceiling")}</summary>
            {fact.support_excerpt || ref?.support_excerpt ? <blockquote>{fact.support_excerpt || ref?.support_excerpt}</blockquote> : <p>{text("此记录未保存可展示的原文片段。", "No displayable source passage was saved with this record.")}</p>}
            <small>{fact.assertion_ceiling || fact.evidence_ceiling}</small>
            {!abstractOnly ? <p><a href={sourceUrl(sourceId, page)} target="_blank" rel="noopener noreferrer">{text("打开原始 PDF", "Open original PDF")}</a></p> : null}
          </details>
        </article>;
      })}</div>
    </section>;
  })}</>;
}
