import { sectionErrorMessage } from "./sectionErrorMessage";
import { useState } from "react";
import type { Job } from "../../api/types";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";
import { sectionGenerationLabel, sectionReadinessLabel } from "./sectionStatusLabels";

type CompletedSection = {
  section_id?: string;
  heading?: string;
  generation_mode?: "standard" | "evidence_repaired" | "safe_evidence_fallback" | string;
  section_readiness?: { status?: string } | string;
};

type FailedSection = CompletedSection & { error?: string };

type SectionProgressResult = {
  drafted_section_ids?: string[];
  phase?: string;
  current_section_id?: string;
  current_heading?: string;
  active_sections?: Array<{ section_id: string; heading: string; phase: string }>;
  completed_sections?: CompletedSection[];
  failed_sections?: FailedSection[];
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
  const [preview, setPreview] = useState<{ jobId: string; sectionId: string }>();
  const total = Math.max(0, Number(job.progress_total || 0));
  const current = Math.max(0, Math.min(Number(job.progress_current || 0), total || Number.MAX_SAFE_INTEGER));
  const percentage = total > 0 ? Math.round((current / total) * 100) : undefined;
  const live = progressResult(job);
  const draftedCount = Math.min(total, new Set(live.drafted_section_ids || []).size);
  const completed = Array.isArray(live.completed_sections) ? live.completed_sections : [];
  const failed = Array.isArray(live.failed_sections) ? live.failed_sections : [];
  const checkpoint = job.result?.section_checkpoint as { entries?: Record<string, { output?: { draft_md?: unknown } }> } | undefined;
  const previews = completed.flatMap((section) => {
    const content = checkpoint?.entries?.[section.section_id || ""]?.output?.draft_md;
    return typeof content === "string" && content.trim() && !failed.some((item) => item.section_id === section.section_id)
      ? [{ ...section, content }] : [];
  });
  const selectedPreview = preview?.jobId === job.id
    ? previews.find((section) => section.section_id === preview.sectionId) : undefined;
  const standardCount = completed.filter((section) => !section.generation_mode || section.generation_mode === "standard").length;
  const repairedCount = completed.filter((section) => section.generation_mode === "evidence_repaired").length;
  const fallbackCount = completed.filter((section) => section.generation_mode === "safe_evidence_fallback").length;
  const evidenceNoticeCount = completed.filter((section) => section.generation_mode === "pending_evidence" || section.generation_mode === "limited_evidence").length;
  const active = ["queued", "running", "cancel_requested"].includes(job.status);
  const elapsedSeconds = job.started_at && job.updated_at
    ? Math.max(0, Math.floor((Date.parse(job.updated_at) - Date.parse(job.started_at)) / 1000)) : 0;
  const readinessLabel = (section: CompletedSection) => {
    const readiness = typeof section.section_readiness === "string"
      ? section.section_readiness
      : section.section_readiness?.status;
    return sectionReadinessLabel(readiness, text);
  };
  const generationLabel = (section: CompletedSection) => sectionGenerationLabel(
    section.generation_mode,
    text,
  );

  let title = text("章节任务正在排队", "Section job queued");
  let detail = text("正在等待可用的写作工作线程。", "Waiting for an available writing worker.");
  if (job.status === "queued" && job.queue_reason === "model_waiting") {
    title = text("正在等待模型结果", "Waiting for the model result");
    detail = text("模型请求已独立处理，章节写作资源已释放；收到结果后会自动继续，已完成内容会保留。",
      "The model request is running separately. Writing capacity is free; this job resumes automatically when the result arrives, and completed work is retained.");
  } else if (job.status === "queued" && job.queue_reason === "provider_rate_limit" && job.next_run_at) {
    title = text("模型限流，稍后自动继续", "Model rate-limited; resuming automatically");
    detail = text(`已完成的章节会保留，预计 ${new Date(job.next_run_at).toLocaleTimeString()} 后重试受影响章节。`,
      `Completed sections remain saved. The affected section becomes eligible again at ${new Date(job.next_run_at).toLocaleTimeString()}.`);
  } else if (job.status === "running") {
    if (failed.length && total > 0 && current >= total) {
      title = text("本轮处理结束，部分章节待修复", "Pass finished; some sections need repair");
      detail = text(`已保留 ${completed.length}/${total} 章；下次继续未完成章节。`, `${completed.length}/${total} sections retained; resume unfinished sections.`);
    } else if (total > 0 && current >= total) {
      title = completed.some(section => section.generation_mode === "pending_evidence")
        ? text("章节处理完成，部分章节暂不生成", "Sections processed; some have no prose")
        : text("章节正文已全部生成", "All section prose generated");
      detail = text("正在整理章节报告和图像候选。", "Finalizing the report and figure candidates.");
    } else if (live.active_sections?.length) {
      title = text(`正在处理 ${live.active_sections.length} 个章节`, `Processing ${live.active_sections.length} sections`);
      detail = live.active_sections.map(section => `${section.heading} · ${section.phase === "reviewing"
        ? text("正在核对来源", "Checking sources") : section.phase === "drafting"
          ? text("正在生成正文", "Writing prose") : text("准备中", "Preparing")}`).join("；");
    } else if (live.current_heading) {
      const phaseTitle: Record<string, string> = {
        planning_claims: text(`正在规划论证：${live.current_heading}`, `Planning claims: ${live.current_heading}`),
        drafting: text(`正在按计划成文：${live.current_heading}`, `Realizing plan: ${live.current_heading}`),
        reviewing: text(`正在自动审校：${live.current_heading}`, `Reviewing: ${live.current_heading}`),
        validating: text(`正在校验证据：${live.current_heading}`, `Validating evidence: ${live.current_heading}`),
        continuing_after_failure: text(`该章失败，继续后续章节：${live.current_heading}`, `Section failed; continuing after: ${live.current_heading}`),
        continuing_with_evidence_notice: text("已保存证据有限或待补充章节，继续生成。", "Saved an evidence notice; continuing generation."),
      };
      title = phaseTitle[String(live.phase || "")] || text(`正在生成：${live.current_heading}`, `Generating: ${live.current_heading}`);
      const evidenceDetail = Number(live.evidence_hit_count || 0) > 0
        ? text(`已找到 ${Number(live.evidence_hit_count)} 个证据段，来自 ${Number(live.evidence_paper_count || 0)} 篇论文。`, `${Number(live.evidence_hit_count)} evidence passages found across ${Number(live.evidence_paper_count || 0)} papers.`)
        : "";
      detail = `${evidenceDetail}${evidenceDetail ? " " : ""}${text(`已保留 ${completed.length}/${total} 章，待修复 ${failed.length} 章，完成一章后会立即更新。`, `${completed.length}/${total} sections retained, ${failed.length} need repair; each completion appears immediately.`)}`;
    } else {
      title = text("正在准备章节证据", "Preparing section evidence");
      detail = text("正在读取 Blueprint、MinerU 证据和章节写作规则。", "Reading the Blueprint, MinerU evidence, and writing rules.");
    }
  } else if (job.status === "cancel_requested") {
    title = text("正在安全停止章节生成", "Stopping section generation safely");
    detail = text("已完成的章节进度会保留在本次任务记录中。", "Completed section progress remains in this job record.");
  } else if (job.status === "succeeded") {
    title = text("章节生成完成", "Section generation complete");
    detail = text("章节任务已完成并保存为当前版本；内容完整性请结合各章提示检查。", "The section job completed and saved the current version; review each section's content notices for completeness.");
  } else if (job.status === "failed") {
    title = completed.length
      ? text("部分结果已保留，任务待继续", "Results retained; task needs continuation")
      : text("章节生成失败", "Section generation failed");
    detail = sectionErrorMessage(job.error_message || "", text);
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
      {active && Number.isFinite(elapsedSeconds) && elapsedSeconds > 0 ? <p>{text(
        `本次任务已进行 ${Math.floor(elapsedSeconds / 60)} 分 ${elapsedSeconds % 60} 秒；包含模型等待与后处理。`,
        `Task elapsed ${Math.floor(elapsedSeconds / 60)}m ${elapsedSeconds % 60}s, including model waits and post-processing.`
      )}</p> : null}
      {total > 0 && live.drafted_section_ids ? <p>{text(
        `正文已生成 ${draftedCount}/${total} · 全流程已处理 ${current}/${total}（不代表质量全部达标）`,
        `Prose generated ${draftedCount}/${total} · processing finished ${current}/${total} (not a quality approval)`
      )}</p> : null}
      {job.status === "queued" && job.queue_reason === "model_waiting" && live.active_sections?.length ? <ul>{live.active_sections.map(section => <li key={section.section_id}>
        {section.heading} · {section.phase === "reviewing" ? text("正文后处理：核验或局部修复中", "Post-writing: checking or local repair")
          : section.phase === "drafting" ? text("正文生成中", "Generating prose") : text("准备中", "Preparing")}
      </li>)}</ul> : null}
      {job.status !== "succeeded" && completed.length ? <p>{text(`已保留 ${completed.length} 章的检查点；整批发布前不会替换当前正式版本。`, `${completed.length} section checkpoints retained; the current version is unchanged until the batch is published.`)}</p> : null}
      {completed.length ? <p className="section-progress-summary">{text(`标准生成 ${standardCount} · 自动修复 ${repairedCount} · 安全保底 ${fallbackCount}`, `Standard ${standardCount} · repaired ${repairedCount} · safe fallback ${fallbackCount}`)}{evidenceNoticeCount ? text(` · 证据有限/待补充 ${evidenceNoticeCount}（可继续）`, ` · limited/pending evidence ${evidenceNoticeCount} (can continue)`) : ""}</p> : null}
      {completed.length ? <ol className="section-progress-completed">{completed.map((section, index) => <li key={`${section.section_id || "section"}-${index}`}><span>{section.heading || section.section_id || text(`章节 ${index + 1}`, `Section ${index + 1}`)}</span><small>{[generationLabel(section), readinessLabel(section)].filter(Boolean).join(" · ")}</small></li>)}</ol> : null}
      {failed.length ? <ol className="section-progress-completed failed">{failed.map((section, index) => <li key={`failed-${section.section_id || "section"}-${index}`}><span>{section.heading || section.section_id || text(`章节 ${index + 1}`, `Section ${index + 1}`)}</span><small>{sectionErrorMessage(section.error || "", text)}</small></li>)}</ol> : null}
      {job.status !== "succeeded" && previews.length ? <div className="section-checkpoint-preview">
        <p>{text("查看本次任务已保留的正文（未发布预览，不会替换当前稿件或进入下游）。", "Preview prose retained by this task. Unpublished; it does not replace the current manuscript or enter downstream stages.")}</p>
        <div className="chip-list">{previews.map((section) => <button type="button" key={section.section_id}
          aria-pressed={selectedPreview?.section_id === section.section_id}
          onClick={() => setPreview(selectedPreview?.section_id === section.section_id ? undefined : { jobId: job.id, sectionId: section.section_id || "" })}>
          {section.heading || section.section_id}
        </button>)}</div>
        {selectedPreview ? <article className="section-checkpoint-prose"><MarkdownView content={selectedPreview.content} /></article> : null}
      </div> : null}
    </section>
  );
}
