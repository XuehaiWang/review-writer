import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";

type DiscoveryJobProgressProps = {
  job?: Job;
  submitting?: boolean;
};

const activeStatuses = new Set(["queued", "running", "cancel_requested"]);

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
    title = text("检索失败", "Search failed");
    detail = job?.error_message || text("检索任务执行失败，请检查错误信息后重试。", "The search failed. Review the error and try again.");
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
    </section>
  );
}
