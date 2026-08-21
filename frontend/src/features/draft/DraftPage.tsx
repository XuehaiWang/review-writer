import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { ACTIVE_JOB_POLL_INTERVAL_MS, PUBLICATION_POLL_INTERVAL_MS } from "../../api/polling";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { MarkdownView } from "../../components/MarkdownView";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { DraftJobStatus } from "./DraftJobStatus";
import { readDraftJobId, writeDraftJobId } from "./draftJobPersistence";
import { preferredDraftJobId, restorableDraftJobId, serverJobToRemember } from "./draftJobSelection";
import { draftPublicationIsPending } from "./draftPublicationSync";
import { hardGateDetails, type HardGateFinding } from "./hardGateDetails";

type Paragraph = { paragraph_id: string; text: string };
type ParagraphImage = { figure_id: string; artifact_id: string; url: string };
type QualityIssue = Record<string, unknown> & { issue_id?: string; severity?: string; paragraph_id?: string; message?: string; diagnosis?: string; score?: number; route?: string; failed_dimensions?: string[]; paragraph?: { paragraph_id: string; text: string; images: ParagraphImage[] } };
type IncrementalEvaluation = { paragraph_id: string; old_paragraph_score: number; new_paragraph_score: number; previous_overall_score: number; updated_overall_score: number; evaluated_at?: string };
type RewriteCandidate = {
  candidate_id: string;
  paragraph_id: string;
  original_text: string;
  candidate_text: string;
  status: string;
  source_paragraph_score?: number;
  candidate_paragraph_score?: number;
  route?: string;
  rewrite_mode?: string;
  requires_manual_confirmation?: boolean;
};
type OptimizationChange = { paragraph_id: string; original_text: string; candidate_text: string; source_paragraph_score?: number; candidate_paragraph_score?: number; score_delta?: number; overall_score_delta?: number };
type OptimizationProposal = { proposal_id: string; source_score: number; candidate_score: number; changes: OptimizationChange[]; status: string; created_at: string };
type DraftVersion = { artifact_id: string; current: boolean; operation: string; created_at: string };
type DraftPayload = {
  project_id: string;
  revision: number;
  status: string;
  draft_artifact_id: string;
  first_draft_md: string;
  paragraphs: Paragraph[];
  quality: Record<string, unknown> & { score?: number; goal?: number; paragraph_goal?: number; issues?: QualityIssue[]; hard_gate_failures?: string[]; incremental_evaluations?: IncrementalEvaluation[]; current?: boolean; status?: string };
  quality_artifact_id: string;
  rewrite_candidates: RewriteCandidate[];
  optimization_proposals: OptimizationProposal[];
  rewrite_states: Record<string, { status?: string; job_id?: string; error?: string }>;
  active_feedback_job_id: string;
  active_feedback_job_type: string;
  latest_feedback_job_id: string;
  latest_feedback_job_type: string;
  latest_feedback_job_status: string;
  draft_approval: Record<string, unknown>;
  draft_approval_current: boolean;
  versions: DraftVersion[];
  freshness: { upstream_stale: boolean; editing_blocked: boolean; stale: boolean };
};

type DraftTab = "preview" | "edit" | "quality" | "approval" | "history";

export function PendingRewrite({ candidate, decide, disabled }: { candidate?: RewriteCandidate; decide: (id: string, decision: "accept" | "reject") => void; disabled: boolean }) {
  const { text } = useUiText();
  if (!candidate) return null;
  const sourceScore = Number(candidate.source_paragraph_score);
  const candidateScore = Number(candidate.candidate_paragraph_score);
  const scored = Number.isFinite(sourceScore) && Number.isFinite(candidateScore);
  return <section className="rewrite-candidate-react">
    <header className="rewrite-candidate-score">
      <div><span>{text("原段分数", "Original score")}</span><strong>{scored ? sourceScore.toFixed(1) : "—"}</strong></div>
      <b aria-hidden="true">→</b>
      <div><span>{text("候选分数", "Candidate score")}</span><strong>{scored ? candidateScore.toFixed(1) : "—"}</strong></div>
      <em>{scored ? text("候选已自动完成单段评分", "Candidate paragraph was scored automatically") : text("旧候选尚无预评分，保存时将使用兼容流程", "This legacy candidate has no precomputed score; saving uses the compatibility flow")}</em>
    </header>
    <div><h4>{text("原文", "Original")}</h4><p>{candidate.original_text}</p></div>
    <div><h4>{text("重写候选", "Rewrite candidate")}</h4><p>{candidate.candidate_text}</p></div>
    <p className="rewrite-candidate-note muted">{text("正文尚未改变。保存时直接采用上方已评分候选，并按分差增量更新全文分数，不再调用模型复评。", "The draft is still unchanged. Saving uses the scored candidate above and updates the overall score by its delta without another model evaluation.")}</p>
    {candidate.requires_manual_confirmation ? <p className="message message-warning">{text("此候选只改善表达，没有解决原评估中的来源或图文身份冲突。保存后该问题仍需人工核对，不能视为已完成来源确认。", "This candidate improves wording only; it does not resolve the source or figure-identity conflict in the evaluation. Manual confirmation is still required after saving.")}</p> : null}
    <footer><button className="button button-primary" type="button" disabled={disabled} onClick={() => decide(candidate.candidate_id, "accept")}>{text("保存此候选", "Save candidate")}</button><button className="button button-secondary" type="button" disabled={disabled} onClick={() => decide(candidate.candidate_id, "reject")}>{text("放弃候选", "Discard candidate")}</button></footer>
  </section>;
}

export function OptimizationProposalReview({ proposal, decide, disabled }: { proposal?: OptimizationProposal; decide: (id: string, decision: "accept" | "reject", selectedParagraphIds?: string[]) => void; disabled: boolean }) {
  const { text } = useUiText();
  const [selected, setSelected] = useState<string[]>([]);
  useEffect(() => {
    setSelected(proposal?.changes.map((change) => change.paragraph_id) || []);
  }, [proposal?.proposal_id]);
  if (!proposal) return null;
  const selectedSet = new Set(selected);
  const selectedScore = Math.max(0, Math.min(100,
    Number(proposal.source_score || 0) + proposal.changes
      .filter((change) => selectedSet.has(change.paragraph_id))
      .reduce((total, change) => total + Number(change.overall_score_delta || 0), 0),
  ));
  const displayedCandidateScore = proposal.changes.every((change) => Number.isFinite(Number(change.overall_score_delta)))
    ? selectedScore
    : selected.length === proposal.changes.length
      ? Number(proposal.candidate_score || 0)
      : Number(proposal.source_score || 0);
  const toggle = (paragraphId: string) => setSelected((current) => current.includes(paragraphId) ? current.filter((value) => value !== paragraphId) : [...current, paragraphId]);
  return <section className="optimization-proposal-review">
    <header><div><span>{text("待人工审核", "Awaiting human review")}</span><h3>{text("批量安全优化对比", "Batch safe-optimization comparison")}</h3></div><strong>{Number(proposal.source_score || 0).toFixed(1)} → {displayedCandidateScore.toFixed(1)}</strong></header>
    <p className="muted">{text(`循环保留了 ${proposal.changes.length} 个通过完整性校验且单段复评提分的候选。正文尚未改变，请勾选要保存的段落；未勾选段落会保留原文。`, `The loop retained ${proposal.changes.length} candidates that passed integrity checks and improved in paragraph-level re-evaluation. The draft is still unchanged; select the paragraphs to save and unchecked paragraphs will keep their originals.`)}</p>
    <div className="optimization-selection-tools"><span>{text(`已选 ${selected.length}/${proposal.changes.length}`, `${selected.length}/${proposal.changes.length} selected`)}</span><button className="button button-quiet" type="button" disabled={disabled || selected.length === proposal.changes.length} onClick={() => setSelected(proposal.changes.map((change) => change.paragraph_id))}>{text("全选", "Select all")}</button><button className="button button-quiet" type="button" disabled={disabled || !selected.length} onClick={() => setSelected([])}>{text("清空选择", "Clear")}</button></div>
    <div className="optimization-change-list">{proposal.changes.map((change) => <article key={change.paragraph_id} className={selectedSet.has(change.paragraph_id) ? "selected" : ""}>
      <div className="optimization-change-heading"><label><input type="checkbox" checked={selectedSet.has(change.paragraph_id)} onChange={() => toggle(change.paragraph_id)} /> <strong>{change.paragraph_id}</strong></label><span>{Number.isFinite(Number(change.source_paragraph_score)) && Number.isFinite(Number(change.candidate_paragraph_score)) ? `${Number(change.source_paragraph_score).toFixed(1)} → ${Number(change.candidate_paragraph_score).toFixed(1)}` : text("已通过单段复评", "Paragraph re-evaluated")}</span></div>
      <div className="optimization-comparison"><div><strong>{text("当前原文", "Current original")}</strong><p>{change.original_text}</p></div><div><strong>{text("优化候选", "Optimized candidate")}</strong><p>{change.candidate_text}</p></div></div>
    </article>)}</div>
    <footer><button className="button button-primary" type="button" disabled={disabled || !selected.length} onClick={() => decide(proposal.proposal_id, "accept", selected)}>{selected.length === proposal.changes.length ? text("保存全部优化", "Save all optimizations") : text(`保存选中的 ${selected.length} 段`, `Save ${selected.length} selected`)}</button><button className="button button-secondary" type="button" disabled={disabled} onClick={() => decide(proposal.proposal_id, "reject")}>{text("放弃本批", "Discard batch")}</button></footer>
  </section>;
}

export function DraftPage() {
  const { text } = useUiText();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const { selected: project } = useSelectedProject();
  const [tab, setTab] = useState<DraftTab>("preview");
  const [draftText, setDraftText] = useState("");
  const [goal, setGoal] = useState(90);
  const [paragraphGoal, setParagraphGoal] = useState(85);
  const [maxIterations, setMaxIterations] = useState(3);
  const [minCaseWords, setMinCaseWords] = useState(140);
  const [maxCaseWords, setMaxCaseWords] = useState(280);
  const [selectedJob, setSelectedJob] = useState({ projectId: "", jobId: "" });
  const [editingParagraph, setEditingParagraph] = useState("");
  const [paragraphText, setParagraphText] = useState("");
  const [qualityFocusParagraph, setQualityFocusParagraph] = useState("");
  const autoAssembleAttempted = useRef("");
  const draft = useQuery({
    queryKey: ["draft", project?.project_id || ""],
    queryFn: () => apiRequest<DraftPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft`),
    enabled: Boolean(project),
    refetchInterval: (query) => query.state.data?.active_feedback_job_id ? ACTIVE_JOB_POLL_INTERVAL_MS : false,
    refetchIntervalInBackground: true,
  });
  const payload = draft.data;
  const localJobId = selectedJob.projectId === project?.project_id ? selectedJob.jobId : "";
  const storedJobId = readDraftJobId(project?.project_id || "");
  const requestedJobId = searchParams.get("job") || "";
  const recoveryJobId = /^[0-9a-f-]{36}$/i.test(requestedJobId) ? requestedJobId : "";
  const latestRestorableJobId = restorableDraftJobId(
    payload?.latest_feedback_job_id,
    payload?.latest_feedback_job_status,
  );
  const storedRestorableJobId = payload
    && storedJobId === latestRestorableJobId
    ? storedJobId
    : "";
  const currentJobId = preferredDraftJobId({
    activeServerJobId: payload?.active_feedback_job_id,
    recoveryJobId,
    localJobId,
    latestServerJobId: latestRestorableJobId,
    storedJobId: storedRestorableJobId,
  });
  const polledJob = useJob(currentJobId);
  const publicationPending = draftPublicationIsPending(polledJob.data, payload);
  const refresh = async () => queryClient.invalidateQueries({ queryKey: ["draft", project?.project_id || ""] });
  const rememberJob = (id: string) => {
    if (!project?.project_id || !id) return;
    writeDraftJobId(project.project_id, id);
    setSelectedJob({ projectId: project.project_id, jobId: id });
  };
  useEffect(() => {
    if (!project?.project_id) return;
    setSelectedJob((current) => {
      const locallySelectedJobId = current.projectId === project.project_id ? current.jobId : "";
      const jobId = serverJobToRemember({
        activeServerJobId: payload?.active_feedback_job_id,
        latestServerJobId: latestRestorableJobId,
        locallySelectedJobId,
      });
      if (!jobId || locallySelectedJobId === jobId) return current;
      writeDraftJobId(project.project_id, jobId);
      return { projectId: project.project_id, jobId };
    });
  }, [payload?.active_feedback_job_id, latestRestorableJobId, project?.project_id]);
  useEffect(() => {
    if (!project?.project_id || !recoveryJobId) return;
    writeDraftJobId(project.project_id, recoveryJobId);
    setSelectedJob({ projectId: project.project_id, jobId: recoveryJobId });
    const next = new URLSearchParams(searchParams);
    next.delete("job");
    setSearchParams(next, { replace: true });
  }, [project?.project_id, recoveryJobId]);
  useEffect(() => setDraftText(payload?.first_draft_md || ""), [payload?.draft_artifact_id, payload?.first_draft_md]);
  useEffect(() => {
    const job = polledJob.data;
    if (!job || jobIsActive(job.status)) return;
    if (job.status === "succeeded") {
      void queryClient.refetchQueries({ queryKey: ["draft", project?.project_id || ""], type: "active" });
    }
  }, [polledJob.data?.status]);
  useEffect(() => {
    if (!publicationPending || !project?.project_id) return;
    const refetchPublishedDraft = () => {
      void queryClient.refetchQueries({ queryKey: ["draft", project.project_id], type: "active" });
    };
    refetchPublishedDraft();
    const timer = window.setInterval(refetchPublishedDraft, PUBLICATION_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [publicationPending, project?.project_id, queryClient]);

  const assemble = useMutation({ mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/assemble`, { method: "POST" }), onSuccess: refresh });
  useEffect(() => {
    if (!project || draft.isPending || !payload || payload.draft_artifact_id || payload.freshness.upstream_stale || assemble.isPending) return;
    if (autoAssembleAttempted.current === project.project_id) return;
    autoAssembleAttempted.current = project.project_id;
    assemble.mutate();
  }, [assemble.isPending, draft.isPending, payload, project]);
  const saveFull = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft`, { method: "PUT", ...jsonBody({ revision: payload!.revision, text: draftText }) }),
    onSuccess: refresh,
  });
  const saveParagraph = useMutation({
    mutationFn: ({ paragraphId, text }: { paragraphId: string; text: string }) => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/paragraphs/${encodeURIComponent(paragraphId)}`, { method: "PUT", ...jsonBody({ revision: payload!.revision, text }) }),
    onSuccess: async () => { setEditingParagraph(""); await refresh(); },
  });
  const evaluate = useMutation({
    mutationFn: async () => {
      if (draftText.trim() !== payload!.first_draft_md.trim()) {
        await apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft`, { method: "PUT", ...jsonBody({ revision: payload!.revision, text: draftText }) });
        await refresh();
      }
      return apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/evaluation-jobs`, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody({ goal, paragraph_goal: paragraphGoal, max_iterations: maxIterations, min_case_words: minCaseWords, max_case_words: maxCaseWords }) });
    },
    onSuccess: (job) => { rememberJob(job.id); setTab("quality"); void refresh(); },
  });
  const optimize = useMutation({
    mutationFn: async () => {
      if (draftText.trim() !== payload!.first_draft_md.trim()) {
        await apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft`, { method: "PUT", ...jsonBody({ revision: payload!.revision, text: draftText }) });
        await refresh();
      }
      return apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/optimization-jobs`, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody({ goal, paragraph_goal: paragraphGoal, max_iterations: maxIterations, min_case_words: minCaseWords, max_case_words: maxCaseWords }) });
    },
    onSuccess: (job) => { rememberJob(job.id); setTab("quality"); void refresh(); },
  });
  const rewrite = useMutation({
    mutationFn: (paragraphId: string) => apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/paragraphs/${encodeURIComponent(paragraphId)}/rewrite-jobs`, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody({}) }),
    onSuccess: (job) => { rememberJob(job.id); setTab("quality"); void refresh(); },
  });
  const decide = useMutation({
    mutationFn: async ({ candidateId, decision }: { candidateId: string; decision: "accept" | "reject" }): Promise<{ decision: "accept" | "reject"; job?: Job }> => {
      if (decision === "accept") {
        const job = await apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/rewrite-candidates/${encodeURIComponent(candidateId)}/accept-jobs`, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody({ revision: payload!.revision }) });
        return { decision, job };
      }
      await apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/rewrite-candidates/${encodeURIComponent(candidateId)}/reject`, { method: "POST", ...jsonBody({ revision: payload!.revision }) });
      return { decision };
    },
    onSuccess: ({ job }) => { if (job) rememberJob(job.id); setTab("quality"); void refresh(); },
  });
  const decideOptimization = useMutation({
    mutationFn: ({ proposalId, decision, selectedParagraphIds = [] }: { proposalId: string; decision: "accept" | "reject"; selectedParagraphIds?: string[] }) => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/optimization-proposals/${encodeURIComponent(proposalId)}/${decision}`, { method: "POST", ...jsonBody({ revision: payload!.revision, selected_paragraph_ids: selectedParagraphIds }) }),
    onSuccess: refresh,
  });
  const restore = useMutation({
    mutationFn: (artifactId: string) => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/restore`, { method: "POST", ...jsonBody({ revision: payload!.revision, artifact_id: artifactId }) }),
    onSuccess: refresh,
  });
  const approve = useMutation({
    mutationFn: () => {
      const qualityGoal = Number(payload!.quality.goal || 90);
      const score = Number(payload!.quality.score || 0);
      const hardFailures = payload!.quality.hard_gate_failures || [];
      if (hardFailures.length > 0) throw new Error(text("当前评估存在硬性门禁失败，必须修复并重新评估，不能人工覆盖。", "The current evaluation has hard gate failures. Fix and re-evaluate them; they cannot be overridden."));
      let overrideLowScore = false;
      let overrideReason = "";
      if (score < qualityGoal) {
        overrideLowScore = window.confirm(text("当前评分低于目标。确认已人工复核，并覆盖低分继续？", "The current score is below target. Confirm manual review and override the low score?"));
        if (!overrideLowScore) throw new Error(text("已取消人工覆盖。", "Manual override cancelled."));
        overrideReason = text("人工复核后接受当前版本及其评估提示。", "Current version and evaluation warnings accepted after manual review.");
      }
      return apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/draft/approve`, { method: "POST", ...jsonBody({ revision: payload!.revision, override_low_score: overrideLowScore, override_reason: overrideReason }) });
    },
    onSuccess: refresh,
  });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${encodeURIComponent(currentJobId)}/cancel`, { method: "POST" }) });

  const currentJob = polledJob.data;
  const evaluationActive = currentJob?.job_type === "draft.evaluate" && jobIsActive(currentJob.status);
  const optimizationActive = currentJob?.job_type === "draft.optimize" && jobIsActive(currentJob.status);
  const acceptRewriteActive = currentJob?.job_type === "draft.accept-rewrite" && jobIsActive(currentJob.status);
  const issues = payload?.quality.issues || [];
  const approvalGateDetails = hardGateDetails(payload?.quality);
  const latestIncremental = payload?.quality.incremental_evaluations?.slice(-1)[0];
  const error = assemble.error || saveFull.error || saveParagraph.error || evaluate.error || optimize.error || rewrite.error || decide.error || decideOptimization.error || restore.error || approve.error || cancel.error || (currentJob?.status === "failed" ? new Error(currentJob.error_message || text("任务失败。", "Task failed.")) : null);
  const tabLabels: Array<[DraftTab, string]> = [["preview", text("段落编辑", "Paragraph editing")], ["edit", text("全文编辑", "Full-text editing")], ["quality", text("评估与重写", "Evaluation and rewriting")], ["approval", text("人工确认", "Human approval")], ["history", text("版本历史", "Version history")]];
  const pendingCandidate = (paragraphId: string) => payload?.rewrite_candidates
    .filter((item) => item.paragraph_id === paragraphId && item.status === "pending")
    .slice(-1)[0];
  const pendingOptimization = payload?.optimization_proposals
    .filter((item) => item.status === "pending")
    .slice(-1)[0];
  const decideCandidate = (candidateId: string, decision: "accept" | "reject") => decide.mutate({ candidateId, decision });
  const decideOptimizationProposal = (proposalId: string, decision: "accept" | "reject", selectedParagraphIds: string[] = []) => decideOptimization.mutate({ proposalId, decision, selectedParagraphIds });
  const editIssueParagraph = (paragraphId: string) => {
    const paragraph = payload?.paragraphs.find((item) => item.paragraph_id === paragraphId);
    if (!paragraph) return;
    setEditingParagraph(paragraphId);
    setParagraphText(paragraph.text);
    setTab("preview");
  };
  const reviewGateParagraph = (paragraphId: string) => {
    setQualityFocusParagraph(paragraphId);
    setTab("quality");
  };
  useEffect(() => {
    if (tab !== "quality" || !qualityFocusParagraph) return;
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(`quality-issue-${qualityFocusParagraph}`);
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.focus({ preventScroll: true });
    });
    const clearHighlight = window.setTimeout(() => setQualityFocusParagraph(""), 2_400);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(clearHighlight);
    };
  }, [tab, qualityFocusParagraph, issues.length]);
  const gateLabel = (gateId: string) => gateId === "paragraph_readability_or_source_failures"
    ? text("段落可读性或来源校验未通过", "Paragraph readability or source validation failed")
    : gateId;
  const gateDiagnosis = (finding: HardGateFinding) => {
    const diagnosis = finding.diagnosis || text("请检查该段落。", "Review this paragraph.");
    const wordRange = /^Paragraph has (\d+) words; configured range is (\d+)-(\d+)\.$/.exec(diagnosis);
    if (wordRange) {
      return text(
        `段落共 ${wordRange[1]} 个英文词，未达到配置范围 ${wordRange[2]}–${wordRange[3]}。`,
        diagnosis,
      );
    }
    if (diagnosis === "No readable local source is registered for at least one cited paper.") {
      return text("该段引用的至少一篇论文没有可读取的本地来源，需要核对或重新解析 PDF。", diagnosis);
    }
    return diagnosis;
  };
  const gateRoute = (route?: string) => ({
    section_rewrite: text("补写或重写该段", "Expand or rewrite this paragraph"),
    local_source_recheck: text("核对本地论文来源", "Check the local paper source"),
    final_polish: text("最终润色", "Final polish"),
  }[route || ""] || route || text("人工检查", "Manual review"));
  const rewriteButtonText = (paragraphId: string, hasCandidate: boolean, active: boolean, targetedEvaluation = false) => {
    if (active || (rewrite.isPending && rewrite.variables === paragraphId)) {
      return targetedEvaluation
        ? text("正在生成候选并评分…", "Generating and scoring candidate…")
        : text("正在生成候选…", "Generating candidate…");
    }
    if (targetedEvaluation) return hasCandidate
      ? text("重新生成候选并评分", "Regenerate and score candidate")
      : text("生成候选并评分", "Generate and score candidate");
    return hasCandidate
      ? text("重新生成候选", "Regenerate candidate")
      : text("生成重写候选", "Generate rewrite candidate");
  };

  return <main className="workspace page-container workspace-page draft-page"><div className="workspace-heading"><div><p className="eyebrow">{text("阶段 6 · 初稿反馈循环", "Stage 6 · Draft feedback loop")}</p><h1>{text("初稿编辑、评估与优化", "Draft editing, evaluation, and optimization")}</h1><p className="muted">{text("评估严格绑定当前保存版本；单段重写与批量安全优化都会先生成对比候选，经人工确认后才写入正文。", "Evaluation is bound to the current saved version. Both paragraph rewrites and batch safe optimization create reviewable comparisons before any change is saved.")}</p></div><ProjectSelector /></div>
    {draft.isPending ? <div className="empty-state">{text("正在加载初稿…", "Loading draft…")}</div> : null}{draft.error ? <ErrorState error={draft.error} onRetry={() => draft.refetch()} /> : null}
    {payload ? <><div className="draft-grid-react"><aside className="pane draft-flow-react"><div className="pane-head"><div><span className="step-label">{text("初稿工作流", "Draft workflow")}</span><h2>{project?.slug || project?.project_id}</h2></div></div><div className="draft-flow-list">{tabLabels.map(([value, label], index) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => setTab(value)}><strong>{index + 1}. {label}</strong>{value === "approval" ? <small>{payload.draft_approval_current ? text("已确认", "Approved") : text("等待质量门禁", "Waiting for quality gate")}</small> : null}</button>)}</div></aside>
      <section className="pane draft-main-react"><div className="pane-head"><div><span className="step-label">{payload.freshness.upstream_stale ? text("已过期", "Out of date") : text("当前", "Current")}</span><h2>{tabLabels.find(([value]) => value === tab)?.[1]}</h2></div></div><div className="draft-main-content">
        {payload.freshness.upstream_stale ? <p className="message message-error">{text("上游内容已变化，请重新生成初稿后再编辑。", "Upstream content changed. Regenerate the draft before editing.")}</p> : null}
        {error ? <p className="message message-error">{error.message}</p> : null}
        {tab === "quality" ? <OptimizationProposalReview proposal={pendingOptimization} decide={decideOptimizationProposal} disabled={decideOptimization.isPending} /> : null}
        {tab === "preview" ? payload.paragraphs.length ? payload.paragraphs.map((paragraph) => { const candidate = pendingCandidate(paragraph.paragraph_id); const state = payload.rewrite_states[paragraph.paragraph_id]; return <article className="draft-paragraph-card" key={paragraph.paragraph_id}><header><strong>{paragraph.paragraph_id}</strong><span className={`job-pill ${state?.status || ""}`}>{state?.status || ""}</span></header>{editingParagraph === paragraph.paragraph_id ? <><textarea rows={8} value={paragraphText} onChange={(event) => setParagraphText(event.target.value)} /><button className="button button-primary" type="button" disabled={!paragraphText.trim() || saveParagraph.isPending} onClick={() => saveParagraph.mutate({ paragraphId: paragraph.paragraph_id, text: paragraphText })}>{text("保存段落", "Save paragraph")}</button></> : <p>{paragraph.text}</p>}<footer><button className="button button-secondary" type="button" onClick={() => { setEditingParagraph(paragraph.paragraph_id); setParagraphText(paragraph.text); }}>{text("编辑段落", "Edit paragraph")}</button></footer><PendingRewrite candidate={candidate} decide={decideCandidate} disabled={decide.isPending} /></article>; }) : <MarkdownView content={payload.first_draft_md} empty={text("尚未生成初稿。", "Draft not generated yet.")} /> : null}
        {tab === "edit" ? <div className="full-draft-editor"><textarea value={draftText} onChange={(event) => setDraftText(event.target.value)} spellCheck={false} /><button className="button button-primary" type="button" disabled={saveFull.isPending || !draftText.trim() || draftText.trim() === payload.first_draft_md.trim()} onClick={() => saveFull.mutate()}>{saveFull.isPending ? text("保存中…", "Saving…") : text("保存全文", "Save full draft")}</button></div> : null}
        {tab === "quality" ? <>{!payload.quality.current ? <p className="message message-warning">{text("全文评分已过期。候选生成后只会自动评分该候选段落，不会重新评估全文；保存候选后再按该段分差更新总分。", "The full-draft score is stale. A generated candidate is scored only at paragraph scope; the full draft is not re-evaluated. Saving then updates the overall score by that paragraph's delta.")}</p> : null}<div className="quality-score-grid"><article><span>{text("当前分数", "Current score")}</span><strong>{payload.quality.score === undefined ? "—" : Number(payload.quality.score).toFixed(1)}</strong></article><article><span>{text("目标", "Goal")}</span><strong>{payload.quality.goal === undefined ? "—" : Number(payload.quality.goal).toFixed(1)}</strong></article><article><span>{text("问题数", "Issues")}</span><strong>{issues.length}</strong></article></div>{latestIncremental ? <section className="message message-success"><strong>{text(`最近保存的单段候选：${latestIncremental.paragraph_id}`, `Latest saved paragraph candidate: ${latestIncremental.paragraph_id}`)}</strong><p>{text(`段落 ${Number(latestIncremental.old_paragraph_score).toFixed(1)} → ${Number(latestIncremental.new_paragraph_score).toFixed(1)}；全文 ${Number(latestIncremental.previous_overall_score).toFixed(1)} → ${Number(latestIncremental.updated_overall_score).toFixed(1)}`, `Paragraph ${Number(latestIncremental.old_paragraph_score).toFixed(1)} → ${Number(latestIncremental.new_paragraph_score).toFixed(1)}; overall ${Number(latestIncremental.previous_overall_score).toFixed(1)} → ${Number(latestIncremental.updated_overall_score).toFixed(1)}`)}</p></section> : null}{issues.map((issue, index) => { const paragraphId = String(issue.paragraph_id || ""); const candidate = pendingCandidate(paragraphId); const state = payload.rewrite_states[paragraphId]; const rewriteActive = jobIsActive(state?.status); return <article id={`quality-issue-${paragraphId}`} tabIndex={-1} className={`quality-issue-react${qualityFocusParagraph === paragraphId ? " quality-focus" : ""}`} key={String(issue.issue_id || index)}><header><strong>{String(issue.severity || "issue")} · {paragraphId}</strong><span>{String(issue.message || issue.diagnosis || "")}</span></header><dl className="quality-issue-meta"><div><dt>{text("段落分数", "Paragraph score")}</dt><dd>{issue.score === undefined || issue.score === null ? "—" : Number(issue.score).toFixed(1)}</dd></div><div><dt>{text("处理路线", "Route")}</dt><dd>{issue.route || "—"}</dd></div></dl>{issue.failed_dimensions?.length ? <div className="quality-dimensions"><strong>{text("未通过维度", "Failed dimensions")}</strong>{issue.failed_dimensions.map((dimension) => <span key={dimension}>{dimension}</span>)}</div> : null}<p>{issue.paragraph?.text || ""}</p>{issue.paragraph?.images?.length ? <div className="issue-images">{issue.paragraph.images.map((image) => <figure key={image.artifact_id}><img src={image.url} alt={image.figure_id} /><figcaption>{image.figure_id}</figcaption></figure>)}</div> : null}<div className="quality-issue-actions"><button className="button button-secondary" type="button" onClick={() => editIssueParagraph(paragraphId)}>{text("在 Preview 中编辑", "Edit in Preview")}</button><button className="button button-secondary" type="button" disabled={rewrite.isPending || rewriteActive} onClick={() => rewrite.mutate(paragraphId)}>{rewriteButtonText(paragraphId, Boolean(candidate), rewriteActive, true)}</button></div><PendingRewrite candidate={candidate} decide={decideCandidate} disabled={decide.isPending || acceptRewriteActive} /></article>; })}{!issues.length ? <div className="empty-state">{payload.quality.current ? text("当前评估未发现问题。", "No issues found in the current evaluation.") : text("请评估当前初稿。", "Evaluate the current draft.")}</div> : null}</> : null}
        {tab === "approval" ? <section className={payload.draft_approval_current ? "approval-card good" : "approval-card"}><h2>{payload.draft_approval_current ? text("初稿已人工确认", "Draft manually approved") : text("等待人工确认", "Waiting for human approval")}</h2><p>{payload.quality.current ? text(`当前评估分数：${payload.quality.score ?? "—"}`, `Current evaluation score: ${payload.quality.score ?? "—"}`) : text("请先评估当前保存版本。", "Evaluate the current saved version first.")}</p>{approvalGateDetails.length ? <><p className="message message-error">{text("以下硬性门禁失败不可人工覆盖。请按段落明细修复后重新评估：", "The following hard gates cannot be overridden. Fix the listed paragraphs and re-evaluate:")}</p><div className="hard-gate-detail-list">{approvalGateDetails.map((detail) => <article key={detail.gate_id} className="hard-gate-detail"><header><strong>{gateLabel(detail.gate_id)}</strong>{detail.findings.length ? <span>{text(`涉及 ${detail.findings.length} 个段落`, `${detail.findings.length} paragraphs`)}</span> : null}</header>{detail.findings.length ? <div className="hard-gate-paragraphs">{detail.findings.map((finding, index) => <section key={`${detail.gate_id}-${finding.paragraph_id}-${finding.rule || index}`}><div><strong>{finding.paragraph_id}</strong><span>{[finding.rule, finding.severity].filter(Boolean).join(" · ")}</span></div><p>{gateDiagnosis(finding)}</p><footer><small>{text("建议处理：", "Suggested action: ")}{gateRoute(finding.route)}</small><button className="button button-secondary" type="button" onClick={() => reviewGateParagraph(finding.paragraph_id)}>{text("在评估与重写中处理", "Review in evaluation and rewriting")}</button></footer></section>)}</div> : <p className="muted">{text(`门禁标识：${detail.gate_id}。当前旧报告没有段落明细，请在“评估与重写”查看问题并重新评估。`, `Gate: ${detail.gate_id}. This legacy report has no paragraph details; review Evaluation and rewriting, then re-evaluate.`)}</p>}</article>)}</div></> : null}<button className="button button-primary" type="button" disabled={!payload.quality.current || approve.isPending || payload.draft_approval_current || Boolean(payload.quality.hard_gate_failures?.length)} onClick={() => approve.mutate()}>{payload.draft_approval_current ? text("已确认", "Approved") : text("确认并允许进入终稿", "Approve and allow final stage")}</button>{payload.draft_approval_current ? <button className="button button-secondary" type="button" onClick={() => navigate(`/final?project=${encodeURIComponent(project!.project_id)}`)}>{text("进入终稿", "Enter final stage")}</button> : null}</section> : null}
        {tab === "history" ? <div className="version-list-react">{payload.versions.map((version) => <article key={version.artifact_id} className={version.current ? "current" : ""}><strong>{version.operation || "saved"}</strong><span>{version.created_at}</span><code>{version.artifact_id}</code>{version.current ? <em>{text("当前版本", "Current version")}</em> : <button className="button button-secondary" type="button" disabled={restore.isPending} onClick={() => { if (window.confirm(text("恢复这个不可变初稿版本？", "Restore this immutable draft version?"))) restore.mutate(version.artifact_id); }}>{text("恢复此版本", "Restore this version")}</button>}</article>)}</div> : null}
      </div></section>
      <aside className="pane draft-control-react"><div className="pane-head"><div><span className="step-label">{text("质量控制", "Quality controls")}</span><h2>{text("质量控制", "Quality controls")}</h2></div></div><div className="gate-body"><div className="feedback-settings"><label>{text("全文目标分数", "Full-draft target score")}<div className="goal-input"><input type="number" min={90} max={100} step={0.5} value={goal} onChange={(event) => setGoal(Math.min(100, Math.max(90, Number(event.target.value) || 90)))} /><span>/ 100</span></div></label><label>{text("段落目标分数", "Paragraph target score")}<div className="goal-input"><input type="number" min={0} max={100} step={0.5} value={paragraphGoal} onChange={(event) => setParagraphGoal(Math.min(100, Math.max(0, Number(event.target.value) || 85)))} /><span>/ 100</span></div></label><label>{text("最大优化轮次", "Maximum iterations")}<input type="number" min={1} max={10} value={maxIterations} onChange={(event) => setMaxIterations(Math.min(10, Math.max(1, Number(event.target.value) || 3)))} /></label><div className="case-word-range"><label>{text("案例最少词数", "Minimum case words")}<input type="number" min={1} value={minCaseWords} onChange={(event) => setMinCaseWords(Math.max(1, Number(event.target.value) || 140))} /></label><label>{text("案例最多词数", "Maximum case words")}<input type="number" min={minCaseWords} value={maxCaseWords} onChange={(event) => setMaxCaseWords(Math.max(minCaseWords, Number(event.target.value) || 280))} /></label></div></div><button className="button button-primary" type="button" disabled={!payload.draft_artifact_id || evaluate.isPending || optimize.isPending || jobIsActive(currentJob?.status)} onClick={() => evaluate.mutate()}>{evaluate.isPending ? text("正在启动评估…", "Starting evaluation…") : evaluationActive ? text("正在评估…", "Evaluating…") : text("评估当前初稿", "Evaluate current draft")}</button><button className="button button-secondary" type="button" disabled={!payload.draft_artifact_id || evaluate.isPending || optimize.isPending || jobIsActive(currentJob?.status) || maxCaseWords < minCaseWords} onClick={() => optimize.mutate()}>{optimize.isPending ? text("正在启动优化…", "Starting optimization…") : optimizationActive ? text("正在批量优化…", "Optimizing…") : text("批量安全优化", "Batch safe optimize")}</button><p className="feedback-help">{text("批量优化会逐段执行完整性校验和复评，只把分数提高的候选送入人工对比；你确认前正文不会改变。", "Batch optimization integrity-checks and re-evaluates each paragraph, then sends only improved candidates to human comparison. The saved draft does not change until you confirm.")}</p><button className="button button-quiet danger" type="button" disabled={!jobIsActive(currentJob?.status) || cancel.isPending} onClick={() => cancel.mutate()}>{currentJob?.status === "cancel_requested" || cancel.isPending ? text("正在取消…", "Cancelling…") : text("取消运行任务", "Cancel running task")}</button>{evaluate.isPending ? <DraftJobStatus startingType="draft.evaluate" /> : optimize.isPending ? <DraftJobStatus startingType="draft.optimize" /> : currentJob ? <DraftJobStatus job={currentJob} publicationPending={publicationPending} /> : null}<dl className="draft-summary"><dt>{text("分数", "Score")}</dt><dd>{payload.quality.score ?? "—"}</dd><dt>{text("状态", "Status")}</dt><dd>{payload.freshness.upstream_stale ? text("已过期", "Stale") : text("当前", "Current")}</dd><dt>Revision</dt><dd>{payload.revision}</dd></dl></div></aside></div></> : null}
  </main>;
}
