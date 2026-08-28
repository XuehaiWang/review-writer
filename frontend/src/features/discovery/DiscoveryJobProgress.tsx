import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";

type DiscoveryJobProgressProps = {
  job?: Job;
  submitting?: boolean;
};

const activeStatuses = new Set(["queued", "running", "cancel_requested"]);
const sourceOrder = ["crossref", "openalex", "semantic_scholar", "arxiv"];

type SourceState = { status?: string; count?: number; error?: string; errors?: string[] };
type ScreeningState = {
  current: number;
  total: number;
  cached: number;
  running: number;
  concurrency: number;
};

function screeningState(result: Record<string, unknown> | undefined): ScreeningState | undefined {
  const progress = result?.source_progress;
  if (!progress || typeof progress !== "object") return undefined;
  const value = progress as Record<string, unknown>;
  if (value.stage !== "paper_screening") return undefined;
  const total = Math.max(0, Number(value.total || 0));
  if (!total) return undefined;
  return {
    current: Math.max(0, Math.min(Number(value.current || 0), total)),
    total,
    cached: Math.max(0, Number(value.cached || 0)),
    running: Math.max(0, Number(value.running || 0)),
    concurrency: Math.max(1, Number(value.concurrency || 1)),
  };
}

function sourceStates(result: Record<string, unknown> | undefined): Record<string, SourceState> {
  const progress = result?.source_progress;
  const external = result?.external_search;
  const value = progress && typeof progress === "object"
    ? (progress as Record<string, unknown>).sources
    : external && typeof external === "object"
      ? (external as Record<string, unknown>).source_statuses
      : undefined;
  return value && typeof value === "object" ? value as Record<string, SourceState> : {};
}

function sourceStatusLabel(status: string, text: (zh: string, en: string) => string): string {
  if (status === "disabled") return text("未启用", "Disabled");
  if (status === "completed") return text("已完成", "Completed");
  if (status === "partial") return text("部分完成", "Partial");
  if (status === "failed") return text("失败", "Failed");
  if (status === "running") return text("检索中", "Searching");
  return text("等待中", "Queued");
}

export function DiscoveryJobProgress({ job, submitting = false }: DiscoveryJobProgressProps) {
  const { text } = useUiText();
  const status = job?.status || (submitting ? "submitting" : "queued");
  const screening = screeningState(job?.result);
  const useScreeningProgress = status === "running" && Boolean(screening);
  const total = useScreeningProgress
    ? screening!.total
    : Math.max(0, Number(job?.progress_total || 0));
  const current = useScreeningProgress
    ? screening!.current
    : Math.max(0, Math.min(Number(job?.progress_current || 0), total || Number.MAX_SAFE_INTEGER));
  const determinate = total > 0;
  const active = status === "submitting" || activeStatuses.has(status);
  const percentage = determinate
    ? Math.max(0, Math.min(100, Math.round((current / total) * 100)))
    : status === "succeeded" ? 100 : undefined;

  let title = text("正在提交检索任务", "Submitting search job");
  let detail = text("正在准备当前项目的检索请求。", "Preparing the project discovery request.");
  if (status === "queued") {
    title = text("检索任务正在排队", "Search job queued");
    detail = text("任务已提交，正在等待工作线程。", "The search is waiting for a worker.");
  } else if (status === "running") {
    const persistedStage = job?.result?.source_progress && typeof job.result.source_progress === "object"
      ? String((job.result.source_progress as Record<string, unknown>).stage || "")
      : "";
    if (screening) {
      title = text("正在进行论文初步证据分类", "Screening candidate-paper evidence");
      detail = text(
        `已完成 ${screening.current}/${screening.total} 篇，复用缓存 ${screening.cached} 篇；最多 ${screening.concurrency} 篇并行。`,
        `Completed ${screening.current}/${screening.total}; reused ${screening.cached} cached classifications with up to ${screening.concurrency} concurrent papers.`,
      );
    } else if (persistedStage === "query_planning") {
      title = text("正在生成查询计划", "Building the query plan");
      detail = text("正在将综述主题压缩为检索词、筛选条件和分类维度。", "Converting the review topic into search terms, filters, and classification axes.");
    } else if (persistedStage === "local_search") {
      title = text("正在检索本地文献库", "Searching the local Library");
      detail = text("正在执行题录、分类规则和全文词法召回。", "Running metadata, taxonomy, and full-text lexical retrieval.");
    } else if (current <= 1) {
      title = text("正在生成查询计划", "Building the query plan");
      detail = text("正在读取项目主题、基础 Metadata 和分类规则。", "Reading the project topic, base Metadata, and taxonomy rules.");
    } else if (current === 2) {
      title = text("正在检索本地与联网来源", "Searching local and online sources");
      detail = text("正在执行题录、分类规则、全文词法和外部来源检索。", "Running metadata, taxonomy, full-text lexical, and external-source retrieval.");
    } else if (current === 3 || current === 4) {
      title = text("正在执行论文级语义召回", "Running paper-level semantic retrieval");
      detail = text("正在复用文献库 Chunk 向量，并将相关片段聚合为论文级排序。", "Reusing Library Chunk vectors and aggregating relevant passages into paper-level rankings.");
    } else if (current === 5) {
      title = text("正在重排并融合候选", "Reranking and fusing candidates");
      detail = text("正在进行外部标题摘要语义重排、跨来源去重和推荐解释。", "Semantically reranking external titles and abstracts, deduplicating sources, and building explanations.");
    } else {
      title = text("正在校验并保存结果", "Validating and saving results");
      detail = text("正在发布新的待确认检索产物。", "Publishing the new reviewable Discovery artifact.");
    }
  } else if (status === "cancel_requested") {
    title = text("正在停止检索", "Stopping search");
    detail = text("已收到停止请求，正在等待安全检查点。", "The stop request is waiting for a safe checkpoint.");
  } else if (status === "succeeded") {
    title = text("检索完成", "Search complete");
    detail = text("最新候选论文和分类分组已经载入下方审核区。", "The latest candidates and taxonomy groups are loaded below.");
  } else if (status === "failed") {
    if (job?.error_code === "INSUFFICIENT_CREDIT") {
      title = text("检索未执行", "Search not run");
      detail = text(
        "余额不足，本次检索已停止，且没有生成新的候选结果。请在“API 设置”中查看余额，或联系管理员添加额度后重新检索。",
        "Your balance is insufficient. This search was stopped and did not create new candidates. Review your balance in API Settings or contact an administrator for credit, then run the search again.",
      );
    } else {
      title = text("检索失败", "Search failed");
      detail = job?.error_message || text("检索任务执行失败，请检查错误信息后重试。", "The search failed. Review the error and try again.");
    }
  } else if (status === "cancelled") {
    title = text("检索已取消", "Search cancelled");
    detail = text("本次任务没有发布新的检索结果。", "This run did not publish new discovery results.");
  } else if (status === "interrupted") {
    title = text("检索已中断", "Search interrupted");
    detail = text("服务执行期间发生中断，请重新开始检索。", "The service interrupted this run. Start the search again.");
  }

  const stepText = useScreeningProgress
    ? text(`论文 ${current}/${total}`, `Paper ${current}/${total}`)
    : determinate
    ? text(`步骤 ${current}/${total}`, `Step ${current}/${total}`)
    : active ? text("处理中", "In progress") : text("已结束", "Finished");
  const stateClass = status === "submitting" ? "queued" : status;
  const sources = sourceStates(job?.result);

  return (
    <section className={`discovery-job-progress ${stateClass} ${active && !determinate ? "indeterminate" : ""}`} role="status" aria-live="polite" aria-atomic="true">
      <header>
        <div><span>{text("文献检索进度", "Discovery progress")}</span><strong>{title}</strong></div>
        <em>{stepText}</em>
      </header>
      <div
        className="discovery-job-progress-track"
        role="progressbar"
        aria-label={text("文献检索进度", "Literature discovery progress")}
        aria-valuemin={determinate ? 0 : undefined}
        aria-valuemax={determinate ? 100 : undefined}
        aria-valuenow={percentage}
        aria-valuetext={stepText}
      >
        <span style={percentage === undefined ? undefined : { width: `${percentage}%` }} />
      </div>
      <p>{detail}</p>
      {Object.keys(sources).length ? <div className="discovery-source-statuses" aria-label={text("联网来源状态", "Online source status")}>{sourceOrder.filter((source) => sources[source]).map((source) => { const state = sources[source]; const sourceStatus = String(state.status || "queued"); const count = Number(state.count || 0); return <span key={source} className={sourceStatus}><strong>{source === "semantic_scholar" ? "Semantic Scholar" : source === "arxiv" ? "arXiv" : source[0].toUpperCase() + source.slice(1)}</strong><em>{sourceStatusLabel(sourceStatus, text)}{count ? ` · ${count}` : ""}</em></span>; })}</div> : null}
    </section>
  );
}
