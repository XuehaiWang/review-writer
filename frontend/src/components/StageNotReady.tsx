import { useEffect, useRef } from "react";
import { useUiText } from "../i18n/useUiText";

const stages: Record<string, [string, string, string, string]> = {
  discovery: ["检索", "Discovery", "请先检索并确认本项目使用的论文。", "Search and confirm the papers for this project first."],
  planning: ["分析与大纲", "Analysis & outline", "请先完成大纲选择与章节规划。", "Complete outline selection and chapter planning first."],
  sections: ["章节", "Sections", "请先生成章节草稿。", "Generate the section drafts first."],
  images: ["图像", "Images", "请先完成当前所需的图像选择。", "Complete the required figure selection first."],
  draft: ["初稿", "Draft", "请先准备初稿并完成所需确认。", "Prepare the draft and complete its required confirmation first."],
  final: ["终稿", "Final", "当前还没有生成终稿文件，请先生成最终稿。", "No final file has been generated yet. Generate the final manuscript first."],
};

export function StageNotReady({ details, onRefresh }: { details: Record<string, unknown>; onRefresh?: () => void }) {
  const { text } = useUiText();
  const refresh = useRef(onRefresh); refresh.current = onRefresh;
  const stage = String(details.next_stage || "");
  const info = stages[stage];
  const job = details.active_job as { status?: string; current?: number; total?: number } | null;
  useEffect(() => {
    let busy = false;
    const timer = window.setInterval(async () => {
      if (busy || document.visibilityState === "hidden" || !refresh.current) return;
      busy = true;
      try { await refresh.current(); } catch { /* The query displays its actual error. */ }
      finally { busy = false; }
    }, job ? 5000 : 15000);
    return () => window.clearInterval(timer);
  }, [!!job]);
  return <section className="empty-state" role="status">
    <h2>{text("此阶段尚未就绪", "This stage is not ready yet")}</h2>
    <p>{details.reason === "figure_approval_required"
      ? text("初稿尚未准备好。请先前往图像阶段检查图片，并完成阶段确认；不使用的图片可按现有流程跳过。", "The draft is not ready. Review the figures and confirm the figure stage first; unused figures can be skipped through the existing workflow.")
      : info ? text(info[2], info[3]) : text("请先完成前面的工作步骤。", "Complete the preceding workflow steps first.")}</p>
    {job ? <p>{job.status === "queued" ? text("前置任务正在排队。", "The prerequisite task is queued.") : job.status === "cancel_requested" ? text("前置任务正在停止。", "The prerequisite task is stopping.") : text("前置任务正在执行。", "The prerequisite task is running.")}
      {Number(job.total) > 0 ? ` ${job.current || 0}/${job.total}` : ""}</p> : null}
    <p>{text("完成后此页面会自动更新，不会自动发起生成或消耗 API。", "This page updates after completion. No generation or paid API call is started automatically.")}</p>
    <div className="stage-not-ready-actions">
      {info ? <a className="button button-primary" href={`/${stage}?project=${encodeURIComponent(String(details.project_id || ""))}`}>{text(`前往${info[0]}`, `Go to ${info[1]}`)}</a> : null}
      {onRefresh ? <button className="button button-secondary" type="button" onClick={onRefresh}>{text("刷新状态", "Refresh status")}</button> : null}
    </div>
  </section>;
}
