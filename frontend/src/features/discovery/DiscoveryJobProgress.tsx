import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";

type DiscoveryJobProgressProps = {
  job?: Job;
  submitting?: boolean;
};

const activeStatuses = new Set(["queued", "running", "cancel_requested"]);
const sourceOrder = ["crossref", "openalex", "semantic_scholar", "arxiv"];

type SourceState = { status?: string; count?: number; error?: string; errors?: string[] };

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
  const total = Math.max(0, Number(job?.progress_total || 0));
  const current = Math.max(0, Math.min(Number(job?.progress_current || 0), total || Number.MAX_SAFE_INTEGER));
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
    if (current <= 1) {
      title = text("正在准备检索范围", "Preparing search scope");
      detail = text("正在读取项目主题、基础 Metadata 和分类规则。", "Reading the project topic, base Metadata, and taxonomy rules.");
    } else if (current === 2) {
      title = text("正在分析主题并检索论文", "Analyzing the topic and searching papers");
      detail = text("正在生成查询计划、纠正分类证据并检索本地与联网来源。", "Building the query plan, checking classification evidence, and searching local and online sources.");
    } else {
      title = text("正在校验并保存结果", "Validating and saving results");
      detail = text("正在去重候选论文、生成项目 Tag 建议并发布检索产物。", "Deduplicating candidates, creating project Tag suggestions, and publishing the discovery artifact.");
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

  const stepText = determinate
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
