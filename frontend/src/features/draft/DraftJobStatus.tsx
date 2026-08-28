import type { Job } from "../../api/types";
import { useUiText } from "../../i18n/useUiText";

type DraftJobStatusProps = {
  job?: Job;
  startingType?: "draft.evaluate" | "draft.optimize" | "draft.rewrite" | "draft.accept-rewrite";
  publicationPending?: boolean;
};

const activeStatuses = new Set(["queued", "running", "cancel_requested"]);

export function DraftJobStatus({ job, startingType = "draft.evaluate", publicationPending = false }: DraftJobStatusProps) {
  const { text } = useUiText();
  const jobType = job?.job_type || startingType;
  const status = job?.status || "submitting";
  const evaluating = jobType === "draft.evaluate";
  const optimizing = jobType === "draft.optimize";
  const acceptingRewrite = jobType === "draft.accept-rewrite";
  const result = (job?.result || {}) as Record<string, unknown>;
  const feedback = (result.feedback_status || {}) as Record<string, unknown>;
  const evidenceRepair = (result.evidence_repair || {}) as Record<string, unknown>;
  const referenceRepair = (result.reference_repair || {}) as Record<string, unknown>;
  const repairTasks = Array.isArray(result.repair_tasks)
    ? result.repair_tasks.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
  const repairStatus = String(result.repair_status || "");
  const phase = String(feedback.phase || "");
  const iteration = Number(feedback.iteration || 0);
  const maxIterations = Number(feedback.max_iterations || 0);
  const currentParagraph = String(feedback.current_paragraph_id || "");
  const rewriteCompleted = Number(feedback.rewrite_completed || 0);
  const rewriteTotal = Number(feedback.rewrite_total || 0);
  const paragraphCompleted = Number(feedback.paragraph_completed || 0);
  const paragraphTotal = Number(feedback.paragraph_total || 0);
  const scoringBatchCompleted = Number(feedback.scoring_batch_completed || 0);
  const scoringBatchTotal = Number(feedback.scoring_batch_total || 0);
  const draftChanged = typeof result.draft_changed === "boolean" ? result.draft_changed : undefined;
  const proposalCreated = result.proposal_created === true;
  const changeCount = Number(result.change_count || 0);
  const acceptedRewrites = Number(result.rewrite_accepted || feedback.rewrite_accepted || 0);
  const rejectedRewrites = Number(result.rewrite_rejected || feedback.rewrite_rejected || 0);
  const deferredRewrites = Number(result.rewrite_deferred || feedback.rewrite_deferred || 0);
  const deferredParagraphIds = Array.isArray(feedback.deferred_paragraph_ids)
    ? feedback.deferred_paragraph_ids.map(String).filter(Boolean)
    : [];
  const bestScore = Number(feedback.best_score || result.score || 0);
  const bestScoreRestored = feedback.best_score_restored === true;
  const repairedEvidenceCount = Number(evidenceRepair.added_evidence_count || 0);
  const downgradedClaimCount = Number(evidenceRepair.downgraded_claim_count || 0);
  const referenceRebuilt = referenceRepair.changed === true;
  const active = status === "submitting" || activeStatuses.has(status);
  const total = Math.max(0, Number(job?.progress_total || 0));
  const current = Math.max(0, Math.min(Number(job?.progress_current || 0), total || Number.MAX_SAFE_INTEGER));
  const determinate = total > 0;
  const percentage = determinate ? Math.max(0, Math.min(100, Math.round((current / total) * 100))) : status === "succeeded" ? 100 : undefined;

  let title = evaluating ? text("正在启动评估", "Starting evaluation") : optimizing ? text("正在启动批量优化", "Starting batch optimization") : acceptingRewrite ? text("正在保存候选", "Starting candidate save") : text("正在启动候选生成", "Starting candidate generation");
  let detail = evaluating
    ? text("正在提交当前初稿的评估任务。", "Submitting the current draft for evaluation.")
    : optimizing
      ? text("正在提交批量安全优化任务。", "Submitting the batch safe-optimization job.")
      : acceptingRewrite
        ? text("正在提交已评分候选的保存任务。", "Submitting the scored candidate for saving.")
        : text("正在提交候选生成与单段评分任务。", "Submitting candidate generation and paragraph scoring.");

  if (status === "queued") {
    title = text("任务排队中", "Job queued");
    detail = evaluating
      ? text("评估任务已经提交，正在等待工作线程。", "The evaluation job is waiting for a worker.")
      : optimizing
        ? text("批量优化任务已经提交，正在等待工作线程。", "The batch optimization job is waiting for a worker.")
        : acceptingRewrite
          ? text("候选保存任务已经提交，正在等待工作线程。", "The candidate save job is waiting for a worker.")
          : text("候选生成与评分任务已经提交，正在等待工作线程。", "The candidate generation and scoring job is waiting for a worker.");
  } else if (status === "running") {
    title = evaluating ? text("正在评估当前初稿", "Evaluating current draft") : optimizing ? text("正在批量安全优化", "Running batch safe optimization") : acceptingRewrite ? text("正在保存已评分候选", "Saving scored candidate") : text("正在生成并评分候选", "Generating and scoring candidate");
    if (optimizing && phase === "diagnosing") {
      detail = text("正在合并重复问题并定位共同根因。", "Grouping repeated findings and locating their shared root causes.");
    } else if (optimizing && ["repairing_references", "repairing_deterministic"].includes(phase)) {
      detail = text("正在按稳定的 Paper ID 核对引文，并准备重建连续参考文献编号。", "Checking citations against stable Paper IDs and preparing a consecutive reference rebuild.");
    } else if (optimizing && phase === "repairing_evidence") {
      detail = text("正在把逐段原文复核命中的片段写回版本化证据包，并记录证据不足 Claim 的降级轨迹。", "Persisting matched original-source passages into the versioned evidence package and recording downgraded evidence-gap claims.");
    } else if (optimizing && phase === "validating_full_draft") {
      detail = text("正在对组合后的候选全文重新执行一次完整评分与硬性校验。", "Re-evaluating the exact combined candidate as a full manuscript with all hard checks.");
    } else if (optimizing && ["publishing", "validating"].includes(phase)) {
      detail = text("正在执行完整终稿校验，并原子发布正文、证据包、参考文献和质量报告。", "Running final full-draft validation and atomically publishing the manuscript, evidence package, references, and quality report.");
    } else if ((evaluating || optimizing) && phase === "preflight") {
      detail = text("正在执行确定性预检。", "Running deterministic preflight checks.");
    } else if ((evaluating || optimizing) && phase === "source_checking") {
      detail = text("正在逐段核对原始文献证据。", "Checking original-source evidence paragraph by paragraph.");
    } else if ((evaluating || optimizing) && phase === "scoring") {
      if (scoringBatchTotal > 0) {
        const activeBatch = Math.min(scoringBatchTotal, scoringBatchCompleted + 1);
        detail = text(
          `正在分批评分：第 ${activeBatch}/${scoringBatchTotal} 批，已完成 ${paragraphCompleted}/${paragraphTotal || "—"} 个段落。`,
          `Scoring batch ${activeBatch}/${scoringBatchTotal}; ${paragraphCompleted}/${paragraphTotal || "—"} paragraphs completed.`,
        );
      } else {
        detail = text("正在进行全文和逐段评分。", "Scoring the full draft and every paragraph.");
      }
    } else if (optimizing && phase === "rewriting") {
      detail = text(
        `第 ${iteration}/${maxIterations || "—"} 轮：安全重写 ${rewriteCompleted}/${rewriteTotal}${currentParagraph ? `，当前 ${currentParagraph}` : ""}${deferredRewrites ? `，暂缓 ${deferredRewrites}` : ""}。`,
        `Iteration ${iteration}/${maxIterations || "—"}: safe rewrites ${rewriteCompleted}/${rewriteTotal}${currentParagraph ? `, current ${currentParagraph}` : ""}${deferredRewrites ? `, deferred ${deferredRewrites}` : ""}.`,
      );
    } else if (evaluating && current >= 2) {
      detail = text("评分已经完成，正在校验并保存评估结果。", "Scoring is complete; validating and saving the evaluation result.");
    } else if (evaluating) {
      detail = text("正在执行预检、原文核查和 AI 全文及逐段评分，这可能需要几分钟。", "Running preflight, source checks, and AI full-draft and paragraph scoring. This may take a few minutes.");
    } else if (optimizing) {
      detail = text("正在评估、生成安全段落重写并复评；达到目标或恢复最佳版本后结束。", "Evaluating, applying safe paragraph rewrites, and re-evaluating until the goal or best-state restore is reached.");
    } else if (acceptingRewrite) {
      detail = text("候选已经完成单段评分；现在只写入候选并按已存分差增量更新全文分数，不再调用模型。", "The candidate has already been scored. It is now being saved and the overall score updated from the stored delta without another model call.");
    } else {
      detail = text("正在生成所选段落的候选，随后只评分该候选段落；不会评估全文。", "Generating a candidate for the selected paragraph, then scoring only that candidate; the full draft is not evaluated.");
    }
  } else if (status === "cancel_requested") {
    title = text("正在取消任务", "Cancelling job");
    detail = text("已收到取消请求，正在等待当前安全检查点。", "Cancellation was requested and is waiting for the next safe checkpoint.");
  } else if (status === "succeeded") {
    if (publicationPending) {
      title = text("正在同步最新结果", "Synchronizing latest result");
      detail = text("任务已完成，正在重新读取已发布的初稿与评估。", "The job is complete. Reloading the published draft and evaluation now.");
    } else {
      if (optimizing && proposalCreated) {
        title = text("批量优化候选已生成", "Batch optimization proposal ready");
        detail = text(
          `已生成 ${changeCount} 个段落的优化对比${deferredRewrites ? `；另有 ${deferredRewrites} 段因服务商暂时不可用而待重试` : ""}，正文尚未改变。请在“评估与重写”中检查后选择“保存全部优化”或“放弃本批”。`,
          `${changeCount} paragraph comparisons are ready${deferredRewrites ? `; ${deferredRewrites} more were deferred because the provider was temporarily unavailable` : ""}. The saved draft is unchanged. Review them under Evaluation and rewriting, then save or discard the batch.`,
        );
      } else if (optimizing && draftChanged === false) {
        title = text("优化完成，正文未改变", "Optimization complete; draft unchanged");
        detail = deferredRewrites > 0
          ? text(
            `本次队列已继续处理到末尾，但 ${deferredRewrites} 个段落因服务商暂时不可用而待重试${deferredParagraphIds.length ? `：${deferredParagraphIds.join("、")}` : ""}。其他段落的结果和检查点均已保留。`,
            `The queue continued to completion, but ${deferredRewrites} paragraph(s) were deferred because the provider was temporarily unavailable${deferredParagraphIds.length ? `: ${deferredParagraphIds.join(", ")}` : ""}. Results and checkpoints for the other paragraphs were preserved.`,
          )
          : bestScoreRestored
          ? text(
            `循环中有 ${acceptedRewrites} 次候选通过单次安全校验、${rejectedRewrites} 次被拒绝，但复评没有超过最佳分数 ${bestScore.toFixed(2)}，因此已恢复优化前的最佳正文。最新评分和问题请在“评估与重写”中查看。`,
            `${acceptedRewrites} candidate rewrites passed individual safety checks and ${rejectedRewrites} were rejected, but re-evaluation did not beat the best score of ${bestScore.toFixed(2)}. The best pre-optimization draft was restored. See Evaluation and rewriting for the latest score and issues.`,
          )
          : text(
            "没有候选在全文复评后形成可发布的净改进，因此正文保持不变；最新评分和问题已经更新。",
            "No candidate produced a publishable net improvement after full-draft re-evaluation, so the draft remains unchanged; the latest score and issues were updated.",
          );
      } else {
        title = evaluating ? text("初稿评估完成", "Draft evaluation complete") : optimizing ? text("批量安全优化完成", "Batch safe optimization complete") : acceptingRewrite ? text("已保存评分候选", "Scored candidate saved") : text("候选已生成并评分", "Candidate generated and scored");
        detail = evaluating
          ? text("最新分数和问题段落已经刷新。", "The latest score and paragraph issues have been refreshed.")
          : optimizing
            ? draftChanged
              ? text(
                `安全修改已保存：新增 ${repairedEvidenceCount} 条直接证据，记录 ${downgradedClaimCount} 条 Claim 处置${referenceRebuilt ? "，并重建了参考文献编号" : ""}；仅有歧义候选继续留给人工确认。`,
                `Safe changes were saved with ${repairedEvidenceCount} direct evidence addition(s), ${downgradedClaimCount} Claim disposition(s)${referenceRebuilt ? ", and rebuilt reference numbering" : ""}; only ambiguous candidates remain for manual review.`,
              )
              : text("优化结果已生成，请检查仍需人工确认的段落对比。", "The optimization result is ready. Review any paragraph comparisons that still require manual confirmation.")
            : acceptingRewrite
              ? text("已采用候选生成时的单段分数，并发布增量更新后的全文分数。", "The paragraph score computed with the candidate was reused and the incrementally updated overall score was published.")
              : text("候选分数已经显示。请比较原文、候选及分数，然后选择保存或放弃。", "The candidate score is ready. Compare the original, candidate, and scores, then save or discard it.");
      }
    }
  } else if (status === "failed") {
    title = evaluating ? text("初稿评估失败", "Draft evaluation failed") : optimizing ? text("批量安全优化失败", "Batch safe optimization failed") : acceptingRewrite ? text("候选保存失败", "Candidate save failed") : text("候选生成或评分失败", "Candidate generation or scoring failed");
    detail = job?.error_message || text("任务执行失败，请检查模型配置后重试。", "The job failed. Check the model configuration and try again.");
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
    : active
      ? text("处理中", "In progress")
      : text("已结束", "Finished");

  return (
    <section className={`draft-job-status ${stateClass} ${active && !determinate ? "indeterminate" : ""}`} role="status" aria-live="polite" aria-atomic="true">
      <header>
        <div><span>{evaluating ? text("初稿质量评估", "Draft quality evaluation") : optimizing ? text("批量安全优化", "Batch safe optimization") : acceptingRewrite ? text("候选保存", "Candidate save") : text("候选生成与评分", "Candidate generation and scoring")}</span><strong>{title}</strong></div>
        <em>{stepText}</em>
      </header>
      <div
        className="draft-job-status-track"
        role="progressbar"
        aria-label={evaluating ? text("初稿评估进度", "Draft evaluation progress") : optimizing ? text("批量优化进度", "Batch optimization progress") : acceptingRewrite ? text("候选保存进度", "Candidate save progress") : text("候选生成与评分进度", "Candidate generation and scoring progress")}
        aria-valuemin={determinate ? 0 : undefined}
        aria-valuemax={determinate ? 100 : undefined}
        aria-valuenow={percentage}
        aria-valuetext={stepText}
      >
        <span style={percentage === undefined ? undefined : { width: `${percentage}%` }} />
      </div>
      <p>{detail}</p>
      {repairTasks.length ? <details className="draft-repair-task-summary"><summary>{text(`修复任务 ${repairTasks.length} · ${repairStatus || "completed"}`, `${repairTasks.length} repair task(s) · ${repairStatus || "completed"}`)}</summary><ul>{repairTasks.map((task, index) => { const target = (task.target || {}) as Record<string, unknown>; const paragraphs = Array.isArray(target.paragraph_ids) ? target.paragraph_ids.map(String).filter(Boolean) : []; const sections = Array.isArray(target.section_ids) ? target.section_ids.map(String).filter(Boolean) : []; return <li key={String(task.task_id || index)}><strong>{String(task.repair_route || "repair")}</strong><span>{[...sections, ...paragraphs].join(" · ") || text("全文", "Full draft")}</span><em>{String(task.status || "queued")}</em></li>; })}</ul></details> : null}
    </section>
  );
}
