import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";

export type FinalAction = "conclusion" | "overview" | "build" | "export";

type FinalJobStatusProps = {
  job?: Job;
  startingAction?: FinalAction;
  submissionError?: Error | null;
};

const activeStatuses = new Set(["queued", "running", "cancel_requested"]);

function actionFromJob(job: Job | undefined, fallback: FinalAction): FinalAction {
  const value = String(job?.job_type || "").replace(/^final\./, "");
  return value === "conclusion" || value === "overview" || value === "build" || value === "export"
    ? value
    : fallback;
}

export function FinalJobStatus({ job, startingAction = "build", submissionError = null }: FinalJobStatusProps) {
  const { text } = useUiText();
  const action = actionFromJob(job, startingAction);
  const status = submissionError ? "failed" : job?.status || "submitting";
  const active = status === "submitting" || activeStatuses.has(status);
  const total = Math.max(0, Number(job?.progress_total || 0));
  const current = Math.max(0, Math.min(Number(job?.progress_current || 0), total || Number.MAX_SAFE_INTEGER));
  const determinate = total > 0;
  const percentage = determinate
    ? Math.max(0, Math.min(100, Math.round((current / total) * 100)))
    : status === "succeeded" ? 100 : undefined;
  const actionLabel = action === "conclusion"
    ? text("结论生成", "Conclusion generation")
    : action === "overview"
      ? text("总览图生成", "Overview figure generation")
      : action === "export"
        ? text("Word 生成与下载", "Word generation and download")
        : text("最终稿生成", "Final draft generation");

  let title = text("正在提交任务", "Submitting job");
  let detail = text("正在保存任务并准备后台执行。", "Saving the job and preparing background execution.");
  if (status === "queued") {
    title = text("任务排队中", "Job queued");
    detail = text("任务已保存；离开本页或刷新后仍可继续查看进度。", "The job is persisted; progress remains available after navigation or refresh.");
  } else if (status === "running") {
    title = action === "conclusion"
      ? text("正在生成并校验结论", "Generating and validating conclusion")
      : action === "overview"
        ? text("正在生成并校验总览图", "Generating and validating overview figure")
        : action === "export"
          ? text("正在生成 Word 文档", "Generating Word document")
          : text("正在合并并审计最终稿", "Assembling and auditing final draft");
    if (action === "conclusion") {
      detail = current <= 1
        ? text("正在读取当前人工确认的初稿。", "Reading the currently approved draft.")
        : text("正在生成结论、执行完整性校验并发布结果。", "Generating the conclusion, validating integrity, and publishing the result.");
    } else if (action === "overview") {
      detail = current <= 1
        ? text("正在分析当前综述内容和图示结构。", "Analyzing the current review content and figure structure.")
        : current < total - 1
          ? text("图像服务正在生成综述总览图。", "The image provider is generating the review overview figure.")
          : text("正在校验图像并保存为当前总览图。", "Validating and saving the image as the current overview figure.");
    } else if (action === "export") {
      detail = current <= 1
        ? text("正在准备最终 Markdown、图片和参考文献。", "Preparing final Markdown, images, and references.")
        : text("正在生成、校验并发布 DOCX 文件。", "Generating, validating, and publishing the DOCX file.");
    } else {
      detail = current <= 1
        ? text("正在确认当前初稿和可用的结论、总览图。", "Checking the current draft and available conclusion and overview figure.")
        : current < total - 1
          ? text("正在按文章顺序合并内容并执行终稿审计。", "Merging content in article order and running the final audit.")
          : text("正在发布最终稿和审计结果。", "Publishing the final draft and audit result.");
    }
  } else if (status === "cancel_requested") {
    title = text("正在取消任务", "Cancelling job");
    detail = text("已收到取消请求，正在等待安全检查点。", "Cancellation was requested and is waiting for a safe checkpoint.");
  } else if (status === "succeeded") {
    title = text("任务已完成", "Job completed");
    detail = action === "export"
      ? text("Word 已生成；浏览器将开始下载，也可使用下方的当前 DOCX 下载入口。", "The Word file is ready. The browser will start the download, and the current DOCX link remains available below.")
      : text("结果已保存并同步到当前终稿阶段。", "The result was saved and synchronized to the current Final stage.");
  } else if (status === "failed") {
    title = text("任务执行失败", "Job failed");
    detail = submissionError?.message || job?.error_message || text("请检查服务配置后重试。", "Check the provider configuration and try again.");
  } else if (status === "cancelled") {
    title = text("任务已取消", "Job cancelled");
    detail = text("本次任务没有发布新的结果。", "This job did not publish a new result.");
  } else if (status === "interrupted") {
    title = text("任务已中断", "Job interrupted");
    detail = text("服务运行期间发生中断，请重新启动任务。", "The service interrupted this job. Start it again.");
  }

  const stateClass = status === "submitting" ? "queued" : status;
  const stepText = determinate
    ? text(`步骤 ${current}/${total}`, `Step ${current}/${total}`)
    : active ? text("处理中", "In progress") : text("已结束", "Finished");

  return (
    <section className={`draft-job-status final-job-status ${stateClass} ${active && !determinate ? "indeterminate" : ""}`} role="status" aria-live="polite" aria-atomic="true">
      <header>
        <div><span>{actionLabel}</span><strong>{title}</strong></div>
        <em>{stepText}</em>
      </header>
      <div
        className="draft-job-status-track"
        role="progressbar"
        aria-label={text(`${actionLabel}进度`, `${actionLabel} progress`)}
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
