import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiRequest } from "../../api/client";
import { useUiText } from "../../i18n/useUiText";
import { jobIsActive } from "../../hooks/useJob";

export type TopicRecommendationState = { input_fingerprint: string; status: string; previous_outline_md?: string; job?: { id?: string; created_at?: string; error_message?: string }; model_progress?: { attempt: number; status: string; model: string } | null };

export function TopicRecommendationPending({ projectId, state, refresh }: {
  projectId: string; state?: TopicRecommendationState | null; refresh: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const attempted = useRef("");
  const [refreshError, setRefreshError] = useState(false);
  const [now, setNow] = useState(Date.now());
  const refreshRef = useRef(refresh); refreshRef.current = refresh;
  const generate = useMutation({ mutationFn: (retry: boolean) => apiRequest<{ id: string; status: string }>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline/topic/jobs?retry=${retry}`, { method: "POST" }),
    onSuccess: async () => { try { await refreshRef.current(); setRefreshError(false); } catch { setRefreshError(true); } } });
  const start = useRef(generate.mutate); start.current = generate.mutate;
  useEffect(() => {
    if (state?.status === "not_started" && attempted.current !== `${projectId}:${state.input_fingerprint}`) {
      attempted.current = `${projectId}:${state.input_fingerprint}`;
      start.current(false);
    }
  }, [projectId, state?.input_fingerprint, state?.status]);
  // Keep following an accepted job even if the first page refresh failed or was stale.
  const submitted = generate.data && jobIsActive(generate.data.status)
    && (state?.job?.id !== generate.data.id || jobIsActive(state.status));
  const active = generate.isPending || !!submitted || !!state && jobIsActive(state.status);
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let refreshing = false;
    const timer = window.setInterval(async () => {
      setNow(Date.now());
      if (refreshing) return;
      refreshing = true;
      try { await refreshRef.current(); if (!disposed) setRefreshError(false); }
      catch { if (!disposed) setRefreshError(true); }
      finally { refreshing = false; }
    }, 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [active]);
  const elapsed = state?.job?.created_at ? Math.max(0, Math.floor((now - Date.parse(state.job.created_at)) / 1000)) : 0;
  const progress = state?.model_progress;
  return <article className="topic-outline-recommendation">
    <div className="topic-outline-recommendation-copy"><h3>{text("主题驱动的组合大纲", "Topic-guided hybrid outline")}</h3>
      <p role="status">{active ? text("正在根据主题和入选论文推荐章节结构，无需等待事实卡全部完成。", "Recommending an outline from your topic and selected papers; complete fact cards are not required.")
        : text("推荐尚未就绪。系统会分析主题和论文内容，不使用固定模板替代推荐，也不会覆盖已保存的大纲。", "The recommendation is not ready. It uses your topic and papers, not a fixed template, and will not overwrite your saved outline.")}</p>
      {active ? <progress aria-label={text("正在推荐大纲", "Generating outline recommendation")} /> : null}
      {active ? <p role="status">{progress
        ? progress.status === "succeeded" ? text("模型已返回，正在检查并整理推荐。", "Model response received; validating the recommendation.")
          : progress.attempt > 1 ? text(`模型请求正在第 ${progress.attempt} 次尝试，请勿重复提交。`, `Model request attempt ${progress.attempt} is in progress. Please do not resubmit.`)
          : text("正在等待模型返回章节建议。", "Waiting for the model's outline recommendation.")
        : state?.status === "queued" ? text("任务已提交，正在排队。", "Submitted; waiting in the queue.")
          : text("任务已提交，正在准备论文依据。", "Submitted; preparing paper evidence.")}
        {elapsed > 0 ? text(` 已等待 ${elapsed} 秒。`, ` Waiting for ${elapsed} seconds.`) : ""}
        {text("完成后会自动显示，不会覆盖已保存的大纲。", "The result will appear automatically without overwriting your saved outline.")}</p> : null}
      {refreshError ? <p className="message message-warning">{text("状态同步暂时失败，正在重新连接；请勿重复提交任务。", "Status refresh failed; reconnecting. Please do not resubmit the task.")}</p> : null}
      {generate.error || state?.job?.error_message ? <p className="message message-error">{generate.error?.message || state?.job?.error_message}</p> : null}
      {state?.previous_outline_md ? <details><summary>{text("查看上次推荐（依据已变化，等待更新）", "Previous recommendation (inputs changed)")}</summary><pre style={{ whiteSpace: "pre-wrap" }}>{state.previous_outline_md}</pre></details> : null}
    </div>
    <button className="button button-primary" disabled={active || state?.status === "waiting_for_papers"} onClick={() => generate.mutate(true)}>
      {active ? text("正在推荐…", "Generating…") : text("生成 / 重试推荐", "Generate / retry recommendation")}
    </button>
  </article>;
}
