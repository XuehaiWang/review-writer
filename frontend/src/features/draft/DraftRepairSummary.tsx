import { useUiText } from "../../i18n/useUiText";

export type RepairFinding = { repair_class?: string; resolution_state?: string };
export type RevisionFailure = { paragraph_id?: string; paragraph_ids?: string[]; reason?: string; reasons?: string[] };
export type Adjustment = { section_id: string; paragraph_ids: string[]; claim_ids: string[]; proposed_action: string; claims?: { claim_id: string; original_text: string }[] };
export type SourceCheck = { entries?: { paragraph_id?: string; source_check_status?: string;
  targeted_source_recheck?: { status?: string }; unsupported_claims?: string[]; missing_core_claim_ids?: string[];
  source_evidence_refs?: string[]; papers?: { paper_id?: string; passages?: { ref?: string; page?: number; text?: string }[] }[] }[] };

// Render only the current published Quality, never the pending candidate report.
export function SavedSourceChecks({ report }: { report?: SourceCheck }) {
  const { text } = useUiText();
  const rows = report?.entries?.filter(row => row.source_check_status === "verified"
    && row.targeted_source_recheck?.status === "supported_without_prose_change"
    && !row.unsupported_claims?.length && !row.missing_core_claim_ids?.length) || [];
  if (!rows.length) return null;
  return <details className="advanced-panel"><summary>{text(`已保存补证关联（${rows.length} 段，正文未改动）`, `Saved source links (${rows.length} paragraphs, prose unchanged)`)}</summary>
    <p>{text("已补查并核验原文支持，不需要为这些段落重复接受改写。正文候选仍需单独确认。", "Local passages were checked and saved as support. Prose candidates still require separate confirmation.")}</p>
    {rows.map(row => <section key={row.paragraph_id}><strong>{row.paragraph_id}</strong>
      {row.papers?.flatMap(paper => (paper.passages || []).filter(passage => row.source_evidence_refs?.includes(passage.ref || ""))
        .map(passage => <blockquote key={`${paper.paper_id}-${passage.ref}`}><small>{paper.paper_id} · {passage.ref}</small><p>{passage.text}</p></blockquote>))}
    </section>)}
  </details>;
}

const labels: Record<string, [string, string]> = {
  draft_rewrite: ["可直接优化", "Ready to rewrite"],
  claim_narrowing: ["收窄无依据的细节", "Narrow unsupported details"],
  evidence_rescue_then_rewrite: ["先补查原文", "Recover local evidence first"],
  planning_adjustment: ["待确认论点调整", "Review argument adjustment"],
  human_confirmation: ["待核实科学冲突", "Review source conflict"],
  stage_repair: ["交由对应阶段处理", "Repair in the owning stage"],
  advisory: ["可选建议", "Optional advice"],
};

export function RepairLabel({ issue }: { issue: RepairFinding }) {
  const { text } = useUiText();
  const label = labels[issue.repair_class || ""];
  return label ? <span className="job-pill">{issue.resolution_state === "provider_deferred"
    ? text("服务暂缓，可重试", "Provider deferred; retry available") : text(...label)}</span> : null;
}

export function RevisionFailures({ items }: { items?: RevisionFailure[] }) {
  const { text } = useUiText();
  if (!items?.length) return null;
  return <details className="advanced-panel"><summary>{text(`本次未修改的段落及原因（${items.length}）`, `Unchanged paragraphs and reasons (${items.length})`)}</summary>
    <ul>{items.map((item, index) => <li key={index}><strong>{(item.paragraph_ids || [item.paragraph_id]).filter(Boolean).join(", ")}</strong>
      <p>{item.reason || item.reasons?.join(", ") || text("未得到可安全应用的修改。", "No safely applicable revision was found.")}</p></li>)}</ul>
  </details>;
}

export function DraftRepairSummary({ issues, adjustments, onRevise, disabled }: {
  issues: RepairFinding[]; adjustments: Adjustment[]; onRevise: () => void; disabled: boolean;
}) {
  const { text } = useUiText();
  const counts = issues.reduce<Record<string, number>>((out, issue) => {
    if (issue.repair_class) out[issue.repair_class] = (out[issue.repair_class] || 0) + 1;
    return out;
  }, {});
  if (!Object.keys(counts).length) return null;
  return <section className="draft-repair-summary" aria-label={text("问题处理方式", "Finding actions")}>
    <div className="quality-score-grid">{Object.entries(counts).map(([kind, count]) => <article key={kind}>
      <RepairLabel issue={{ repair_class: kind }} /><strong>{count}</strong>
    </article>)}</div>
    {adjustments.length ? <details className="advanced-panel" open><summary>{text("论点调整建议", "Proposed argument adjustments")}</summary>
      <p>{text("在当前初稿中检查这些论点及相关段落，生成论点与正文联合候选。确认前不改正文，也不重新生成上游大纲或章节。", "Review these arguments and dependent paragraphs in the current Draft. Joint argument/body candidates leave the manuscript and upstream plan unchanged until confirmation.")}</p>
      <ul>{adjustments.map(row => <li key={row.section_id}><strong>{row.section_id}</strong> · {row.paragraph_ids.join(", ")}
        {row.claims?.filter(claim => claim.original_text).map(claim => <blockquote key={claim.claim_id}>{claim.original_text}</blockquote>)}
        <p>{text("保留本节的科学问题，根据已有证据收窄结论；无法回答的部分保留为待研究问题。", "Preserve the research question and narrow the conclusion to available evidence; keep unanswered parts as open questions.")}</p>
        {row.claim_ids.length ? <small>{text("涉及论点：", "Affected claims: ")}{row.claim_ids.join(", ")}</small> : null}
      </li>)}</ul>
      <button className="button button-secondary" type="button" disabled={disabled} onClick={onRevise}>
        {text("在初稿中生成联合修订候选", "Generate joint revision in Draft")}
      </button>
    </details> : null}
  </section>;
}
