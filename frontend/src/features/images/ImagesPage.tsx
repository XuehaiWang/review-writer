import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { ACTIVE_JOB_POLL_INTERVAL_MS } from "../../api/polling";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { displayFigureLabel } from "../../utils/paperLabels";
import { figureTypeLabel } from "./figureTypeLabels";
import { SvgKetcherEditor } from "./SvgKetcherEditor";

type FigureCandidate = Record<string, unknown> & {
  figure_id?: string;
  paper_id?: string;
  title?: string;
  source_label?: string;
  source_image_path?: string;
  source_image_url?: string;
  manuscript_selected?: boolean;
  candidate_index?: number;
  score?: number;
  why_selected?: string;
  what_it_shows?: string;
  fits_paragraph_or_claim?: string;
  source_caption_text?: string;
  recommended_action?: string;
  auto_figure_type?: string;
  automatic_selection_eligible?: boolean;
  candidate_qualification?: { eligible?: boolean; score?: number; minimum_score?: number; reasons?: string[] };
};

type ReviewPaper = {
  paper_id: string;
  title: string;
  candidates: FigureCandidate[];
  review_required: boolean;
  selected_candidate_index?: number;
  human_review?: { reviewed_at?: string; review_note?: string; representative_role?: string };
};

type ReviewPayload = {
  project_id: string;
  papers: ReviewPaper[];
  revision: number;
  status: string;
  paper_display_labels?: Record<string, string>;
  freshness: { source_stale: boolean; review_stale: boolean; redraw_inputs_in_sync?: boolean };
};

type ReviewSelectionResult = {
  revision: number;
  status: string;
  selected_count: number;
  missing_paper_ids: string[];
  selection_complete: boolean;
};

type RedrawRow = Record<string, unknown> & {
  figure_id?: string;
  paper_id?: string;
  status?: string;
  output_path?: string;
  redrawn_path?: string;
  output_image_path?: string;
  redrawn_image?: string;
  image_path?: string;
  path?: string;
  rejected_preview_image?: string;
  editable_svg?: string;
  audit_url?: string;
  render_mode?: string;
  figure_type?: string;
  source_preserved?: boolean;
  ai_redraw_performed?: boolean;
  output_state?: "source_original" | "ai_redrawn" | "manually_edited" | "approved_source_original" | "approved_ai_redrawn" | "approved_manually_edited" | "failed";
  figure_source_kind?: "source_paper" | "multi_paper_synthesis" | "review_generated";
  rights_status?: "source_attributed" | "license_verified" | "permission_unknown" | "original_synthesis";
  source_identity_status?: "verified" | "unresolved" | "not_required";
  source_paper_id?: string | null;
  source_label?: string | null;
  permission_status?: "verified" | "unknown" | "not_required_for_source_reuse";
  manual_edit?: { base_mode?: "source" | "redrawn"; audit_path?: string };
  manual_arrow_edit?: { base_mode?: "source" | "redrawn"; audit_path?: string; editable_svg?: string };
  human_approval?: { status?: string; current_source_match?: boolean; current_output_match?: boolean; current_policy_match?: boolean };
};

type RedrawPayload = {
  project_id: string;
  figure_candidates: FigureCandidate[] | { figures?: FigureCandidate[]; candidates?: FigureCandidate[] };
  redrawn_manifest: { figures?: RedrawRow[]; items?: RedrawRow[]; redrawn_figures?: RedrawRow[] } | RedrawRow[] | null;
  revision: number;
  paper_display_labels?: Record<string, string>;
  batch_redraw: { job_id: string; status: string; total: number; completed: number; succeeded: number; failed: number; errors?: Array<Record<string, unknown>> };
  figure_redraw_states: Record<string, { status?: string; job_id?: string; job_status?: string; origin?: string; error?: string }>;
  figure_type_options: Array<{ value: string; label: string }>;
  source_preservation?: { preserved_count: number; generated_count: number; unprocessed_count: number; all_selected: boolean };
  freshness: { source_stale: boolean; redraw_stale: boolean; selected_count: number; usable_count: number };
  report: { jobs: Job[] };
};

type RedrawSubmission = {
  figureIds: string[];
  type: string;
  retryJobId?: string;
  exactRetry?: boolean;
};

function artifactUrl(value?: unknown) {
  const path = String(value || "");
  return path.startsWith("/api/v1/artifacts/") ? path : "";
}

function sourceUrl(candidate?: FigureCandidate) {
  return artifactUrl(candidate?.source_image_url || candidate?.source_image_path);
}

function candidatesOf(payload?: RedrawPayload) {
  const value = payload?.figure_candidates;
  if (Array.isArray(value)) return value;
  return value?.figures || value?.candidates || [];
}

function rowsOf(payload?: RedrawPayload) {
  const value = payload?.redrawn_manifest;
  if (Array.isArray(value)) return value;
  return value?.figures || value?.items || value?.redrawn_figures || [];
}

function loadRedraw(projectId: string) {
  return apiRequest<RedrawPayload>(`/api/v1/projects/${encodeURIComponent(projectId)}/figures`);
}

function rowImage(row?: RedrawRow) {
  return artifactUrl(row?.output_path || row?.redrawn_path || row?.output_image_path || row?.redrawn_image || row?.image_path || row?.path || row?.rejected_preview_image);
}

function approved(row?: RedrawRow) {
  const approval = row?.human_approval;
  return Boolean(approval?.status === "approved" && approval.current_source_match !== false && approval.current_output_match !== false && approval.current_policy_match !== false);
}

function outputStateLabel(state: RedrawRow["output_state"], text: (zh: string, en: string) => string) {
  const labels: Record<string, string> = {
    source_original: text("来源原图", "Source original"),
    ai_redrawn: text("AI重绘", "AI redrawn"),
    manually_edited: text("人工编辑", "Manually edited"),
    approved_source_original: text("已审核来源原图", "Approved source original"),
    approved_ai_redrawn: text("已审核AI重绘", "Approved AI redraw"),
    approved_manually_edited: text("已审核人工编辑", "Approved manual edit"),
    failed: text("未生成", "Not generated"),
  };
  return labels[String(state || "")] || text("重绘结果", "Redrawn output");
}

function FigureReview({ projectId, onOpenRedraw, opening, openError }: { projectId: string; onOpenRedraw: () => void; opening: boolean; openError?: Error | null }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [selectedPaperId, setSelectedPaperId] = useState("");
  const [note, setNote] = useState("");
  const [representativeRole, setRepresentativeRole] = useState("unknown");
  const [selectionComplete, setSelectionComplete] = useState(false);
  const review = useQuery({ queryKey: ["figure-review", projectId], queryFn: () => apiRequest<ReviewPayload>(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/review`) });
  const papers = review.error ? [] : review.data?.papers || [];
  const selected = papers.find((paper) => paper.paper_id === selectedPaperId) || papers[0];
  useEffect(() => {
    if (selected && selected.paper_id !== selectedPaperId) setSelectedPaperId(selected.paper_id);
  }, [selected, selectedPaperId]);
  useEffect(() => setNote(selected?.human_review?.review_note || ""), [selected]);
  useEffect(() => setRepresentativeRole(selected?.human_review?.representative_role || "unknown"), [selected]);
  useEffect(() => setSelectionComplete(review.data?.status === "approved"), [review.data?.status]);
  const save = useMutation({
    mutationFn: (candidateIndex: number) => apiRequest<ReviewSelectionResult>(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/review/${encodeURIComponent(selected!.paper_id)}`, { method: "PUT", ...jsonBody({ revision: review.data!.revision, candidate_index: candidateIndex, review_note: note.trim(), representative_role: representativeRole }) }),
    onSuccess: async (result) => {
      setSelectionComplete(result.selection_complete);
      queryClient.removeQueries({ queryKey: ["figures", projectId], exact: true });
      await queryClient.invalidateQueries({ queryKey: ["figure-review", projectId] });
    },
  });
  const required = papers.filter((paper) => paper.review_required !== false);
  const reviewed = required.filter((paper) => paper.human_review?.reviewed_at).length;
  const paperLabels = review.data?.paper_display_labels || {};
  return <>
    {review.isPending ? <div className="empty-state">{text("正在加载源图候选…", "Loading source figure candidates…")}</div> : null}
    {review.error ? <ErrorState error={review.error} onRetry={() => review.refetch()} /> : null}
    {review.data && !review.error ? <>
      {review.data.freshness.source_stale ? <p className="message message-error">{text("章节产物已经变化，请重新生成图像候选后再审核。", "Section outputs changed. Regenerate figure candidates before review.")}</p> : null}
      {review.data.freshness.review_stale ? <p className="message message-warning">{text("选图集合需要重新验证。", "The figure selection set must be reviewed again.")}</p> : null}
      {review.data.freshness.redraw_inputs_in_sync === false && reviewed > 0 ? <p className="message message-warning">{text("当前源图选择尚未同步到图像处理工作区。同步会使基于旧候选图的重绘、初稿和终稿过期。", "The current source selections are not yet synchronized with the processing workspace. Synchronizing makes redraws, drafts, and final outputs based on older candidates stale.")}</p> : null}
      <div className="figure-review-grid-react">
        <section className="pane figure-paper-list"><div className="pane-head"><div><span className="step-label">{text("引用论文", "Cited papers")}</span><h2>{reviewed}/{required.length} {text("已选", "selected")}</h2></div></div><div className="paper-list">{papers.map((paper) => { const done = paper.review_required === false || Boolean(paper.human_review?.reviewed_at); return <button key={paper.paper_id} className={paper.paper_id === selected?.paper_id ? "paper-row active" : "paper-row"} type="button" onClick={() => setSelectedPaperId(paper.paper_id)}><span className="paper-row-main"><strong><span title={paper.paper_id}>{paperLabels[paper.paper_id] || paper.paper_id}</span> · {paper.title}</strong><small>{paper.review_required === false ? text("没有可用图", "No usable figure") : done ? text("已选择", "Selected") : text("需要选择", "Selection required")}</small></span><span className={done ? "status-dot ok" : "status-dot warning"} /></button>; })}</div></section>
        <section className="pane candidate-review-pane"><div className="pane-head"><div><span className="step-label">{text("源图候选", "Source candidates")}</span><h2>{selected ? `${paperLabels[selected.paper_id] || selected.paper_id} · ${selected.title}` : text("选择论文", "Select a paper")}</h2></div></div><div className="candidate-grid-react">{selected?.candidates.map((candidate) => { const current = candidate.candidate_index === selected.selected_candidate_index; const image = sourceUrl(candidate); const autoEligible = candidate.automatic_selection_eligible !== false && candidate.candidate_qualification?.eligible !== false; return <article key={candidate.candidate_index} className={current ? "candidate-card-react selected" : "candidate-card-react"}><div><strong>{text("候选", "Candidate")} {candidate.candidate_index}</strong><small>{candidate.source_label || ""} · {text("评分", "score")} {candidate.score ?? "n/a"} · {autoEligible ? text("可自动入选", "Auto-eligible") : text("仅供人工选择", "Manual selection only")}</small></div>{image ? <a href={image} target="_blank" rel="noreferrer"><img src={image} alt={`${text("候选", "Candidate")} ${candidate.candidate_index}`} /></a> : <div className="empty-state">{text("图像路径不可用。", "The image path is unavailable.")}</div>}<button className={current ? "button button-secondary" : "button button-primary"} type="button" disabled={save.isPending} onClick={() => save.mutate(Number(candidate.candidate_index))}>{current ? text("已选择", "Selected") : text("使用此候选图", "Use this candidate")}</button></article>; })}</div></section>
        <aside className="pane review-note-pane"><div className="pane-head"><div><span className="step-label">{text("可选设置", "Optional settings")}</span><h2>{text("审核备注", "Review note")}</h2></div></div><div className="gate-body"><p className="muted">{text("系统会根据图注和正文自动判断图像角色；仅在判断不准确时手动调整。", "The system infers the figure role from its caption and manuscript context. Adjust it only when needed.")}</p><details className="advanced-panel figure-review-advanced"><summary>{text("图像角色与审核备注", "Figure role and review note")}</summary><div className="advanced-panel-body"><label><span>{text("此图支持哪类正文内容", "What manuscript content this figure supports")}</span><select value={representativeRole} onChange={(event) => setRepresentativeRole(event.target.value)}><option value="unknown">{text("自动判断", "Infer automatically")}</option><option value="conceptual_overview">{text("概念总览", "Conceptual overview")}</option><option value="workflow">{text("研究流程", "Workflow")}</option><option value="core_transformation">{text("核心方法或转化", "Core method or transformation")}</option><option value="mechanism_model">{text("机理或模型", "Mechanism or model")}</option><option value="scope_samples">{text("范围或样本", "Scope or samples")}</option><option value="quantitative_results">{text("定量结果", "Quantitative results")}</option><option value="comparison_ablation">{text("比较或消融", "Comparison or ablation")}</option><option value="structure_image">{text("结构或成像", "Structure or imaging")}</option></select></label><textarea rows={7} value={note} onChange={(event) => setNote(event.target.value)} placeholder={text("选择此图的理由", "Why this figure was selected")} /><p className="muted">{text("这里只锁定来源论文和图像角色；正文位置会根据当前章节的语义与证据自动选择。", "Only the source paper and role are fixed here; placement is selected from current evidence using manuscript semantics.")}</p></div></details>{save.error ? <p className="message message-error">{save.error.message}</p> : null}</div></aside>
      </div>
      <div className="stage-action-bar"><div><strong>{text("源图选择实时同步", "Source selections sync live")}</strong><p>{review.data.freshness.redraw_inputs_in_sync === false ? text("这是旧项目或尚未同步的选择；请明确同步后再进入图像处理。", "This is an older or unsynchronized selection set. Synchronize it explicitly before opening image processing.") : selectionComplete ? text("全部源图已选好，AI重绘页已同步更新。", "All source figures are selected and the AI redraw workspace is up to date.") : text("每次选择都会立即显示到AI重绘与编辑；可以先处理已选图。", "Every selection appears immediately in AI redraw and editing; selected figures can be processed now.")}</p>{openError ? <span className="message message-error">{openError.message}</span> : null}</div><button className="button button-primary" type="button" disabled={reviewed === 0 || save.isPending || opening} onClick={onOpenRedraw}>{opening ? text("正在同步…", "Syncing…") : review.data.freshness.redraw_inputs_in_sync === false ? text("同步并查看AI重绘与编辑", "Sync and open AI redraw") : text("查看AI重绘与编辑", "Open AI redraw and editing")}</button></div>
    </> : null}
  </>;
}

function FigureRedraw({ projectId, onBack }: { projectId: string; onBack: () => void }) {
  const { language, text } = useUiText();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [selectedId, setSelectedId] = useState("");
  const [figureType, setFigureType] = useState("auto");
  const [editorFigureId, setEditorFigureId] = useState("");
  const redraw = useQuery({
    queryKey: ["figures", projectId],
    queryFn: () => loadRedraw(projectId),
    refetchInterval: (query) => {
      const value = query.state.data;
      const active = jobIsActive(value?.batch_redraw.status) || Object.values(value?.figure_redraw_states || {}).some((state) => ["queued", "running", "retrying", "cancel_requested"].includes(String(state.status || "")));
      return active ? ACTIVE_JOB_POLL_INTERVAL_MS : false;
    },
    refetchIntervalInBackground: true,
  });
  const payload = redraw.error ? undefined : redraw.data;
  const candidates = candidatesOf(payload);
  const rows = rowsOf(payload);
  const selected = candidates.find((candidate) => candidate.figure_id === selectedId) || candidates[0];
  const row = rows.find((item) => item.figure_id === selected?.figure_id);
  const state = selected?.figure_id ? payload?.figure_redraw_states[selected.figure_id] : undefined;
  useEffect(() => { if (selected?.figure_id && selected.figure_id !== selectedId) setSelectedId(selected.figure_id); }, [selected, selectedId]);
  useEffect(() => setFigureType(String(row?.figure_type || selected?.auto_figure_type || "auto")), [row?.figure_type, selected?.auto_figure_type, selected?.figure_id]);
  const refresh = async () => queryClient.invalidateQueries({ queryKey: ["figures", projectId] });
  const submit = useMutation({
    mutationFn: ({ figureIds, type, retryJobId, exactRetry }: RedrawSubmission) => {
      if (exactRetry && retryJobId) {
        return apiRequest<Job>(`/api/v1/jobs/${encodeURIComponent(retryJobId)}/retry`, { method: "POST" });
      }
      const single = figureIds.length === 1;
      const endpoint = single
        ? `/api/v1/projects/${encodeURIComponent(projectId)}/figures/${encodeURIComponent(figureIds[0])}/jobs`
        : `/api/v1/projects/${encodeURIComponent(projectId)}/figures/jobs`;
      const body = single
        ? { figure_type: type, ...(retryJobId ? { retry_of_job_id: retryJobId } : {}) }
        : { figure_ids: figureIds, figure_type: type };
      return apiRequest<Job>(endpoint, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody(body) });
    },
    onSuccess: refresh,
  });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${encodeURIComponent(payload!.batch_redraw.job_id)}/cancel`, { method: "POST" }), onSuccess: refresh });
  const approveOne = useMutation({ mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/${encodeURIComponent(selected!.figure_id!)}/approve`, { method: "POST" }), onSuccess: refresh });
  const approveAll = useMutation({ mutationFn: () => apiRequest<{ approved_count: number; already_approved_count: number; skipped_count: number }>(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/approve-successful`, { method: "POST" }), onSuccess: refresh });
  const preserveSources = useMutation({
    mutationFn: () => apiRequest<{ preserved_count: number; already_preserved_count: number; retained_generated_count: number; selected_count: number }>(
      `/api/v1/projects/${encodeURIComponent(projectId)}/figures/preserve-sources`,
      { method: "POST" },
    ),
    onSuccess: refresh,
  });
  const setInclusion = useMutation({
    mutationFn: ({ figureId, included }: { figureId: string; included: boolean }) => apiRequest(
      `/api/v1/projects/${encodeURIComponent(projectId)}/figures/${encodeURIComponent(figureId)}/${included ? "include" : "exclude"}`,
      { method: "POST" },
    ),
    onSuccess: refresh,
  });
  const confirm = useMutation({ mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/confirm`, { method: "POST", ...jsonBody({ revision: payload!.revision }) }), onSuccess: () => navigate(`/draft?project=${encodeURIComponent(projectId)}`) });
  const active = Boolean(payload && (jobIsActive(payload.batch_redraw.status) || Object.values(payload.figure_redraw_states).some((item) => ["queued", "running", "retrying", "cancel_requested"].includes(String(item.status || "")))));
  const paperLabels = payload?.paper_display_labels || {};
  const source = sourceUrl(selected);
  const output = rowImage(row);
  const outputLabel = outputStateLabel(row?.output_state, text);
  const editorRow = rows.find((item) => item.figure_id === editorFigureId);
  const editorOutput = rowImage(editorRow);
  const selectedFigureLabel = selected
    ? displayFigureLabel(String(selected.figure_id || ""), String(selected.paper_id || ""), paperLabels)
    : text("图像", "Figure");
  const selectedRetryable = ["failed", "interrupted", "cancelled"].includes(String(state?.job_status || state?.status || ""));
  const selectedExactRetry = selectedRetryable && state?.origin === "single" && Boolean(state?.job_id);
  const actionError = submit.error || cancel.error || approveOne.error || approveAll.error || preserveSources.error || setInclusion.error || confirm.error;
  return <>
    {redraw.isPending ? <div className="empty-state">{text("正在加载重绘工作区…", "Loading the redraw workspace…")}</div> : null}
    {redraw.error ? <ErrorState error={redraw.error} onRetry={() => redraw.refetch()} /> : null}
    {payload ? <>
      {payload.freshness.source_stale ? <p className="message message-error">{text("源图候选已过期，请返回源图审核。", "Source candidates are stale. Return to source review.")}</p> : null}
      <div className="redraw-batch-bar">
        <div>
          <strong>{text("批量图像处理", "Batch figure processing")}</strong>
          <p>{payload.batch_redraw.status} · {payload.batch_redraw.completed}/{payload.batch_redraw.total} ({text("成功", "succeeded")} {payload.batch_redraw.succeeded}, {text("失败", "failed")} {payload.batch_redraw.failed})</p>
          {payload.source_preservation?.preserved_count ? <p role="status">{text(`已有 ${payload.source_preservation.preserved_count} 张未重绘图使用原图。`, `${payload.source_preservation.preserved_count} unredrawn figures use their source images.`)}</p> : null}
          {payload.source_preservation?.generated_count ? <p role="status">{text(`已有 ${payload.source_preservation.generated_count} 张重绘或编辑结果，将保持不变。`, `${payload.source_preservation.generated_count} redraw or edit outputs remain unchanged.`)}</p> : null}
          {preserveSources.data ? <p role="status">{text(`本次将 ${preserveSources.data.preserved_count} 张未处理图保存为原图，保留 ${preserveSources.data.retained_generated_count} 张现有重绘结果。`, `Saved ${preserveSources.data.preserved_count} unprocessed figures as source images and kept ${preserveSources.data.retained_generated_count} existing redraws.`)}</p> : null}
          {approveAll.data ? <p role="status">{text(`本次通过 ${approveAll.data.approved_count} 张，已通过 ${approveAll.data.already_approved_count} 张，跳过 ${approveAll.data.skipped_count} 张。`, `Approved ${approveAll.data.approved_count}, already approved ${approveAll.data.already_approved_count}, skipped ${approveAll.data.skipped_count}.`)}</p> : null}
        </div>
        <button
          className="button button-secondary"
          type="button"
          disabled={active || preserveSources.isPending || payload.source_preservation?.unprocessed_count === 0 || candidates.filter((candidate) => candidate.manuscript_selected !== false).length === 0}
          onClick={() => {
            if (window.confirm(text("仅将尚未生成的图保存为原图；已有AI重绘或人工编辑结果保持不变。是否继续？", "Save only figures without an output as source images? Existing AI redraws and manual edits will remain unchanged."))) preserveSources.mutate();
          }}
        >
          {preserveSources.isPending ? text("正在保存原图…", "Saving source images…") : payload.source_preservation?.unprocessed_count === 0 ? text("没有待处理图", "No unprocessed figures") : text(`未重绘图全部保留原图（${payload.source_preservation?.unprocessed_count ?? 0}）`, `Retain unredrawn sources (${payload.source_preservation?.unprocessed_count ?? 0})`)}
        </button>
        <button className="button button-primary" type="button" disabled={active || submit.isPending} onClick={() => submit.mutate({ figureIds: candidates.filter((candidate) => candidate.manuscript_selected !== false).map((candidate) => String(candidate.figure_id || "")).filter(Boolean), type: "auto" })}>{text("全部AI重绘", "Redraw all with AI")}</button>
        <button className="button button-secondary" type="button" disabled={!jobIsActive(payload.batch_redraw.status) || cancel.isPending} onClick={() => cancel.mutate()}>{text("全部停止生成", "Stop all generation")}</button>
        <button className="button button-secondary" type="button" disabled={approveAll.isPending} onClick={() => approveAll.mutate()}>{approveAll.isPending ? text("审核中…", "Approving…") : text("全部通过", "Approve all")}</button>
      </div>
      <div className="figure-redraw-grid-react">
        <section className="pane redraw-list-pane">
          <div className="pane-head"><div><span className="step-label">{text("图像", "Figures")}</span><h2>{candidates.length} {text("张图", "figures")}</h2></div></div>
          <div className="paper-list">
            {candidates.map((candidate) => {
              const figureId = String(candidate.figure_id || "");
              const paperId = String(candidate.paper_id || "");
              const itemState = payload.figure_redraw_states[figureId];
              const itemRow = rows.find((candidateRow) => candidateRow.figure_id === candidate.figure_id);
              const itemOutput = rowImage(itemRow);
              const submitting = submit.isPending && submit.variables?.figureIds.includes(figureId);
              const generating = submitting || ["queued", "running", "retrying", "cancel_requested"].includes(String(itemState?.status || ""));
              const sourcePreserved = Boolean(itemRow?.source_preserved);
              const failed = !sourcePreserved && itemState?.status === "failed";
              const status = generating
                ? text("生成中", "Generating")
                : sourcePreserved
                  ? text("已保留原图", "Source retained")
                  : failed
                    ? text("失败", "Failed")
                    : itemOutput
                      ? approved(itemRow) ? text("已通过", "Approved") : text("已生成", "Generated")
                      : text("未生成", "Not generated");
              return <button key={candidate.figure_id} type="button" className={candidate.figure_id === selected?.figure_id ? "paper-row active" : "paper-row"} onClick={() => setSelectedId(figureId)}><span className="paper-row-main"><strong title={figureId}>{displayFigureLabel(figureId, paperId, paperLabels)} · {candidate.source_label || paperLabels[paperId] || paperId}</strong><small>{status}{failed && itemState?.error ? ` · ${itemState.error}` : ""}</small></span><span className={failed ? "status-dot warning" : itemOutput ? "status-dot ok" : "status-dot"} /></button>;
            })}
          </div>
        </section>
        <section className="pane redraw-preview-pane"><div className="pane-head"><div><span className="step-label" title={String(selected?.figure_id || "")}>{selectedFigureLabel}</span><h2>{row?.source_preserved ? text("原图保存预览", "Retained source preview") : text("源图与重绘对照", "Source and redraw comparison")}</h2></div></div><div className="figure-compare-react"><figure><figcaption>{text("源图候选", "Source Candidate")}</figcaption>{source ? <a href={source} target="_blank" rel="noreferrer"><img src={source} alt={text("源图候选", "Source candidate")} /></a> : <div className="empty-state">{text("源图不可用。", "Source image unavailable.")}</div>}</figure><figure><figcaption>{outputLabel}</figcaption>{output ? <a href={output} target="_blank" rel="noreferrer"><img src={output} alt={outputLabel} /></a> : <div className="empty-state">{text("尚未生成。", "Not generated yet.")}</div>}</figure></div><div className="redraw-controls"><button className="button button-primary" type="button" disabled={!selected?.figure_id || ["queued", "running", "retrying"].includes(String(state?.status || "")) || submit.isPending} onClick={() => submit.mutate({ figureIds: [String(selected?.figure_id || "")], type: "auto", retryJobId: selectedRetryable ? state?.job_id : undefined, exactRetry: selectedExactRetry })}>{selectedRetryable ? text("重试自动重绘", "Retry automatic redraw") : text("自动判断并AI重绘", "Classify and redraw with AI")}</button>{output && !row?.source_preserved ? <button className="button button-secondary" type="button" disabled={approved(row) || approveOne.isPending} onClick={() => approveOne.mutate()}>{approved(row) ? text("人工审核已通过", "Manually approved") : text("人工审核通过", "Approve manually")}</button> : null}<details className="advanced-panel redraw-advanced-tools"><summary>{text("高级重绘与在线编辑", "Advanced redraw and online editing")}</summary><div className="advanced-panel-body advanced-action-grid"><label>{text("指定图像类型", "Figure type")}<select value={figureType} onChange={(event) => setFigureType(event.target.value)}>{payload.figure_type_options.map((option) => <option key={option.value} value={option.value}>{figureTypeLabel(option.value, language, option.label)}</option>)}</select></label><button className="button button-secondary" type="button" disabled={!selected?.figure_id || submit.isPending} onClick={() => submit.mutate({ figureIds: [String(selected?.figure_id || "")], type: figureType, retryJobId: selectedRetryable ? state?.job_id : undefined })}>{selectedRetryable ? text("按所选类型重试", "Retry with selected type") : text("按所选类型AI重绘", "Redraw selected type with AI")}</button><button className="button button-secondary" type="button" disabled={!selected?.figure_id} onClick={() => setEditorFigureId(String(selected?.figure_id || ""))}>{text("在线编辑 SVG / Ketcher", "Edit SVG / Ketcher online")}</button>{artifactUrl(row?.editable_svg) ? <a className="button button-quiet" href={artifactUrl(row?.editable_svg)} download>{text("下载可编辑SVG", "Download editable SVG")}</a> : null}</div></details></div>{state?.error && !submit.isPending && !row?.source_preserved ? <p className="message message-error">{text("最近一次生成失败：", "Latest generation failed: ")}{state.error}</p> : null}<details className="advanced-panel figure-details-panel"><summary>{text("查看选图依据、图注与权利状态", "View rationale, caption, and rights status")}</summary><div className="figure-details"><p><strong>{text("选择理由", "Why selected")}</strong>{selected?.why_selected || "—"}</p><p><strong>{text("图像内容", "What it shows")}</strong>{selected?.what_it_shows || "—"}</p><p><strong>{text("论点匹配", "Claim fit")}</strong>{selected?.fits_paragraph_or_claim || "—"}</p><p><strong>{text("图注", "Caption")}</strong>{selected?.source_caption_text || "—"}</p><p><strong>{text("权利状态", "Rights status")}</strong>{row?.rights_status === "license_verified" ? text("复用许可已核验", "Reuse licence verified") : row?.rights_status === "original_synthesis" ? text("系统独立综合图", "Original synthesis") : row?.rights_status === "source_attributed" ? text("已注明来源，复用许可未知", "Source attributed; reuse permission unknown") : text("许可状态未知", "Permission unknown")}</p></div></details></section>
        <aside className="pane section-gate-react"><div className="pane-head"><div><span className="step-label">{text("审核门", "Review gate")}</span><h2>{text("完整性审核", "Integrity review")}</h2></div></div><div className="gate-body"><p>{text("可用图", "Usable figures")} {payload.freshness.usable_count}/{payload.freshness.selected_count}</p><ul><li>{text("源图必须来自对应论文。", "The source image must come from the corresponding paper.")}</li><li>{text("不得改变反应、条件、产物和机理。", "Reactions, conditions, products, and mechanisms must remain unchanged.")}</li><li>{text("失败或未生成图不会被“全部通过”。", "Failed or missing outputs are never included in Approve all.")}</li></ul><button className="button button-quiet" type="button" onClick={onBack}>{text("返回源图审核", "Return to source review")}</button></div></aside>
      </div>
      {selected?.figure_id ? <div className="stage-action-bar figure-inclusion-bar"><div><strong>{selected.manuscript_selected === false ? text("此图已从论文图像池排除", "This figure is excluded from the paper asset pool") : text("论文级图像池", "Paper-level figure pool")}</strong><p>{text("纳入表示该图可以代表来源论文；系统会在组稿时只选择有安全段落位置和明确论证作用的子集。", "Inclusion means the figure can represent its source paper. Assembly inserts only the subset with a safe paragraph placement and a clear argumentative role.")}</p></div><button className="button button-secondary" type="button" disabled={setInclusion.isPending} onClick={() => setInclusion.mutate({ figureId: String(selected.figure_id), included: selected.manuscript_selected === false })}>{setInclusion.isPending ? text("保存中…", "Saving…") : selected.manuscript_selected === false ? text("重新纳入图像池", "Include in pool") : text("从图像池排除", "Exclude from pool")}</button></div> : null}
      <div className="stage-action-bar"><div><strong>{text("确认图像池并进入初稿", "Confirm figure pool and enter draft")}</strong><p>{active ? text("仍有图像正在生成。", "Figures are still being generated.") : text(`${payload.freshness.usable_count}/${payload.freshness.selected_count}张图已可加入论文级图像池；未找到安全段落位置的图会保留但不会强制插入。`, `${payload.freshness.usable_count}/${payload.freshness.selected_count} figures are ready for the paper-level pool. Assets without a safe paragraph placement are retained but not forced into the manuscript.`)}</p></div><button className="button button-primary" type="button" disabled={active || confirm.isPending || payload.freshness.usable_count !== payload.freshness.selected_count} onClick={() => confirm.mutate()}>{confirm.isPending ? text("确认中…", "Confirming…") : text("确认图像池并进入初稿", "Confirm figure pool and enter draft")}</button>{actionError ? <span className="message message-error">{actionError.message}</span> : null}</div>
      {editorFigureId ? <SvgKetcherEditor projectId={projectId} figureId={editorFigureId} displayFigureId={selectedFigureLabel} row={editorRow} hasRedrawnBase={Boolean(editorOutput)} initialBaseMode={editorOutput ? "redrawn" : "source"} onClose={() => setEditorFigureId("")} onSaved={refresh} /> : null}
    </> : null}
  </>;
}

export function ImagesPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { selected: project } = useSelectedProject();
  const tab = searchParams.get("tab") === "redraw" ? "redraw" : "review";
  const setTab = (value: "review" | "redraw") => { const next = new URLSearchParams(searchParams); next.set("tab", value); setSearchParams(next); };
  const openRedraw = useMutation({
    mutationFn: async () => {
      if (!project) throw new Error(text("请先选择项目。", "Select a project first."));
      const projectId = project.project_id;
      const review = await queryClient.fetchQuery({
        queryKey: ["figure-review", projectId],
        queryFn: () => apiRequest<ReviewPayload>(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/review`),
      });
      return apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/review/sync`, {
        method: "POST",
        ...jsonBody({ revision: review.revision }),
      });
    },
    onSuccess: async () => {
      if (!project) return;
      const projectId = project.project_id;
      queryClient.removeQueries({ queryKey: ["figures", projectId], exact: true });
      await queryClient.invalidateQueries({ queryKey: ["figure-review", projectId] });
      await queryClient.prefetchQuery({ queryKey: ["figures", projectId], queryFn: () => loadRedraw(projectId) });
      setTab("redraw");
    },
  });
  return <main className="workspace page-container workspace-page images-page"><div className="workspace-heading"><div><p className="eyebrow">{text("阶段 5 · 图像工作流", "Stage 5 · Figure workflow")}</p><h1>{text("图像审核与处理", "Figure review and processing")}</h1><p className="muted">{text("源图选择会实时同步；可整批保留原图，也可继续AI重绘、在线编辑和人工审核。", "Source selections sync live; retain all originals or continue with AI redraw, online editing, and review.")}</p></div><ProjectSelector /></div><nav className="workspace-step-tabs"><button className={tab === "review" ? "active" : ""} type="button" onClick={() => setTab("review")}>1 {text("源图审核", "Source review")}</button><button className={tab === "redraw" ? "active" : ""} type="button" disabled={!project || openRedraw.isPending} onClick={() => tab === "redraw" ? undefined : openRedraw.mutate()}>2 {openRedraw.isPending ? text("检查并同步中…", "Checking and syncing…") : text("进入图像处理与编辑", "Open processing and editing")}</button></nav>{project ? tab === "review" ? <FigureReview projectId={project.project_id} onOpenRedraw={() => openRedraw.mutate()} opening={openRedraw.isPending} openError={openRedraw.error} /> : <FigureRedraw projectId={project.project_id} onBack={() => setTab("review")} /> : <div className="empty-state">{text("请先选择项目。", "Select a project first.")}</div>}</main>;
}
