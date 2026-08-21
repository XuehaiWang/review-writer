import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
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
  freshness: { source_stale: boolean; review_stale: boolean };
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

function FigureReview({ projectId, onOpenRedraw, opening, openError }: { projectId: string; onOpenRedraw: () => void; opening: boolean; openError?: Error | null }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [selectedPaperId, setSelectedPaperId] = useState("");
  const [note, setNote] = useState("");
  const [representativeRole, setRepresentativeRole] = useState("paper_overview");
  const [selectionComplete, setSelectionComplete] = useState(false);
  const review = useQuery({ queryKey: ["figure-review", projectId], queryFn: () => apiRequest<ReviewPayload>(`/api/v1/projects/${encodeURIComponent(projectId)}/figures/review`) });
  const papers = review.error ? [] : review.data?.papers || [];
  const selected = papers.find((paper) => paper.paper_id === selectedPaperId) || papers[0];
  useEffect(() => {
    if (selected && selected.paper_id !== selectedPaperId) setSelectedPaperId(selected.paper_id);
  }, [selected, selectedPaperId]);
  useEffect(() => setNote(selected?.human_review?.review_note || ""), [selected]);
  useEffect(() => setRepresentativeRole(selected?.human_review?.representative_role || "paper_overview"), [selected]);
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
      <div className="figure-review-grid-react">
        <section className="pane figure-paper-list"><div className="pane-head"><div><span className="step-label">{text("引用论文", "Cited papers")}</span><h2>{reviewed}/{required.length} {text("已选", "selected")}</h2></div></div><div className="paper-list">{papers.map((paper) => { const done = paper.review_required === false || Boolean(paper.human_review?.reviewed_at); return <button key={paper.paper_id} className={paper.paper_id === selected?.paper_id ? "paper-row active" : "paper-row"} type="button" onClick={() => setSelectedPaperId(paper.paper_id)}><span className="paper-row-main"><strong><span title={paper.paper_id}>{paperLabels[paper.paper_id] || paper.paper_id}</span> · {paper.title}</strong><small>{paper.review_required === false ? text("没有可用图", "No usable figure") : done ? text("已选择", "Selected") : text("需要选择", "Selection required")}</small></span><span className={done ? "status-dot ok" : "status-dot warning"} /></button>; })}</div></section>
        <section className="pane candidate-review-pane"><div className="pane-head"><div><span className="step-label">{text("源图候选", "Source candidates")}</span><h2>{selected ? `${paperLabels[selected.paper_id] || selected.paper_id} · ${selected.title}` : text("选择论文", "Select a paper")}</h2></div></div><div className="candidate-grid-react">{selected?.candidates.map((candidate) => { const current = candidate.candidate_index === selected.selected_candidate_index; const image = sourceUrl(candidate); return <article key={candidate.candidate_index} className={current ? "candidate-card-react selected" : "candidate-card-react"}><div><strong>{text("候选", "Candidate")} {candidate.candidate_index}</strong><small>{candidate.source_label || ""} · {text("评分", "score")} {candidate.score ?? "n/a"}</small></div>{image ? <a href={image} target="_blank" rel="noreferrer"><img src={image} alt={`${text("候选", "Candidate")} ${candidate.candidate_index}`} /></a> : <div className="empty-state">{text("图像路径不可用。", "The image path is unavailable.")}</div>}<button className={current ? "button button-secondary" : "button button-primary"} type="button" disabled={save.isPending} onClick={() => save.mutate(Number(candidate.candidate_index))}>{current ? text("已选择", "Selected") : text("使用此候选图", "Use this candidate")}</button></article>; })}</div></section>
        <aside className="pane review-note-pane"><div className="pane-head"><div><span className="step-label">{text("论文级代表性", "Paper-level role")}</span><h2>{text("审核备注", "Review note")}</h2></div></div><div className="gate-body"><label><span>{text("此图代表论文的哪类内容", "What this figure represents")}</span><select value={representativeRole} onChange={(event) => setRepresentativeRole(event.target.value)}><option value="paper_overview">{text("论文总体策略", "Paper overview")}</option><option value="core_transformation">{text("核心转化", "Core transformation")}</option><option value="mechanism">{text("关键机理", "Mechanism")}</option><option value="scope">{text("底物范围或结果", "Scope or results")}</option></select></label><textarea rows={7} value={note} onChange={(event) => setNote(event.target.value)} placeholder={text("选择此图的理由", "Why this figure was selected")} /><p className="muted">{text("这里只锁定来源论文和代表性角色；正文段落位置由当前章节证据自动派生。", "Only the source paper and representative role are fixed here; placement is derived from current section evidence.")}</p>{save.error ? <p className="message message-error">{save.error.message}</p> : null}</div></aside>
      </div>
      <div className="stage-action-bar"><div><strong>{text("源图选择实时同步", "Source selections sync live")}</strong><p>{selectionComplete ? text("全部源图已选好，AI重绘页已同步更新。", "All source figures are selected and the AI redraw workspace is up to date.") : text("每次选择都会立即显示到AI重绘与编辑；可以先处理已选图。", "Every selection appears immediately in AI redraw and editing; selected figures can be processed now.")}</p>{openError ? <span className="message message-error">{openError.message}</span> : null}</div><button className="button button-primary" type="button" disabled={reviewed === 0 || save.isPending || opening} onClick={onOpenRedraw}>{opening ? text("正在同步…", "Syncing…") : text("查看AI重绘与编辑", "Open AI redraw and editing")}</button></div>
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
      return active ? 1000 : false;
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
  const editorRow = rows.find((item) => item.figure_id === editorFigureId);
  const editorOutput = rowImage(editorRow);
  const selectedFigureLabel = selected
    ? displayFigureLabel(String(selected.figure_id || ""), String(selected.paper_id || ""), paperLabels)
    : text("图像", "Figure");
  const selectedRetryable = ["failed", "interrupted", "cancelled"].includes(String(state?.job_status || state?.status || ""));
  const selectedExactRetry = selectedRetryable && state?.origin === "single" && Boolean(state?.job_id);
  const actionError = submit.error || cancel.error || approveOne.error || approveAll.error || setInclusion.error || confirm.error;
  return <>
    {redraw.isPending ? <div className="empty-state">{text("正在加载重绘工作区…", "Loading the redraw workspace…")}</div> : null}
    {redraw.error ? <ErrorState error={redraw.error} onRetry={() => redraw.refetch()} /> : null}
    {payload ? <>
      {payload.freshness.source_stale ? <p className="message message-error">{text("源图候选已过期，请返回源图审核。", "Source candidates are stale. Return to source review.")}</p> : null}
      <div className="redraw-batch-bar"><div><strong>{text("全部AI重绘", "Redraw all with AI")}</strong><p>{payload.batch_redraw.status} · {payload.batch_redraw.completed}/{payload.batch_redraw.total} ({text("成功", "succeeded")} {payload.batch_redraw.succeeded}, {text("失败", "failed")} {payload.batch_redraw.failed})</p>{approveAll.data ? <p role="status">{text(`本次通过 ${approveAll.data.approved_count} 张，已通过 ${approveAll.data.already_approved_count} 张，跳过 ${approveAll.data.skipped_count} 张。`, `Approved ${approveAll.data.approved_count}, already approved ${approveAll.data.already_approved_count}, skipped ${approveAll.data.skipped_count}.`)}</p> : null}</div><button className="button button-primary" type="button" disabled={active || submit.isPending} onClick={() => submit.mutate({ figureIds: candidates.filter((candidate) => candidate.manuscript_selected !== false).map((candidate) => String(candidate.figure_id || "")).filter(Boolean), type: "auto" })}>{text("全部AI重绘", "Redraw all with AI")}</button><button className="button button-secondary" type="button" disabled={!jobIsActive(payload.batch_redraw.status) || cancel.isPending} onClick={() => cancel.mutate()}>{text("全部停止生成", "Stop all generation")}</button><button className="button button-secondary" type="button" disabled={approveAll.isPending} onClick={() => approveAll.mutate()}>{approveAll.isPending ? text("审核中…", "Approving…") : text("全部通过", "Approve all")}</button></div>
      <div className="figure-redraw-grid-react">
        <section className="pane redraw-list-pane"><div className="pane-head"><div><span className="step-label">{text("图像", "Figures")}</span><h2>{candidates.length} {text("张图", "figures")}</h2></div></div><div className="paper-list">{candidates.map((candidate) => { const figureId = String(candidate.figure_id || ""); const paperId = String(candidate.paper_id || ""); const itemState = payload.figure_redraw_states[figureId]; const itemRow = rows.find((candidateRow) => candidateRow.figure_id === candidate.figure_id); const itemOutput = rowImage(itemRow); const submitting = submit.isPending && submit.variables?.figureIds.includes(figureId); const failed = itemState?.status === "failed"; const status = submitting || ["queued", "running", "retrying", "cancel_requested"].includes(String(itemState?.status || "")) ? text("生成中", "Generating") : failed ? text("失败", "Failed") : itemOutput ? approved(itemRow) ? text("已通过", "Approved") : text("已生成", "Generated") : text("未生成", "Not generated"); return <button key={candidate.figure_id} type="button" className={candidate.figure_id === selected?.figure_id ? "paper-row active" : "paper-row"} onClick={() => setSelectedId(figureId)}><span className="paper-row-main"><strong title={figureId}>{displayFigureLabel(figureId, paperId, paperLabels)} · {candidate.source_label || paperLabels[paperId] || paperId}</strong><small>{status}{failed && itemState?.error ? ` · ${itemState.error}` : ""}</small></span><span className={failed ? "status-dot warning" : itemOutput ? "status-dot ok" : "status-dot"} /></button>; })}</div></section>
        <section className="pane redraw-preview-pane"><div className="pane-head"><div><span className="step-label" title={String(selected?.figure_id || "")}>{selectedFigureLabel}</span><h2>{text("源图与重绘对照", "Source and redraw comparison")}</h2></div></div><div className="figure-compare-react"><figure><figcaption>{text("源图候选", "Source Candidate")}</figcaption>{source ? <a href={source} target="_blank" rel="noreferrer"><img src={source} alt={text("源图候选", "Source candidate")} /></a> : <div className="empty-state">{text("源图不可用。", "Source image unavailable.")}</div>}</figure><figure><figcaption>{text("重绘结果", "Redrawn Output")}</figcaption>{output ? <a href={output} target="_blank" rel="noreferrer"><img src={output} alt={text("重绘结果", "Redrawn output")} /></a> : <div className="empty-state">{text("尚未生成。", "Not generated yet.")}</div>}</figure></div><div className="redraw-controls"><button className="button button-primary" type="button" disabled={!selected?.figure_id || ["queued", "running", "retrying"].includes(String(state?.status || "")) || submit.isPending} onClick={() => submit.mutate({ figureIds: [String(selected?.figure_id || "")], type: "auto", retryJobId: selectedRetryable ? state?.job_id : undefined, exactRetry: selectedExactRetry })}>{selectedRetryable ? text("重试自动重绘", "Retry automatic redraw") : text("自动判断并AI重绘", "Classify and redraw with AI")}</button><label>{text("指定图像类型", "Figure type")}<select value={figureType} onChange={(event) => setFigureType(event.target.value)}>{payload.figure_type_options.map((option) => <option key={option.value} value={option.value}>{figureTypeLabel(option.value, language, option.label)}</option>)}</select></label><button className="button button-secondary" type="button" disabled={!selected?.figure_id || submit.isPending} onClick={() => submit.mutate({ figureIds: [String(selected?.figure_id || "")], type: figureType, retryJobId: selectedRetryable ? state?.job_id : undefined })}>{selectedRetryable ? text("按所选类型重试", "Retry with selected type") : text("按所选类型AI重绘", "Redraw selected type with AI")}</button>{output ? <button className="button button-secondary" type="button" disabled={approved(row) || approveOne.isPending} onClick={() => approveOne.mutate()}>{approved(row) ? text("人工审核已通过", "Manually approved") : text("人工审核通过", "Approve manually")}</button> : null}<button className="button button-secondary" type="button" disabled={!selected?.figure_id} onClick={() => setEditorFigureId(String(selected?.figure_id || ""))}>{text("在线编辑 SVG / Ketcher", "Edit SVG / Ketcher online")}</button>{artifactUrl(row?.editable_svg) ? <a className="button button-quiet" href={artifactUrl(row?.editable_svg)} download>{text("下载可编辑SVG", "Download editable SVG")}</a> : null}</div>{state?.error && !submit.isPending ? <p className="message message-error">{text("最近一次生成失败：", "Latest generation failed: ")}{state.error}</p> : null}<div className="figure-details"><p><strong>{text("选择理由", "Why selected")}</strong>{selected?.why_selected || "—"}</p><p><strong>{text("图像内容", "What it shows")}</strong>{selected?.what_it_shows || "—"}</p><p><strong>{text("论点匹配", "Claim fit")}</strong>{selected?.fits_paragraph_or_claim || "—"}</p><p><strong>{text("图注", "Caption")}</strong>{selected?.source_caption_text || "—"}</p></div></section>
        <aside className="pane section-gate-react"><div className="pane-head"><div><span className="step-label">{text("审核门", "Review gate")}</span><h2>{text("完整性审核", "Integrity review")}</h2></div></div><div className="gate-body"><p>{text("可用图", "Usable figures")} {payload.freshness.usable_count}/{payload.freshness.selected_count}</p><ul><li>{text("源图必须来自对应论文。", "The source image must come from the corresponding paper.")}</li><li>{text("不得改变反应、条件、产物和机理。", "Reactions, conditions, products, and mechanisms must remain unchanged.")}</li><li>{text("失败或未生成图不会被“全部通过”。", "Failed or missing outputs are never included in Approve all.")}</li></ul><button className="button button-quiet" type="button" onClick={onBack}>{text("返回源图审核", "Return to source review")}</button></div></aside>
      </div>
      {selected?.figure_id ? <div className="stage-action-bar figure-inclusion-bar"><div><strong>{selected.manuscript_selected === false ? text("此图已从正文排除", "This figure is excluded from the manuscript") : text("正文图像取舍", "Manuscript figure inclusion")}</strong><p>{text("只有你明确排除的失败图才不会阻塞进入初稿；源图选择和其他成功重绘不会改变。", "Only explicitly excluded failed figures stop blocking Draft; source selections and other successful redraws remain unchanged.")}</p></div><button className="button button-secondary" type="button" disabled={setInclusion.isPending} onClick={() => setInclusion.mutate({ figureId: String(selected.figure_id), included: selected.manuscript_selected === false })}>{setInclusion.isPending ? text("保存中…", "Saving…") : selected.manuscript_selected === false ? text("重新纳入正文", "Include again") : text("排除此图", "Exclude figure")}</button></div> : null}
      <div className="stage-action-bar"><div><strong>{text("执行图像并进入初稿", "Confirm figures and enter draft")}</strong><p>{active ? text("仍有图像正在生成。", "Figures are still being generated.") : text(`${payload.freshness.usable_count}/${payload.freshness.selected_count}张图可进入正文。`, `${payload.freshness.usable_count}/${payload.freshness.selected_count} figures can enter the manuscript.`)}</p></div><button className="button button-primary" type="button" disabled={active || confirm.isPending || payload.freshness.usable_count !== payload.freshness.selected_count} onClick={() => confirm.mutate()}>{confirm.isPending ? text("确认中…", "Confirming…") : text("执行图像并进入初稿", "Confirm figures and enter draft")}</button>{actionError ? <span className="message message-error">{actionError.message}</span> : null}</div>
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
  return <main className="workspace page-container workspace-page images-page"><div className="workspace-heading"><div><p className="eyebrow">{text("阶段 5 · 图像工作流", "Stage 5 · Figure workflow")}</p><h1>{text("图像审核与AI重绘", "Figure review and AI redraw")}</h1><p className="muted">{text("源图选择会实时同步到AI重绘与编辑，再生成、在线编辑并人工审核最终输出。", "Source selections sync live to AI redraw and editing, where outputs can be generated, edited, and reviewed.")}</p></div><ProjectSelector /></div><nav className="workspace-step-tabs"><button className={tab === "review" ? "active" : ""} type="button" onClick={() => setTab("review")}>1 {text("源图审核", "Source review")}</button><button className={tab === "redraw" ? "active" : ""} type="button" disabled={!project || openRedraw.isPending} onClick={() => tab === "redraw" ? undefined : openRedraw.mutate()}>2 {openRedraw.isPending ? text("同步中…", "Syncing…") : text("AI重绘与编辑", "AI redraw and editing")}</button></nav>{project ? tab === "review" ? <FigureReview projectId={project.project_id} onOpenRedraw={() => openRedraw.mutate()} opening={openRedraw.isPending} openError={openRedraw.error} /> : <FigureRedraw projectId={project.project_id} onBack={() => setTab("review")} /> : <div className="empty-state">{text("请先选择项目。", "Select a project first.")}</div>}</main>;
}
