import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";

type CompletedSection = {
  section_id?: string;
  heading?: string;
};

type SectionProgressResult = {
  phase?: string;
  current_section_id?: string;
  current_heading?: string;
  completed_sections?: CompletedSection[];
  evidence_hit_count?: number;
  evidence_paper_count?: number;
};

function progressResult(job?: Job): SectionProgressResult {
  const value = job?.result?.section_progress;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as SectionProgressResult
    : {};
}

export function SectionJobProgress({ job }: { job: Job }) {
  const { text } = useUiText();
  const total = Math.max(0, Number(job.progress_total || 0));
  const current = Math.max(0, Math.min(Number(job.progress_current || 0), total || Number.MAX_SAFE_INTEGER));
  const percentage = total > 0 ? Math.round((current / total) * 100) : undefined;
  const live = progressResult(job);
  const completed = Array.isArray(live.completed_sections) ? live.completed_sections : [];
  const active = ["queued", "running", "cancel_requested"].includes(job.status);

  let title = text("章节任务正在排队", "Section job queued");
  let detail = text("正在等待可用的写作工作线程。", "Waiting for an available writing worker.");
  if (job.status === "running") {
    if (total > 0 && current >= total) {
      title = text("章节正文已全部生成", "All section prose generated");
      detail = text("正在整理章节报告和图像候选。", "Finalizing the report and figure candidates.");
    } else if (live.current_heading) {
      if (live.phase === "validating") {
        title = text(`正在校验证据：${live.current_heading}`, `Validating evidence: ${live.current_heading}`);
      } else {
        title = text(`正在生成：${live.current_heading}`, `Generating: ${live.current_heading}`);
      }
      const evidenceDetail = Number(live.evidence_hit_count || 0) > 0
        ? text(`已找到 ${Number(live.evidence_hit_count)} 个证据段，来自 ${Number(live.evidence_paper_count || 0)} 篇论文。`, `${Number(live.evidence_hit_count)} evidence passages found across ${Number(live.evidence_paper_count || 0)} papers.`)
        : "";
      detail = `${evidenceDetail}${evidenceDetail ? " " : ""}${text(`已完成 ${current}/${total} 章，完成一章后会立即更新。`, `${current}/${total} sections complete; each completion appears immediately.`)}`;
    } else {
      title = text("正在准备章节证据", "Preparing section evidence");
      detail = text("正在读取 Blueprint、MinerU 证据和章节写作规则。", "Reading the Blueprint, MinerU evidence, and writing rules.");
    }
  } else if (job.status === "cancel_requested") {
    title = text("正在安全停止章节生成", "Stopping section generation safely");
    detail = text("已完成的章节进度会保留在本次任务记录中。", "Completed section progress remains in this job record.");
  } else if (job.status === "succeeded") {
    title = text("章节生成完成", "Section generation complete");
    detail = text("全部章节已经校验并发布为当前版本。", "All sections were validated and published as the current version.");
  } else if (job.status === "failed") {
    title = text("章节生成失败", "Section generation failed");
    detail = job.error_message || text("请查看错误信息并重试。", "Review the error and retry.");
  } else if (job.status === "cancelled") {
    title = text("章节生成已取消", "Section generation cancelled");
    detail = text("本次任务没有发布不完整章节。", "This job did not publish incomplete sections.");
  } else if (job.status === "interrupted") {
    title = text("章节生成已中断", "Section generation interrupted");
    detail = text("服务中断后可重新启动章节任务。", "Restart the section job after the service interruption.");
  }

  const counter = total > 0 ? `${current}/${total}` : text("准备中", "Preparing");

  return (
    <section className={`section-job-progress ${job.status} ${active && !total ? "indeterminate" : ""}`} role="status" aria-live="polite" aria-atomic="true">
      <header><div><span>{text("章节生成进度", "Section generation progress")}</span><strong>{title}</strong></div><em>{counter}</em></header>
      <div
        className="section-job-progress-track"
        role="progressbar"
        aria-label={text("章节生成进度", "Section generation progress")}
        aria-valuemin={total ? 0 : undefined}
        aria-valuemax={total ? 100 : undefined}
        aria-valuenow={percentage}
        aria-valuetext={counter}
      ><span style={percentage === undefined ? undefined : { width: `${percentage}%` }} /></div>
      <p>{detail}</p>
      {completed.length ? <ol className="section-progress-completed">{completed.map((section, index) => <li key={`${section.section_id || "section"}-${index}`}><span>{section.heading || section.section_id || text(`章节 ${index + 1}`, `Section ${index + 1}`)}</span><small>{text("已完成", "Complete")}</small></li>)}</ol> : null}
    </section>
  );
}
