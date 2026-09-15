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
  if (fact?.processing && Object.values(fact.processing).some((status) => status === "pending" || status === "failed")) return "pending";
  if (fact && ["complete", "partial", "limited"].includes(fact.status || "")) {
    const limitedFacts = paper.scientific_facts?.some(item => {
      const value = item as { support_level?: string; verification?: { status?: string } } | null;
      return value?.support_level === "context_only" || ["uncertain", "rejected"].includes(value?.verification?.status || "");
    });
    if (limitedFacts || (fact.review_readiness && fact.review_readiness !== "complete")) return "limited";
  }
  return fact && ["complete", "partial", "limited"].includes(fact.status || "") ? "complete" : "pending";
}

export function analysisFailureReason(error: string): "source" | "retrieval" | "network" | "format" | "unknown" {
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

export function MatrixAnalysisStatus({ paper, papers, projectId, busy, refresh }: {
  paper?: Paper; papers: Paper[]; projectId: string; busy: boolean; refresh: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const retry = useMutation({
    mutationFn: (ids: string[]) => {
      const query = new URLSearchParams();
      // Default to recovery: reuse current facts and completed verification.
      ids.forEach((id) => query.append("paper_ids", id));
      return apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/matrix/enrichment/jobs?${query}`, {
        method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() },
      });
    },
    onSuccess: async () => { await refresh(); retry.reset(); },
  });
  const failed = papers.filter((item) => analysisState(item) === "failed");
  const error = paper?.fact_enrichment?.last_attempt?.error || paper?.fact_enrichment?.error || "";
  const reason = analysisFailureReason(error);
  const failedHere = paper && analysisState(paper) === "failed";
  const pending = paper && analysisState(paper) === "pending" && Boolean(paper.fact_enrichment?.processing);
  const retryable = failed.filter((item) => analysisFailureReason(item.fact_enrichment?.last_attempt?.error || item.fact_enrichment?.error || "") !== "source");
  if (!failed.length && !pending && !retry.isPending && !retry.error) return null;
  return <section className={`matrix-analysis-actions message ${failedHere ? "message-warning" : "message-info"}`} aria-live="polite">
    {failedHere ? <>
      <strong>{paper.fact_enrichment?.last_attempt?.status === "failed"
        ? text("本次更新失败，保留上次有效分析结果。", "Update failed; the previous valid analysis is retained.")
        : text("本篇论文科学事实分析未完成。", "Scientific fact analysis is incomplete for this paper.")}</strong>
      <p>{reason === "source" ? text("可读取的原文不足或缺失，请先在文献库检查原文解析，再重新分析。", "Readable source content is missing or insufficient. Check the source extraction in Library before retrying.")
        : reason === "retrieval" ? text("本次未检索到可用原文片段，不代表解析文件缺失，也不代表论文没有相关内容。可继续检索分析，或在写作时按具体问题补查。", "No usable passage was retrieved. This does not mean the source file or relevant findings are absent. Resume analysis or retrieve for specific writing questions.")
        : reason === "network" ? text("模型或网络暂时不可用，可重试分析。", "The model or network is temporarily unavailable. Retry the analysis.")
        : reason === "format" ? text("模型返回的分析结果未能正确读取，可重试分析。", "The model response could not be read. Retry the analysis.")
        : text("本次分析未能完成，可以重试；若持续失败，请联系管理员查看任务记录。", "Analysis did not complete. Retry, or contact the administrator if failures persist.")}</p>
      <p>{text("分析失败不代表论文没有科学证据，不会因此自动排除论文。", "Analysis failure does not mean the paper lacks scientific evidence and does not automatically exclude it.")}</p>
      {reason === "source" ? <a className="button button-secondary" href={`/library?project=${encodeURIComponent(projectId)}`}>{text("查看文献库原文", "Check Library sources")}</a>
        : <button className="button button-secondary" disabled={busy || retry.isPending} onClick={() => retry.mutate([paper.paper_id])}>{text("继续本篇未完成分析", "Resume this paper")}</button>}
      {error ? <details><summary>{text("技术详情", "Technical details")}</summary><p>{error}</p></details> : null}
    </> : pending && paper ? <><p>{text("已有结果会保留，可继续尚未完成的提取或核验。", "Existing results are retained; resume unfinished extraction or verification.")}</p><button className="button button-secondary" disabled={busy || retry.isPending} onClick={() => retry.mutate([paper.paper_id])}>{text("继续本篇未完成分析", "Resume this paper")}</button></> : null}
    {retryable.length > 0 ? <button className="button button-secondary" disabled={busy || retry.isPending} onClick={() => retry.mutate(retryable.map((item) => item.paper_id))}>
      {retry.isPending ? text("正在提交…", "Submitting…") : text(`重试失败项（${retryable.length}）`, `Retry failed papers (${retryable.length})`)}
    </button> : null}
    {retry.isPending ? <p role="status">{text("正在提交并更新任务状态…", "Submitting and updating task status…")}</p> : null}
    {retry.error ? <p role="alert">{text("重试请求未完成：", "Retry request failed: ")}{retry.error.message}</p> : null}
  </section>;
}
