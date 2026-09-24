import { LocalizedError } from "../../components/LocalizedError";
import { useMutation } from "@tanstack/react-query";
import { apiRequest, newIdempotencyKey } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";

type Paper = { paper_id: string; scientific_facts?: unknown[]; fact_enrichment?: {
  status?: string; extraction_status?: string; error?: string;
  review_readiness?: string;
  processing?: { extraction?: string; verification?: string; source_recovery?: string };
  fact_extraction_profile?: { stop_reason?: string };
  last_attempt?: { status?: string; error?: string };
} };

export function analysisFailed(paper: Paper): boolean {
  const fact = paper.fact_enrichment;
  return [fact?.last_attempt?.status, fact?.extraction_status, fact?.status].includes("failed");
}

export function analysisState(paper: Paper, active = false): "pending" | "running" | "complete" | "limited" | "failed" {
  if (active) return "running";
  const fact = paper.fact_enrichment;
  if (analysisFailed(paper)) return "failed";
  if (["provider_or_budget_unavailable", "retrieval_unavailable", "extraction_failed"].includes(fact?.fact_extraction_profile?.stop_reason || "")) return "failed";
  const terminalGap = ["no_new_evidence", "supplement_budget_reached", "review_ready_deferred_supplements"].includes(fact?.fact_extraction_profile?.stop_reason || "");
  if (terminalGap && !["pending", "failed"].includes(fact?.processing?.verification || "") && !["pending", "failed"].includes(fact?.processing?.extraction || "")) return "limited";
  if (fact?.processing && Object.values(fact.processing).some((status) => status === "pending" || status === "failed")) return "pending";
  if (fact && ["complete", "partial", "limited"].includes(fact.status || "")) {
    const limitedFacts = paper.scientific_facts?.some(item => {
      const value = item as { support_level?: string; verification?: { status?: string } } | null;
      return value?.support_level === "context_only" || ["uncertain", "rejected"].includes(value?.verification?.status || "");
    });
    if (fact.status === "limited" || limitedFacts || (fact.review_readiness && fact.review_readiness !== "complete")) return "limited";
  }
  return fact && ["complete", "partial", "limited"].includes(fact.status || "") ? "complete" : "pending";
}

export function analysisFailureReason(error: string): "source" | "configuration" | "retrieval" | "network" | "format" | "unknown" {
  if (/model.{0,120}(?:not available|not found)|api.?key|unauthorized|permission|insufficient.{0,20}(?:quota|credit)|401|403|模型.*(?:未.*开放|不存在)|(?:余额|额度).*(?:不足|耗尽)|授权.*(?:不可用|失效)/i.test(error)) return "configuration";
  if (/no .*evidence candidate|no source-addressable evidence/i.test(error)) return "retrieval";
  if (/missing.*(?:source|extraction)|source.*missing|build full-text indexes/i.test(error)) return "source";
  if (/timeout|timed out|unavailable|429|50[234]|connection|transport/i.test(error)) return "network";
  if (/json|decode|invalid.*format|parse.*response/i.test(error)) return "format";
  return "unknown";
}

export function MatrixEvidenceUse({ paper }: { paper: Paper }) {
  const { text } = useUiText();
  return <details className="matrix-evidence-use">
    <summary><strong>{text("证据用途", "Evidence use")}</strong><span>{paper.fact_enrichment?.review_readiness === "complete"
      ? text("可用于介绍本篇研究", "Supports an introduction to this study")
      : text("当前证据不足，按写作问题补查原文", "Current evidence is insufficient; retrieve sources for writing questions")}</span></summary>
    {analysisState(paper) === "limited" ? <p>{text("分析已完成，但部分记录不确定、被否决或尚不足以支撑论证。已有有效证据可继续使用，受限记录不会自动升级为可靠论据；无需仅为消除状态提示重复分析整篇。", "Analysis is complete, with evidence limitations. Valid evidence remains usable; uncertain or rejected records are not promoted. Do not rerun the entire paper just to clear this status.")}</p> : null}
    <p>{text("当前记录不代表全文覆盖情况。具体实验数据、机理和跨论文比较，需要对应原文支持；未提取到的字段不等于论文未报告。", "These records do not measure full-text coverage. Experimental details, mechanisms and cross-paper comparisons need matching source support; an unextracted field does not mean it was unreported.")}</p>
  </details>;
}

export function unfinishedAnalysis(papers: Paper[]) {
  const unfinished = papers.filter(p => ["pending", "failed"].includes(analysisState(p)));
  const blocked = unfinished.filter(p => ["source", "configuration"].includes(analysisFailureReason(p.fact_enrichment?.last_attempt?.error || p.fact_enrichment?.error || "")));
  return { unfinished, blocked, retryable: unfinished.filter(p => !blocked.includes(p)) };
}

export function MatrixAnalysisStatus({ paper, papers, projectId, busy, refresh, recoveryJobId }: {
  paper?: Paper; papers: Paper[]; projectId: string; busy: boolean; refresh: () => Promise<unknown>; recoveryJobId?: string;
}) {
  const { text } = useUiText();
  const { unfinished, blocked, retryable } = unfinishedAnalysis(papers);
  const retry = useMutation({
    mutationFn: () => {
      if (recoveryJobId) return apiRequest(`/api/v1/jobs/${encodeURIComponent(recoveryJobId)}/retry`, { method: "POST" });
      const query = new URLSearchParams();
      retryable.forEach(p => query.append("paper_ids", p.paper_id));
      return apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/matrix/enrichment/jobs?${query}`, {
        method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() },
      });
    },
    onSuccess: async () => { await refresh(); },
  });
  if (paper) {
    if (!["pending", "failed"].includes(analysisState(paper))) return null;
    const error = paper.fact_enrichment?.last_attempt?.error || paper.fact_enrichment?.error || "";
    return <details className="matrix-analysis-details"><summary>{text("分析详情", "Analysis details")}</summary>
      <p>{paper.fact_enrichment?.last_attempt?.status === "failed" ? text("本次更新未完成，上次有效结果已保留。", "The update did not finish; previous valid results are retained.") : text("可通过列表顶部继续未完成分析。已有结果会保留。", "Resume unfinished analysis above the paper list. Existing results are retained.")}</p>
      {error ? <p>{error}</p> : null}
    </details>;
  }
  const count = recoveryJobId ? Math.max(unfinished.length, 1) : retryable.length;
  return <section className="matrix-analysis-actions" aria-live="polite">
    <button type="button" className="button button-secondary" disabled={busy || retry.isPending || count === 0} onClick={() => retry.mutate()}>
      {busy ? text("分析进行中…", "Analysis in progress…") : retry.isPending ? text("正在提交…", "Submitting…") : text(`继续未完成分析（${count}）`, `Resume unfinished analysis (${count})`)}
    </button>
    {blocked.length && !recoveryJobId ? <p className="muted">{text(`另有 ${blocked.length} 篇需先恢复原文或服务配置，已有结果仍可使用。`, `${blocked.length} papers need source or service recovery first. Existing results remain usable.`)}</p> : null}
    {blocked.length ? <details><summary>{text("查看原因", "Details")}</summary>{blocked.map(p => <p key={p.paper_id}>{p.paper_id}: {p.fact_enrichment?.last_attempt?.error || p.fact_enrichment?.error}</p>)}<a href={`/library?project=${encodeURIComponent(projectId)}`}>{text("查看文献库原文", "Check Library sources")}</a></details> : null}
    {retry.isSuccess ? <p role="status">{text("已提交，系统将优先复用已有结果。", "Submitted. Existing results will be reused first.")}</p> : null}
    {retry.error ? <p role="alert">{text("未能继续分析：", "Could not resume analysis: ")}<LocalizedError error={retry.error} /></p> : null}
  </section>;
}
