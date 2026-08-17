import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { MarkdownView } from "../../components/MarkdownView";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { FinalJobStatus, type FinalAction } from "./FinalJobStatus";
import { readFinalJobId, writeFinalJobId } from "./finalJobPersistence";

type FinalPayload = {
  project_id: string;
  revision: number;
  status: string;
  draft_approval_current: boolean;
  draft_approval: Record<string, unknown> & { record?: Record<string, unknown> };
  final_draft_md: string;
  final_artifact_id: string;
  final_current: boolean;
  conclusion_generated_md: string;
  conclusion_artifact_id: string;
  conclusion_current: boolean;
  overview_figure_url: string;
  overview_figure_exists: boolean;
  overview_figure_current: boolean;
  overview_text: { title?: string; subtitle?: string; labels?: string[] };
  validation: Record<string, unknown> & { valid?: boolean; blocking_issues?: string[]; warning_issues?: string[] };
  release: Record<string, unknown> & { status?: string };
  release_current: boolean;
  docx_url: string;
  final_draft_docx_exists: boolean;
  final_draft_docx_stale: boolean;
  active_final_job_id: string;
  active_final_job_type: string;
  latest_final_job_id: string;
  latest_final_job_type: string;
  latest_final_job_status: string;
  final_audit_report_md: string;
  release_report_md: string;
  freshness: { draft_stale: boolean; final_stale: boolean; release_stale: boolean; stale: boolean };
};

type FinalTab = "preparation" | "conclusion" | "overview" | "final" | "audit" | "release";

function actionFromJobType(jobType?: string): FinalAction {
  const action = String(jobType || "").replace(/^final\./, "");
  return action === "conclusion" || action === "overview" || action === "export" ? action : "build";
}

function StatusPill({ exists, current, optional = true }: { exists: boolean; current: boolean; optional?: boolean }) {
  const { text } = useUiText();
  const label = current
    ? text("当前", "Current")
    : exists
      ? text("已过期", "Stale")
      : optional
        ? text("可选 / 尚未生成", "Optional / not generated")
        : text("未生成", "Not generated");
  return <span className={current ? "status-pill current" : exists ? "status-pill stale" : "status-pill"}>{label}</span>;
}

export function FinalPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const { selected: project } = useSelectedProject();
  const [tab, setTab] = useState<FinalTab>("preparation");
  const [selectedJob, setSelectedJob] = useState({ projectId: "", jobId: "" });
  const [startingAction, setStartingAction] = useState<FinalAction>("build");
  const [overviewTitle, setOverviewTitle] = useState("");
  const [overviewSubtitle, setOverviewSubtitle] = useState("");
  const [overviewLabels, setOverviewLabels] = useState("");
  const downloadedJob = useRef("");
  const pendingDownloadJob = useRef("");

  const final = useQuery({
    queryKey: ["final", project?.project_id || ""],
    queryFn: () => apiRequest<FinalPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/final`),
    enabled: Boolean(project),
    refetchInterval: (query) => query.state.data?.active_final_job_id ? 1_000 : false,
    refetchIntervalInBackground: true,
  });
  const payload = final.data;
  const localJobId = selectedJob.projectId === project?.project_id ? selectedJob.jobId : "";
  const storedJobId = readFinalJobId(project?.project_id || "");
  const storedCurrentJobId = storedJobId && (storedJobId === payload?.active_final_job_id || storedJobId === payload?.latest_final_job_id) ? storedJobId : "";
  const currentJobId = payload?.active_final_job_id || localJobId || payload?.latest_final_job_id || storedCurrentJobId;
  const job = useJob(currentJobId || "");
  const currentJob = job.data;
  const currentAction = actionFromJobType(currentJob?.job_type || payload?.active_final_job_type || payload?.latest_final_job_type);
  const refresh = async () => queryClient.invalidateQueries({ queryKey: ["final", project?.project_id || ""] });

  const rememberJob = (jobId: string) => {
    if (!project?.project_id || !jobId) return;
    writeFinalJobId(project.project_id, jobId);
    setSelectedJob({ projectId: project.project_id, jobId });
  };

  useEffect(() => {
    setOverviewTitle(payload?.overview_text?.title || "");
    setOverviewSubtitle(payload?.overview_text?.subtitle || "");
    setOverviewLabels((payload?.overview_text?.labels || []).join("\n"));
  }, [payload?.overview_text]);

  useEffect(() => {
    if (!project?.project_id) return;
    const serverJobId = payload?.active_final_job_id || payload?.latest_final_job_id || "";
    setSelectedJob((current) => {
      const currentId = current.projectId === project.project_id ? current.jobId : "";
      if (!serverJobId || currentId) return current;
      writeFinalJobId(project.project_id, serverJobId);
      return { projectId: project.project_id, jobId: serverJobId };
    });
  }, [payload?.active_final_job_id, payload?.latest_final_job_id, project?.project_id]);

  useEffect(() => {
    const completed = job.data;
    if (!completed || jobIsActive(completed.status) || completed.status !== "succeeded") return;
    void (async () => {
      await refresh();
      const action = actionFromJobType(completed.job_type);
      if (action === "conclusion") setTab("conclusion");
      if (action === "overview") setTab("overview");
      if (action === "build") setTab("final");
      if (action === "export" && pendingDownloadJob.current === completed.id && downloadedJob.current !== completed.id) {
        downloadedJob.current = completed.id;
        pendingDownloadJob.current = "";
        const artifactId = String(completed.result?.docx_artifact_id || "");
        const href = artifactId ? `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content` : "";
        if (href) {
          const link = document.createElement("a");
          link.href = href;
          link.download = String(completed.result?.download_name || "final_draft.docx");
          document.body.append(link);
          link.click();
          link.remove();
        }
      }
    })();
  }, [job.data?.id, job.data?.status]);

  const runJob = useMutation({
    mutationFn: (action: FinalAction) => apiRequest<Job>(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/${action}-jobs`,
      { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody({}) },
    ),
    onSuccess: (started, action) => {
      rememberJob(started.id);
      if (action === "export") pendingDownloadJob.current = started.id;
      void refresh();
    },
  });
  const startJob = (action: FinalAction) => {
    setStartingAction(action);
    runJob.mutate(action);
  };
  const saveOverview = useMutation({
    mutationFn: () => apiRequest(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/overview-text`,
      { method: "PUT", ...jsonBody({
        revision: payload!.revision,
        title: overviewTitle.trim(),
        subtitle: overviewSubtitle.trim(),
        labels: overviewLabels.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      }) },
    ),
    onSuccess: refresh,
  });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${encodeURIComponent(currentJobId || "")}/cancel`, { method: "POST" }) });
  const active = runJob.isPending || Boolean(currentJob && jobIsActive(currentJob.status));
  const error = saveOverview.error || cancel.error || (currentJob?.status === "failed" ? new Error(currentJob.error_message || text("终稿任务失败。", "Final-stage task failed.")) : null);
  const tabs: Array<[FinalTab, string]> = [
    ["preparation", text("终稿准备", "Final preparation")],
    ["conclusion", text("结论", "Conclusion")],
    ["overview", text("综述总览图", "Review overview figure")],
    ["final", text("最终稿", "Final draft")],
    ["audit", text("终稿审计", "Final audit")],
    ["release", text("发布报告", "Release report")],
  ];
  const approval = payload?.draft_approval.record || payload?.draft_approval || {};

  return <main className="workspace page-container workspace-page final-page">
    <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 7 · 终稿合并与导出", "Stage 7 · Final assembly and export")}</p><h1>{text("终稿生成、审计与Word导出", "Final generation, audit, and Word export")}</h1><p className="muted">{text("结论和总览图是可选中间产物；生成最终稿会使用当前存在且有效的内容。", "The conclusion and overview figure are optional intermediates; final generation uses any current, valid content.")}</p></div><ProjectSelector /></div>
    {final.isPending ? <div className="empty-state">{text("正在加载终稿产物…", "Loading final artifacts…")}</div> : null}
    {final.error ? <ErrorState error={final.error} onRetry={() => final.refetch()} /> : null}
    {payload ? <><div className="final-grid-react">
      <aside className="pane final-list-react"><div className="pane-head"><div><span className="step-label">{text("终稿产物", "Final outputs")}</span><h2>{project?.slug || project?.project_id}</h2></div></div><div className="draft-flow-list">{tabs.map(([value, label]) => <button key={value} className={tab === value ? "active" : ""} type="button" onClick={() => setTab(value)}><strong>{label}</strong></button>)}</div></aside>
      <section className="pane final-main-react"><div className="pane-head"><div><span className="step-label">{payload.freshness.stale ? text("已过期", "Out of date") : text("当前", "Current")}</span><h2>{tabs.find(([value]) => value === tab)?.[1]}</h2></div></div><div className="final-document-react">
        {tab === "preparation" ? <div className="final-preparation-cards"><article className={payload.draft_approval_current ? "good" : "bad"}><h3>{payload.draft_approval_current ? text("初稿已确认", "Draft approved") : text("需要先确认初稿", "Draft approval required")}</h3>{approval.score !== undefined ? <p>{text("分数", "Score")}: {String(approval.score)} / {String(approval.goal || "")}</p> : null}</article><article><strong>{text("结论", "Conclusion")}</strong><StatusPill exists={Boolean(payload.conclusion_artifact_id)} current={payload.conclusion_current} /></article><article><strong>{text("综述总览图", "Review overview figure")}</strong><StatusPill exists={payload.overview_figure_exists} current={payload.overview_figure_current} /></article><article><strong>{text("最终稿", "Final draft")}</strong><StatusPill exists={Boolean(payload.final_artifact_id)} current={payload.final_current} optional={false} /></article></div> : null}
        {tab === "conclusion" ? <MarkdownView content={payload.conclusion_generated_md} empty={text("尚未生成结论；也可直接生成最终稿。", "No conclusion generated; you can also build the final draft directly.")} /> : null}
        {tab === "overview" ? payload.overview_figure_exists ? <><figure className="overview-figure-react"><img src={payload.overview_figure_url} alt={text("综述总览图", "Review overview figure")} /></figure><section className="overview-text-editor"><h3>{text("可编辑总览图文字", "Editable overview figure text")}</h3><label>{text("标题", "Title")}<input value={overviewTitle} onChange={(event) => setOverviewTitle(event.target.value)} /></label><label>{text("副标题", "Subtitle")}<input value={overviewSubtitle} onChange={(event) => setOverviewSubtitle(event.target.value)} /></label><label>{text("标签（每行一个）", "Labels (one per line)")}<textarea rows={7} value={overviewLabels} onChange={(event) => setOverviewLabels(event.target.value)} /></label><button className="button button-primary" type="button" disabled={!overviewTitle.trim() || saveOverview.isPending} onClick={() => saveOverview.mutate()}>{text("保存总览图文字", "Save overview text")}</button></section></> : <div className="empty-state">{text("尚未生成总览图；也可直接生成最终稿。", "No overview figure generated; you can also build the final draft directly.")}</div> : null}
        {tab === "final" ? <MarkdownView content={payload.final_draft_md} empty={text("尚未生成最终稿。", "Final draft not generated yet.")} /> : null}
        {tab === "audit" ? <MarkdownView content={payload.final_audit_report_md} empty={text("尚未执行终稿审计。", "Final audit has not run.")} /> : null}
        {tab === "release" ? <MarkdownView content={payload.release_report_md} empty={text("尚未生成发布报告。", "Release report not generated yet.")} /> : null}
      </div></section>
      <aside className="pane final-actions-react"><div className="pane-head"><div><span className="step-label">{text("生成操作", "Generation actions")}</span><h2>{text("生成操作", "Generation actions")}</h2></div></div><div className="gate-body">
        <p>{text("所有操作严格绑定当前人工确认的初稿版本。", "Every operation is strictly bound to the currently approved draft version.")}</p>
        <button className="button button-secondary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("conclusion")}>{text("生成结论", "Generate conclusion")}</button>
        <button className="button button-secondary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("overview")}>{text("生成总览图", "Generate overview figure")}</button>
        <button className="button button-primary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("build")}>{text("生成最终稿", "Generate final draft")}</button>
        <button className="button button-primary" type="button" disabled={!payload.final_current || !payload.release_current || active} onClick={() => startJob("export")}>{text("生成并下载Word", "Generate and download Word")}</button>
        <button className="button button-quiet danger" type="button" disabled={!currentJob || !jobIsActive(currentJob.status) || cancel.isPending} onClick={() => cancel.mutate()}>{text("取消当前任务", "Cancel current task")}</button>
        {runJob.isPending ? <FinalJobStatus startingAction={startingAction} /> : runJob.error ? <FinalJobStatus startingAction={startingAction} submissionError={runJob.error} /> : currentJob ? <FinalJobStatus job={currentJob} startingAction={currentAction} /> : null}
        <div className="final-status-summary"><div><strong>{text("初稿", "Draft")}</strong><StatusPill exists current={payload.draft_approval_current} optional={false} /></div><div><strong>{text("结论", "Conclusion")}</strong><StatusPill exists={Boolean(payload.conclusion_artifact_id)} current={payload.conclusion_current} /></div><div><strong>{text("总览图", "Overview figure")}</strong><StatusPill exists={payload.overview_figure_exists} current={payload.overview_figure_current} /></div><div><strong>{text("最终稿", "Final draft")}</strong><StatusPill exists={Boolean(payload.final_artifact_id)} current={payload.final_current} optional={false} /></div><div><strong>{text("发布", "Release")}</strong><StatusPill exists={Boolean(payload.release?.status)} current={payload.release_current} optional={false} /></div></div>
        {payload.final_draft_docx_exists ? <a className="button button-secondary" href={payload.docx_url} download="final_draft.docx">{text("下载当前DOCX", "Download current DOCX")}</a> : null}
        {payload.final_draft_docx_stale ? <p className="message message-warning">{text("现有Word已过期，请重新生成并下载。", "The existing Word file is stale. Regenerate and download it.")}</p> : null}
      </div></aside>
    </div>{error ? <p className="message message-error">{error.message}</p> : null}</> : null}
  </main>;
}
