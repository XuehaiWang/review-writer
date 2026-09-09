import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels } from "../../utils/paperLabels";

type LiveFact = {
  fact_id: string;
  field_id: string;
  value: string;
  support_level: string;
};

type LivePaper = {
  paper_id: string;
  status: string;
  fact_count: number;
  pending_fact_count?: number;
  verified_fact_count?: number;
  classification_count: number;
  automatic_resolution_status: string;
  facts_preview: LiveFact[];
};

export type MatrixEnrichmentLive = {
  phase: string;
  current: number;
  total: number;
  current_paper_id: string;
  active_paper_ids: string[];
  target_axis_ids: string[];
  items: LivePaper[];
};

type MatrixPaperIdentity = {
  paper_id: string;
  title?: unknown;
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean) : [];
}

function number(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

/** Read the compact live payload, with a legacy checkpoint fallback for jobs
 * that were already running when the server was upgraded. */
export function readMatrixEnrichmentLive(job: Job): MatrixEnrichmentLive | null {
  const direct = record(job.result?.matrix_enrichment_live);
  if (direct) {
    const items = Array.isArray(direct.items) ? direct.items : [];
    return {
      phase: String(direct.phase || "extracting"),
      current: number(direct.current),
      total: number(direct.total),
      current_paper_id: String(direct.current_paper_id || ""),
      active_paper_ids: strings(direct.active_paper_ids),
      target_axis_ids: strings(direct.target_axis_ids),
      items: items.flatMap((raw) => {
        const item = record(raw);
        if (!item || !item.paper_id) return [];
        const previews = Array.isArray(item.facts_preview) ? item.facts_preview : [];
        return [{
          paper_id: String(item.paper_id),
          status: String(item.status || "complete"),
          fact_count: number(item.fact_count),
          pending_fact_count: number(item.pending_fact_count),
          verified_fact_count: number(item.verified_fact_count),
          classification_count: number(item.classification_count),
          automatic_resolution_status: String(item.automatic_resolution_status || ""),
          facts_preview: previews.flatMap((rawFact) => {
            const fact = record(rawFact);
            if (!fact) return [];
            return [{
              fact_id: String(fact.fact_id || ""),
              field_id: String(fact.field_id || "fact"),
              value: String(fact.value || ""),
              support_level: String(fact.support_level || ""),
            }];
          }),
        }];
      }),
    };
  }

  const progress = record(job.result?.matrix_enrichment_progress || job.result?.section_progress);
  const checkpoint = record(job.result?.matrix_enrichment_checkpoint || job.result?.section_checkpoint);
  if (!progress && !checkpoint) return null;
  const entries = record(checkpoint?.entries) || {};
  const completedIds = strings(progress?.completed_papers);
  const ids = Array.isArray(progress?.completed_papers) ? completedIds : Object.keys(entries);
  const items = ids.flatMap((paperId): LivePaper[] => {
    const entry = record(entries[paperId]);
    const result = record(entry?.result);
    if (!result) return [];
    const facts = Array.isArray(result.facts) ? result.facts : [];
    const tags = record(result.evidence_backed_tags) || {};
    return [{
      paper_id: paperId,
      status: String(result.status || "complete"),
      fact_count: facts.length,
      pending_fact_count: number(result.pending_fact_count),
      verified_fact_count: number(result.verified_fact_count),
      classification_count: Object.values(tags).reduce<number>((sum, value) => sum + (Array.isArray(value) ? value.length : 0), 0),
      automatic_resolution_status: String(record(result.automatic_resolution)?.status || ""),
      facts_preview: facts.slice(0, 3).flatMap((rawFact): LiveFact[] => {
        const fact = record(rawFact);
        return fact ? [{
          fact_id: String(fact.fact_id || ""),
          field_id: String(fact.field_id || "fact"),
          value: String(fact.value || ""),
          support_level: String(fact.support_level || ""),
        }] : [];
      }),
    }];
  });
  return {
    phase: String(progress?.phase || "extracting"),
    current: number(progress?.current ?? job.progress_current),
    total: number(progress?.total ?? job.progress_total),
    current_paper_id: String(progress?.current_paper_id || ""),
    active_paper_ids: strings(progress?.active_paper_ids),
    target_axis_ids: strings(progress?.target_axis_ids),
    items,
  };
}

function titleText(value: unknown): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  const valueRecord = record(value);
  return String(valueRecord?.value || valueRecord?.text || "");
}

export function MatrixLiveProgress({ job, papers }: { job: Job; papers: MatrixPaperIdentity[] }) {
  const { text } = useUiText();
  const live = readMatrixEnrichmentLive(job);
  const paperLabels = buildPaperDisplayLabels(papers);
  const papersById = new Map(papers.map((paper) => [paper.paper_id, paper]));
  const currentPaper = papersById.get(live?.current_paper_id || "");
  const activeIds = live?.active_paper_ids || [];
  const total = live?.total || job.progress_total || papers.length;
  const current = live?.current ?? job.progress_current;
  const percent = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  const phaseLabel = activeIds.length > 1
    ? text(`正在并行提取与核验 ${activeIds.length} 篇论文`, `Extracting and verifying ${activeIds.length} papers in parallel`)
    : ({
    extracting: text("提取与核对原文事实", "Extracting and checking source facts"),
    targeted_recheck: text("正在自动补证分类边界", "Automatically rechecking classification evidence"),
    routing_adjudication: text("正在核对论文分类", "Checking paper classification"),
    verifying: text("正在核验事实与原文的对应关系", "Verifying facts against their sources"),
    verified: text("事实核验已完成", "Fact verification completed"),
    retrieving: text("正在查找缺失事实的原文依据", "Retrieving source evidence for fact gaps"),
    supplementing: text("正在补充缺失事实", "Extracting missing facts"),
    restoring: text("正在恢复已有检查点", "Restoring an existing checkpoint"),
    finalizing: text("正在汇总并写入 Matrix", "Finalizing and publishing to the Matrix"),
  } as Record<string, string>)[live?.phase || "extracting"] || text("正在处理", "Processing");
  const items = [...(live?.items || [])].reverse();

  return (
    <section className="matrix-live-progress" aria-label={text("科学事实实时提取", "Live scientific fact extraction")}>
      <header>
        <div>
          <strong>{phaseLabel}</strong>
          <span>{activeIds.length > 1
            ? activeIds.map((paperId) => paperLabels.get(paperId) || paperId).join(" · ")
            : currentPaper
            ? `${paperLabels.get(currentPaper.paper_id) || currentPaper.paper_id} · ${titleText(currentPaper.title)}`
            : text("等待 Worker 开始处理第一篇论文", "Waiting for the worker to start the first paper")}</span>
        </div>
        <b>{current}/{total || "—"}</b>
      </header>
      <div className="matrix-live-progress-track" aria-hidden="true"><span style={{ width: `${percent}%` }} /></div>
      {live?.phase === "targeted_recheck" && live.target_axis_ids.length ? <p className="matrix-live-recheck">{text("自动补证维度", "Automatic evidence recheck")}: {live.target_axis_ids.map((axis) => axis.replaceAll("_", " ")).join(" · ")}</p> : null}
      {items.length ? <div className="matrix-live-results">
        {items.map((item) => {
          const paper = papersById.get(item.paper_id);
          return <article key={item.paper_id}>
            <div className="matrix-live-result-head">
              <strong>{paperLabels.get(item.paper_id) || item.paper_id}{paper ? ` · ${titleText(paper.title)}` : ""}</strong>
              <span className={item.status === "failed" ? "failed" : "complete"}>{item.pending_fact_count ? text("待核验", "Verification pending") : item.status === "failed" ? text("处理失败", "Processing failed") : text("处理完成", "Processing completed")}</span>
            </div>
            <small>{text(`已提取 ${item.fact_count} 条事实 · ${item.classification_count} 个正式分类`, `${item.fact_count} facts · ${item.classification_count} formal classifications`)}</small>
            {item.pending_fact_count ? <small>{text(`还有 ${item.pending_fact_count} 条事实待核验，继续处理会复用已完成的事实。`, `${item.pending_fact_count} facts await verification; continuing reuses completed work.`)}</small> : null}
            {item.facts_preview.length ? <ul>{item.facts_preview.map((fact, index) => <li key={fact.fact_id || `${item.paper_id}-${index}`}><b>{fact.field_id.replaceAll("_", " ")}</b><span>{fact.value}</span></li>)}</ul> : null}
          </article>;
        })}
      </div> : <p className="matrix-live-empty">{text("第一篇完成后，提取到的事实会逐篇显示在这里。", "Extracted facts will appear here as each paper completes.")}</p>}
    </section>
  );
}
