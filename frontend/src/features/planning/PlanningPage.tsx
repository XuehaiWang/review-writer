import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { queryKeys } from "../../api/queries";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { useUiText } from "../../i18n/useUiText";
import { jobIsActive } from "../../hooks/useJob";
import { applyOutlinePaperRecommendations, buildPaperDisplayLabels, OutlineBuilder, parseOutlineMarkdown, validateVisualOutline } from "./OutlineBuilder";
import { MatrixLiveProgress, readMatrixEnrichmentLive } from "./MatrixLiveProgress";
import { MatrixAnalysisStatus, MatrixEvidenceUse, analysisState } from "./MatrixAnalysisStatus";
import { BibliographyResolutionPanel } from "./BibliographyResolutionPanel";
import { usePlanningCompletionSync } from "./usePlanningCompletionSync";

type MatrixPaper = Record<string, unknown> & {
  paper_id: string;
  title?: string | Record<string, unknown>;
  authors?: string[];
  year?: string | number;
  journal?: string;
  doi?: string;
  keywords?: string[];
  abstract?: string;
  main_content?: string;
  full_reading_complete?: boolean;
  reading_complete?: boolean;
  most_relevant_figure?: Record<string, unknown>;
  scientific_facts?: Array<{
    fact_id?: string;
    field_id?: string;
    value?: string;
    support_excerpt?: string;
    epistemic_status?: string;
    source_channel?: string;
    support_level?: "direct" | "abstract_limited" | "context_only" | "coverage_only";
    review_status?: "not_required" | "auto_limited" | "needs_review" | "human_checked";
    confidence?: number;
    evidence_ceiling?: string;
    assertion_ceiling?: string;
    evidence_refs?: Array<{ page_start?: number | null; page_end?: number | null; chunk_id?: string }>;
  }>;
  fact_enrichment?: {
    status?: string;
    extraction_status?: string;
    processing?: { extraction?: string; verification?: string; source_recovery?: string };
    pending_fact_count?: number;
    verified_fact_count?: number;
    fact_extraction_profile?: { stop_reason?: string };
    review_readiness?: "complete" | "partial" | "source_not_established";
    required_fact_roles?: string[];
    supported_fact_roles?: string[];
    missing_fact_roles?: string[];
    review_status?: string;
    fact_count?: number;
    error?: string;
    last_attempt?: { status?: string; error?: string; updated_at?: string };
    automatic_resolution?: {
      status?: string;
      targeted_recheck_attempted?: boolean;
      resolved_axis_ids?: string[];
      unresolved_required_axes?: string[];
      safe_route_policy?: string;
      user_action_required?: boolean;
    };
  };
  topic_partition_classification: {
    status?: "classified" | "boundary" | "insufficient_evidence" | "cross_category" | "out_of_scope" | "not_requested";
    partition?: string;
    candidate_partition?: string;
    confidence?: number;
    rationale?: string;
    boundary_reason?: string;
    support_excerpt?: string;
    evidence_ceiling?: string;
  };
  provisional_screening_tags?: Array<{ axis_id?: string; axis_label?: string; partition_id?: string; partition_label?: string }>;
  evidence_backed_tags?: Record<string, Array<{
    axis_id?: string;
    axis_label?: string;
    axis_role?: string;
    partition_id?: string;
    partition_label?: string;
    relation_to_paper?: string;
    fact_ids?: string[];
    confidence?: number;
    assertion_ceiling?: string;
    evidence_refs?: Array<{ evidence_key?: string; chunk_id?: string }>;
  }>>;
  classification_outcomes?: Array<{
    axis_id?: string;
    axis_role?: string;
    status?: "insufficient_evidence" | "cross_category" | "out_of_scope";
    reason?: string;
    resolution?: string;
    user_action_required?: boolean;
  }>;
  bibliography_identity?: {
    status?: string;
    verified?: boolean;
    manual_review_status?: string;
    resolved_by?: string;
    resolved_at?: string | null;
    unresolved_conflict_count?: number;
    missing_fields?: string[];
    candidate_count?: number;
    verification_method?: string;
    bibliography_role?: string;
    direct_claim_eligible?: boolean;
    context_only?: boolean;
    parent_paper_id?: string;
  };
};

type BlueprintSection = Record<string, unknown> & {
  section_id?: string;
  title?: string;
  section_thesis?: string;
  writing_objective?: string;
  questions_to_answer?: string[];
  retrieval_directions?: string[];
  single_paper_policy?: { mode?: string; primary_paper_id?: string; requires_user_action?: boolean };
  planning_status?: string;
  planning_notes?: string[];
  thesis_status?: "evidence_grounded" | "provisional" | "structural_synthesis" | string;
  scientific_thesis?: {
    text?: string;
    status?: string;
    missing_components?: string[];
    argument_purpose?: string;
    comparison_axes?: string[];
    boundaries?: string[];
    open_questions?: string[];
  };
  depth_contract?: {
    target_paragraph_count?: number;
    target_word_min?: number;
    target_word_max?: number;
    minimum_comparison_paragraphs?: number;
    requires_section_synthesis_exit?: boolean;
  };
  section_goal?: string;
  assigned_papers?: unknown[];
  major_papers?: unknown[];
  primary_papers?: unknown[];
  section_role?: string;
  paragraph_plan?: unknown[];
  review_claims?: unknown[];
  scientific_claims?: Array<{
    claim_id?: string;
    required_for_section?: boolean;
    proposition?: string;
    allowed_assertion?: string;
    support_status?: "supported" | "partially_supported" | "missing" | string;
    primary_papers?: string[];
    required_fact_roles?: string[];
    fact_ids?: string[];
    evidence_refs?: Array<{ evidence_key?: string }>;
  }>;
  organizing_thread?: string;
  paragraph_tasks?: string[];
  paper_roles?: Array<{ paper_id: string; role: string; reason: string; claim_ids: string[]; presentation?: string }>;
  coverage_by_use?: Array<{ use_id: string; purpose: string; status: string; missing_requirements: string[] }>;
  generation_eligible?: boolean;
  executable_claim_count?: number;
  pending_claim_count?: number;
  automatic_resolution?: {
    action?: string;
    reason?: string;
    target_section_id?: string;
    moved_paper_ids?: string[];
    absorbed_section_ids?: string[];
    absorbed_paper_ids?: string[];
    requires_user_action?: boolean;
  };
  required_figures?: unknown[];
  figure_or_table_needs?: unknown[];
  evidence_readiness?: {
    reason?: string;
    status?: "ready" | "partial" | "insufficient" | "synthesis" | "not_reviewed" | "fact_assisted";
    writeable_primary_count?: number;
    context_only_primary_count?: number;
    unresolved_primary_count?: number;
  };
};

type ScopeContract = Record<string, unknown> & {
  target_question?: string;
  review_objective?: string;
  primary_navigation_axis?: string;
  target_readers?: string[];
  required_reader_outcomes?: string[];
  search_cutoff_date?: string;
};

type CoverageDiagnostics = {
  selected_paper_count?: number;
  search_cutoff_date?: string | null;
  year_distribution?: Record<string, number>;
  year_unknown_count?: number;
  recent_paper_ratio?: number | null;
  source_distribution?: Record<string, number>;
  topic_clusters?: Array<{ label: string; paper_count: number }>;
  warnings?: Array<{ rule_id?: string; message?: string }>;
  limitations?: string[];
};

type PlanningDiagnostics = {
  can_confirm?: boolean;
  blocking_issue_count?: number;
  warning_count?: number;
  issues?: Array<{ rule_id?: string; severity?: string; message?: string }>;
};

type TopicOutlineIntent = {
  available?: boolean;
  primary_axis?: string;
  secondary_axes?: string[];
  partitions?: string[];
  required_partitions?: string[];
  axis_examples?: Record<string, string[]>;
  comparison_dimensions?: string[];
  focus_dimensions?: string[];
  named_systems?: string[];
  requested_outcomes?: string[];
  primary_axis_label?: string;
  secondary_axis_labels?: Record<string, string>;
  system_recommended?: boolean;
};

type OutlineCandidate = Record<string, unknown> & {
  candidate_id?: string;
  outline_style?: string;
  source?: string;
  labels?: { en?: string; zh?: string };
  outline_md?: string;
  topic_outline_intent?: TopicOutlineIntent;
};

type AutoRoutingAdjustment = {
  source_section?: string;
  target_section?: string;
  paper_ids?: string[];
  method?: string;
  created_section?: boolean;
};

type BlueprintRestructureRecord = {
  is_restructure?: boolean;
  previous_blueprint_artifact_id?: string | null;
  trigger_reasons?: string[];
  application_mode?: string;
  rollback_supported?: boolean;
  section_mapping?: Array<{
    previous_section_id?: string | null;
    current_section_id?: string | null;
    previous_title?: string | null;
    current_title?: string | null;
    migration_action?: string;
  }>;
};

type PlanningPayload = {
  topic?: string;
  matrix_revision: number;
  blueprint_revision: number;
  blueprint_artifact_id?: string;
  blueprint_candidate_pending?: boolean;
  blueprint_candidate_inputs?: { literature_matrix?: { rows?: MatrixPaper[] } } | null;
  blueprint_jobs?: Job[];
  matrix_artifact_id?: string;
  literature_matrix?: { rows?: MatrixPaper[] };
  selected_outline_md?: string;
  outline_current?: boolean;
  outline_options_md?: string;
  outline_selection?: Record<string, unknown>;
  outline_candidates?: OutlineCandidate[];
  reference_outline_candidates?: Array<Record<string, unknown> & { candidate_id?: string; source_name?: string }>;
  legacy_reference_outline_count?: number;
  section_blueprint?: {
    fact_source_issues?: Array<{ paper_id: string; fact_id: string; value?: string; reasons: string[]; action: string }>;
    unused_papers?: Array<{ paper_id: string; reason_code: string; reason: string }>;
    academic_planning?: { status?: string; incomplete_sections?: string[]; resume_available?: boolean; stop_reason?: string };
    sections?: BlueprintSection[];
    resolved_outline_md?: string;
    auto_routing_adjustments?: AutoRoutingAdjustment[];
    restructure_record?: BlueprintRestructureRecord;
  };
  blueprint_current?: boolean;
  section_writing_plan_md?: string;
  matrix_sync?: Record<string, unknown>;
  scope_contract?: ScopeContract;
  scope_diagnostics?: PlanningDiagnostics;
  coverage_diagnostics?: CoverageDiagnostics;
  classification_basis?: Record<string, unknown>;
  taxonomy_diagnostics?: PlanningDiagnostics;
  matrix_enrichment?: {
    counts?: Record<string, number>;
    jobs?: Job[];
    summary?: Record<string, unknown>;
    all_failed?: boolean;
    failed_publish_with_pending_rows?: boolean;
    limited_mode_confirmed?: boolean;
    planning_blocked?: boolean;
  };
};

type OutlineRecommendationResponse = {
  sections: Array<{ section_index: number; paper_ids: string[] }>;
  summary: {
    body_section_count: number;
    recommended_paper_count: number;
    unassigned_paper_count: number;
    fulltext_used: boolean;
    model_used: boolean;
    model_resolved_paper_count: number;
    model_fallback_used: boolean;
  };
};

const outlineStyles = [
  { id: "substrate", icon: "S", titleZh: "底物结构", titleEn: "Substrate structure", descriptionZh: "按底物类别和结构差异组织论文。", descriptionEn: "Organize papers by substrate class and structural differences." },
  { id: "catalyst", icon: "C", titleZh: "催化剂与方法", titleEn: "Catalysts and methods", descriptionZh: "比较催化体系、配体与方法学家族。", descriptionEn: "Compare catalytic systems, ligands, and method families." },
  { id: "reaction", icon: "R", titleZh: "反应类型", titleEn: "Reaction type", descriptionZh: "按照转化逻辑与机理策略组织内容。", descriptionEn: "Organize content by transformation logic and mechanistic strategy." },
  { id: "custom", icon: "E", titleZh: "自定义大纲", titleEn: "Custom outline", descriptionZh: "使用新手表单逐节填写，也可切换高级Markdown。", descriptionEn: "Fill sections with a beginner-friendly form, with optional advanced Markdown." },
];

function displayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(displayText).filter(Boolean).join(", ");
  if (typeof value === "object" && "value" in value) return displayText((value as { value: unknown }).value);
  return JSON.stringify(value);
}

function factStatusLabel(status: string, text: (zh: string, en: string) => string): string {
  return ({
    complete: text("已完成", "Completed"),
    limited: text("已完成·部分可用", "Completed · partially usable"),
    failed: text("需恢复", "Recovery needed"),
    pending: text("待处理", "Pending"),
    running: text("分析中", "Analyzing"),
  } as Record<string, string>)[status] || status;
}

function factStatusClass(status: string): string {
  if (status === "complete") return "ok";
  if (status === "failed") return "matrix-recovery";
  if (status === "limited") return "matrix-limited";
  if (status === "running") return "matrix-running";
  if (status === "pending") return "pending";
  return "warning";
}

function outlineAxisLabel(axis: string, text: (zh: string, en: string) => string): string {
  return ({
    reaction_type: text("反应类型", "Reaction type"),
    stereochemical_regime: text("立体化学模式", "Stereochemical regime"),
    catalyst_or_method: text("催化或促进体系", "Catalytic or promoting system"),
    substrate: text("底物类别", "Substrate class"),
    product: text("产物类别", "Product class"),
    organometallic_partner: text("金属有机试剂", "Organometallic partner"),
    ligand_or_chiral_source: text("配体或手性来源", "Ligand or chiral source"),
    leaving_group: text("离去基团类别", "Leaving-group class"),
    document_scope: text("证据或文献类型", "Evidence or document type"),
  } as Record<string, string>)[axis] || axis.replaceAll("_", " ");
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error || new Error("Unable to read the reference file."));
    reader.readAsDataURL(file);
  });
}

function MatrixWorkspace({ payload, projectId, refresh }: { payload: PlanningPayload; projectId: string; refresh: () => Promise<unknown> }) {
  const { text } = useUiText();
  const [mode, setMode] = useState<"reading" | "outline">("reading");
  const [filter, setFilter] = useState("");
  const papers = payload.literature_matrix?.rows || [];
  const [selectedId, setSelectedId] = useState(papers[0]?.paper_id || "");
  const selected = papers.find((paper) => paper.paper_id === selectedId) || papers[0];
  const [note, setNote] = useState(selected?.main_content || "");
  const [complete, setComplete] = useState(Boolean(selected?.full_reading_complete || selected?.reading_complete));
  const [outlineDraft, setOutlineDraft] = useState(payload.selected_outline_md || "");
  const [scopeDraft, setScopeDraft] = useState<ScopeContract>(payload.scope_contract || {});
  const paperLabels = useMemo(() => buildPaperDisplayLabels(papers), [papers]);
  const enrichmentJob = payload.matrix_enrichment?.jobs?.[0];
  const enrichmentActive = Boolean(enrichmentJob && jobIsActive(enrichmentJob.status));
  const enrichmentLive = enrichmentActive && enrichmentJob ? readMatrixEnrichmentLive(enrichmentJob) : null;
  const activeFactPaperIds = new Set(enrichmentLive?.active_paper_ids || []);
  if (enrichmentLive?.current_paper_id) activeFactPaperIds.add(enrichmentLive.current_paper_id);
  const enrichmentFailed = Boolean(enrichmentJob && ["failed", "cancelled", "interrupted"].includes(enrichmentJob.status));
  const hasRecoveryCheckpoint = Boolean(
    enrichmentJob?.result?.matrix_enrichment_checkpoint
    || enrichmentJob?.result?.section_checkpoint,
  );
  const factCounts = papers.reduce((counts, paper) => {
    const status = analysisState(paper, activeFactPaperIds.has(paper.paper_id));
    counts[status === "limited" ? "complete" : status] += 1;
    return counts;
  }, { complete: 0, running: 0, pending: 0, failed: 0 });
  useEffect(() => {
    setNote(selected?.main_content || "");
    setComplete(Boolean(selected?.full_reading_complete || selected?.reading_complete));
  }, [selected]);
  useEffect(() => setOutlineDraft(payload.selected_outline_md || ""), [payload.selected_outline_md]);
  useEffect(() => setScopeDraft(payload.scope_contract || {}), [payload.scope_contract]);

  const saveReading = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/matrix/${encodeURIComponent(selected!.paper_id)}`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, main_content: note, most_relevant_figure: selected?.most_relevant_figure || null, mark_complete: complete }),
    }),
    onSuccess: refresh,
  });
  const chooseOutline = useMutation({
    mutationFn: (outlineStyle: string) => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: outlineStyle }),
    }),
    onSuccess: refresh,
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) await refresh();
    },
  });
  const saveOutline = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: String(payload.outline_selection?.outline_style || "custom"), outline_md: outlineDraft, scope_contract: scopeDraft }),
    }),
    onSuccess: refresh,
  });
  const recommendOutline = useMutation<OutlineRecommendationResponse, Error, string>({
    mutationFn: (sourceOutline) => apiRequest<OutlineRecommendationResponse>(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline/recommendations`, {
      method: "POST",
      ...jsonBody({ revision: payload.matrix_revision, outline_md: sourceOutline }),
    }),
    onSuccess: (result, sourceOutline) => setOutlineDraft((current) => current === sourceOutline
      ? applyOutlinePaperRecommendations(current, result.sections)
      : current),
  });
  const uploadReference = useMutation({
    mutationFn: async (file: File) => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/reference-outlines`, {
      method: "POST",
      ...jsonBody({ revision: payload.matrix_revision, filename: file.name, content_base64: await fileBase64(file) }),
    }),
    onSuccess: refresh,
  });
  const visiblePapers = papers.filter((paper) => [paper.paper_id, paperLabels.get(paper.paper_id), displayText(paper.title), paper.keywords?.join(" "), paper.abstract].join(" ").toLowerCase().includes(filter.toLowerCase()));
  const selectedStyle = String(payload.outline_selection?.outline_style || "");
  const isCurrentOutline = (style: string) => selectedStyle === style
    && (payload.outline_current !== false || style === "custom");
  const outlineChoiceProps = (style: string) => ({
    disabled: chooseOutline.isPending || saveOutline.isPending || recommendOutline.isPending || isCurrentOutline(style),
    onClick: () => chooseOutline.mutate(style),
    "aria-busy": chooseOutline.isPending && chooseOutline.variables === style,
  });
  const outlineChoiceText = (style: string, label: string, currentLabel: string) =>
    chooseOutline.isPending && chooseOutline.variables === style ? text("正在应用…", "Applying…")
      : isCurrentOutline(style) ? currentLabel : label;
  const topicOutlineCandidate = useMemo(
    () => (payload.outline_candidates || []).find((candidate) => candidate.source === "topic"),
    [payload.outline_candidates],
  );
  const topicOutlineIntent = topicOutlineCandidate?.topic_outline_intent;
  const topicAxisExamples = Object.entries(topicOutlineIntent?.axis_examples || {})
    .filter(([, values]) => values.length)
    .map(([axis, values]) => `${outlineAxisLabel(axis, text)}: ${values.join(" / ")}`);
  const outlineReady = validateVisualOutline(parseOutlineMarkdown(outlineDraft)).ready;
  const recommendationSummary = recommendOutline.data?.summary;
  const recommendationMessage = recommendationSummary
    ? text(
      `已为 ${recommendationSummary.body_section_count} 个正文章节推荐 ${recommendationSummary.recommended_paper_count} 篇论文${recommendationSummary.fulltext_used ? "，已使用全文检索" : ""}${recommendationSummary.model_used ? `，模型裁决 ${recommendationSummary.model_resolved_paper_count} 篇模糊匹配` : ""}${recommendationSummary.model_fallback_used ? "；模型暂不可用，本次采用保守匹配" : ""}${recommendationSummary.unassigned_paper_count ? `；另有 ${recommendationSummary.unassigned_paper_count} 篇未强行分配` : ""}。请检查后保存大纲。`,
      `Recommended ${recommendationSummary.recommended_paper_count} papers across ${recommendationSummary.body_section_count} body sections${recommendationSummary.fulltext_used ? " using full-text retrieval" : ""}${recommendationSummary.model_used ? `; the model resolved ${recommendationSummary.model_resolved_paper_count} ambiguous matches` : ""}${recommendationSummary.model_fallback_used ? "; the model was unavailable, so conservative matching was used" : ""}${recommendationSummary.unassigned_paper_count ? `; ${recommendationSummary.unassigned_paper_count} papers were deliberately left unassigned` : ""}. Review the selections, then save the outline.`,
    )
    : "";
  const facts = selected?.scientific_facts || [];
  const formalClassificationTags = Object.values(selected?.evidence_backed_tags || {}).flat();
  const provisionalClassificationTags = selected?.provisional_screening_tags || [];
  const classificationOutcomes = selected?.classification_outcomes || [];
  const selectedFactStatus = selected ? analysisState(selected, activeFactPaperIds.has(selected.paper_id)) : "pending";
  const actionRequiredOutcomes = classificationOutcomes.filter((outcome) => outcome.user_action_required === true);
  const automaticallyHandledOutcomes = classificationOutcomes.filter((outcome) => outcome.user_action_required !== true);
  const unresolvedTopicPartition = ["boundary", "insufficient_evidence", "cross_category", "out_of_scope"].includes(String(selected?.topic_partition_classification?.status || ""));
  const bibliographyIssueCount = papers.filter((paper) => paper.bibliography_identity?.verified === false).length;
  const emptyFactMessage = enrichmentActive
    ? text("正在从全文证据中提取本篇论文的科学事实。", "Scientific facts are being extracted from this paper's evidence.")
    : enrichmentFailed && hasRecoveryCheckpoint && selectedFactStatus === "pending"
      ? text("本篇事实已经完成提取，但尚未发布到 Matrix；请使用左侧的“继续未完成分析”。", "This paper was extracted but not published to the Matrix. Use Resume unfinished analysis on the left.")
      : selectedFactStatus === "pending"
      ? text("生成章节规划时会自动分析当前主题需要的科学事实；原文证据仍是最终依据。", "Chapter planning will automatically analyze the facts needed for the current topic; source passages remain authoritative.")
        : selected?.fact_enrichment?.extraction_status === "failed" || selected?.fact_enrichment?.status === "failed"
          ? text("分析未完成，不代表论文没有证据。请查看上方原因和重试操作。", "Analysis is incomplete, not evidence of absent findings. See the explanation and retry action above.")
          : text("当前没有已核验事实；仍可查看原文，不能据此认定论文未报告相关内容。", "No verified facts are available yet; consult the source rather than treating this as absence of reported findings.");

  return (
    <>
      <nav className="workspace-mode-tabs"><button type="button" className={mode === "reading" ? "active" : ""} onClick={() => setMode("reading")}>{text("文献Matrix", "Literature matrix")}</button><button type="button" className={mode === "outline" ? "active" : ""} onClick={() => setMode("outline")}>{text("大纲选择与上传", "Choose or upload outline")}</button></nav>
      {mode === "reading" ? (
        <div className="planning-grid">
          <section className="pane planning-list-pane">
            <div className="pane-head matrix-fact-head"><div><span className="step-label">{text("文献Matrix", "Literature matrix")}</span><h2>{papers.length} {text("篇论文", "papers")}</h2><p>{enrichmentActive ? text(`正在提取科学事实 ${enrichmentJob?.progress_current || 0}/${enrichmentJob?.progress_total || papers.length}`, `Extracting scientific facts ${enrichmentJob?.progress_current || 0}/${enrichmentJob?.progress_total || papers.length}`) : text("确认采用文献后自动提取事实；章节规划复用有效结果并按需补证", "Facts are extracted after selection confirmation; chapter planning reuses results and fills evidence gaps")}</p><div className="matrix-fact-counts"><span className="complete">{text("已完成", "Completed")} {factCounts.complete}</span><span className="pending">{text("分析中", "Analyzing")} {factCounts.running}</span><span className="pending">{text("待分析或核验", "Pending analysis or verification")} {factCounts.pending}</span><span className="failed">{text("需恢复", "Recovery needed")} {factCounts.failed}</span></div><div className="matrix-header-actions">{bibliographyIssueCount ? <button type="button" className="matrix-bibliography-note" onClick={() => setSelectedId(papers.find((paper) => paper.bibliography_identity?.verified === false)?.paper_id || selectedId)}><strong>{text(`书目待核验 ${bibliographyIssueCount}`, `${bibliographyIssueCount} bibliography records pending`)}</strong><span>{text("点击定位并解决，不阻断内部写作。", "Open the affected paper and resolve it without blocking internal writing.")}</span></button> : null}
              <MatrixAnalysisStatus key={projectId} papers={papers} projectId={projectId} busy={enrichmentActive || Boolean(payload.blueprint_jobs?.some(job => jobIsActive(job.status)))} refresh={refresh} recoveryJobId={enrichmentFailed && hasRecoveryCheckpoint && enrichmentJob?.error_code === "STATE_CONFLICT" && enrichmentJob.available_actions?.includes("retry") ? enrichmentJob.id : undefined} />
            </div></div></div>
            <div className="matrix-list-notices" aria-live="polite">
              {enrichmentActive && enrichmentJob ? <MatrixLiveProgress job={enrichmentJob} papers={papers} /> : null}
              {enrichmentFailed ? <div className="message message-error matrix-enrichment-error"><strong>{enrichmentJob?.error_message === "模型服务响应超时，已完成内容已保留。" ? text("模型服务响应超时，已完成内容已保留。", "The model service timed out. Completed content has been retained.") : enrichmentJob?.error_code === "STATE_CONFLICT" && hasRecoveryCheckpoint ? text("事实已经提取，但尚未写入 Matrix。", "Facts were extracted but not published to the Matrix.") : text("上次科学事实分析未完成。", "The previous scientific fact analysis did not finish.")}</strong><p>{enrichmentJob?.error_message === "模型服务响应超时，已完成内容已保留。" ? text("点击“继续未完成分析”恢复。", "Choose Resume unfinished analysis to continue.") : enrichmentJob?.error_code === "STATE_CONFLICT" && hasRecoveryCheckpoint ? text("Matrix状态在任务期间发生变化。点击“继续未完成分析”可复用检查点，不会重新调用模型。", "The Matrix state changed during the job. Recover the checkpoint without calling the model again.") : text("生成章节规划时会自动重新分析；该问题不会阻止继续规划。", "Chapter planning will analyze the evidence again automatically; this does not block planning.")}</p>{enrichmentJob?.error_message && enrichmentJob.error_message !== "模型服务响应超时，已完成内容已保留。" ? <details><summary>{text("技术详情", "Technical details")}</summary><p>{enrichmentJob.error_message}</p></details> : null}</div> : null}
            </div>
            <input className="pane-search" type="search" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={text("检索Matrix", "Search matrix")} />
            <div className="paper-list">{visiblePapers.map((paper) => { const status = analysisState(paper, activeFactPaperIds.has(paper.paper_id)); return <button type="button" key={paper.paper_id} title={text(`内部论文 ID：${paper.paper_id}；事实状态：${factStatusLabel(status, text)}`, `Internal paper ID: ${paper.paper_id}; fact status: ${factStatusLabel(status, text)}`)} className={paper.paper_id === selected?.paper_id ? "paper-row active" : "paper-row"} onClick={() => setSelectedId(paper.paper_id)}><span className="paper-row-main"><strong>{paperLabels.get(paper.paper_id) || paper.paper_id} · {displayText(paper.title)}</strong><small>{paper.authors?.join(", ")}</small></span><span className={`status-pill ${factStatusClass(status)}`}>{factStatusLabel(status, text)}</span></button>; })}</div>
          </section>
          <section className="pane planning-detail-pane">

            {selected?.bibliography_identity?.verified === false ? <><div className="detail-status-notice warning planning-bibliography-notice" role="status"><strong>{text("规范书目信息待核验", "Canonical bibliography pending")}</strong><p>{selected.bibliography_identity.missing_fields?.length ? text(`待补字段：${selected.bibliography_identity.missing_fields.join("、")}。可继续内部写作，但终稿发布前需要解决。`, `Missing fields: ${selected.bibliography_identity.missing_fields.join(", ")}. Internal writing may continue, but this must be resolved before release.`) : text("题名、期刊、年份或 DOI 尚未完成核验。该论文仍可用于 Matrix 和内部写作，但终稿发布前需要确认。", "The title, venue, year, or DOI is not yet verified. The paper remains usable in the Matrix and internal writing, but must be confirmed before Final release.")}</p></div><BibliographyResolutionPanel paper={selected} onChanged={refresh} /></> : null}
            {selected ? <><div className="pane-head paper-title"><div><span className="step-label" title={text(`内部论文 ID：${selected.paper_id}`, `Internal paper ID: ${selected.paper_id}`)}>{paperLabels.get(selected.paper_id) || selected.paper_id}</span><h2>{displayText(selected.title)}</h2><p>{[selected.authors?.join(", "), selected.year, selected.journal, selected.doi].filter(Boolean).join(" · ")}</p></div><span className={`status-pill ${factStatusClass(selectedFactStatus)}`}>{factStatusLabel(selectedFactStatus, text)}</span></div><div className="planning-detail-content"><MatrixAnalysisStatus key={selected.paper_id} paper={selected} papers={papers} projectId={projectId} busy={enrichmentActive || Boolean(payload.blueprint_jobs?.some((job) => jobIsActive(job.status)))} refresh={refresh} />{selected.fact_enrichment?.processing ? <p className="message message-info">{text(`已核验 ${selected.fact_enrichment.verified_fact_count || 0} 条 · 待核验 ${selected.fact_enrichment.pending_fact_count || 0} 条`, `${selected.fact_enrichment.verified_fact_count || 0} checked · ${selected.fact_enrichment.pending_fact_count || 0} awaiting verification`)}{selected.fact_enrichment.processing.verification === "pending" ? text("。当前章节规划任务可从已有检查点继续核验，已完成事实会保留。", ". The current chapter-planning task can resume verification from its checkpoint; completed facts are retained.") : null}</p> : null}<section className="reading-field"><h3>{text("摘要", "Abstract")}</h3><p>{displayText(selected.abstract) || text("没有摘要。", "No abstract available.")}</p></section><section className="matrix-fact-section"><MatrixEvidenceUse paper={selected} /><h3>{text("已核验事实依据", "Verified source-grounded facts")}</h3>{formalClassificationTags.length ? <div className="message message-success"><strong>{text("正式证据分类", "Formal evidence classification")}</strong><p>{formalClassificationTags.map((tag) => `${tag.axis_label || tag.axis_id}: ${tag.partition_label || tag.partition_id}`).join(" · ")}</p><small>{text("每个正式分类均绑定事实 ID 与原文证据；阶段 02 初步分组不会直接用于正文 Claim。", "Every formal tag is bound to fact IDs and source evidence. Stage 02 grouping cannot directly support manuscript Claims.")}</small></div> : null}{actionRequiredOutcomes.length ? <div className="message message-warning"><strong>{text("仍需处理的分类问题", "Classification issues still requiring attention")}</strong><p>{actionRequiredOutcomes.map((outcome) => `${outlineAxisLabel(String(outcome.axis_id || ""), text)}: ${outcome.status}${outcome.reason ? ` — ${outcome.reason}` : ""}`).join(" · ")}</p></div> : null}{!formalClassificationTags.length && provisionalClassificationTags.length ? <p className="message message-info">{text("阶段 02 有初步分组，但尚未通过全文事实验证，因此只作为检索提示。", "Stage 02 has preliminary grouping, but it has not passed full-text fact validation and remains a retrieval hint only.")}</p> : null}{selected.topic_partition_classification?.status === "classified" ? <div className="message message-success"><strong>{text(`Topic 分区：${selected.topic_partition_classification.partition}`, `Topic partition: ${selected.topic_partition_classification.partition}`)}</strong><p>{text(`证据约束分类置信度 ${Math.round(Number(selected.topic_partition_classification.confidence || 0) * 100)}%。`, `Evidence-bound classification confidence ${Math.round(Number(selected.topic_partition_classification.confidence || 0) * 100)}%.`)}</p>{selected.topic_partition_classification.support_excerpt ? <details><summary>{text("查看分类原文依据", "View classification evidence")}</summary><blockquote>{selected.topic_partition_classification.support_excerpt}</blockquote><small>{selected.topic_partition_classification.evidence_ceiling}</small></details> : null}</div> : null}{(automaticallyHandledOutcomes.length || unresolvedTopicPartition) ? <details className="advanced-panel matrix-auto-resolution"><summary>{text("系统已自动处理分类边界（无需操作）", "Classification boundaries handled automatically (no action needed)")}</summary><div className="advanced-panel-body"><p>{text("系统已执行定向补证；仍无正面证据的维度不会被强制归类，论文会按已证实的反应、产物或方法事实自动路由，不影响后续写作。", "The system ran a targeted evidence check. Dimensions still lacking positive evidence are not forced; the paper is routed by verified reaction, product, or method facts without blocking writing.")}</p>{automaticallyHandledOutcomes.length ? <ul>{automaticallyHandledOutcomes.map((outcome) => <li key={`${outcome.axis_id}-${outcome.status}`}><strong>{outlineAxisLabel(String(outcome.axis_id || ""), text)}</strong>: {outcome.reason || outcome.status}</li>)}</ul> : null}{unresolvedTopicPartition && selected.topic_partition_classification.boundary_reason ? <p>{selected.topic_partition_classification.boundary_reason}</p> : null}</div></details> : null}{selected?.fact_enrichment?.status === "limited" ? <p className="message message-warning">{text("当前只有摘要级证据，不能据此扩展实验条件、机理或详细定量结论。", "Only abstract-level evidence is available; do not extend it into detailed conditions, mechanisms, or quantitative claims.")}</p> : null}{facts.length ? <div className="matrix-fact-list">{facts.map((fact) => { const ref = fact.evidence_refs?.[0]; const support = fact.support_level || (fact.epistemic_status === "abstract_level_report" ? "abstract_limited" : "direct"); return <article key={fact.fact_id || `${fact.field_id}-${ref?.chunk_id}`}><div><strong>{String(fact.field_id || "fact").replaceAll("_", " ")}</strong><span>{text(`支持：${support === "direct" ? "直接证据" : support === "abstract_limited" ? "仅摘要" : "仅上下文"}`, `Support: ${support.replaceAll("_", " ")}`)} · {ref?.page_start ? text(`第 ${ref.page_start} 页`, `Page ${ref.page_start}`) : fact.source_channel || fact.epistemic_status}</span></div><p>{fact.value}</p><details><summary>{text("查看原文依据与证据上限", "View source support and evidence ceiling")}</summary><blockquote>{fact.support_excerpt}</blockquote><small>{fact.assertion_ceiling || fact.evidence_ceiling}</small></details></article>; })}</div> : <p className="muted">{emptyFactMessage}</p>}</section><details className="advanced-panel matrix-reading-advanced"><summary>{text("全文阅读笔记与图像信息（可选）", "Full-text notes and figure data (optional)")}</summary><div className="advanced-panel-body"><section className="reading-field"><h3>{text("全文阅读笔记", "Full-text reading notes")}</h3><textarea rows={14} value={note} onChange={(event) => setNote(event.target.value)} placeholder={text("转化、条件、证据、范围、限制与综述相关性", "Transformation, conditions, evidence, scope, limitations, and relevance")} /></section><label className="check-label"><input type="checkbox" checked={complete} onChange={(event) => setComplete(event.target.checked)} />{text("已完成该论文全文阅读", "Full-text reading completed")}</label><button className="button button-primary" type="button" disabled={saveReading.isPending} onClick={() => saveReading.mutate()}>{saveReading.isPending ? text("保存中…", "Saving…") : text("保存阅读笔记", "Save reading notes")}</button>{saveReading.error ? <p className="message message-error">{saveReading.error.message}</p> : null}<details className="figure-data"><summary>{text("最相关图像信息", "Most relevant figure")}</summary><pre>{JSON.stringify(selected.most_relevant_figure || {}, null, 2)}</pre></details></div></details></div></> : <div className="empty-state">{text("Discovery确认后会在这里显示Matrix。", "The matrix appears here after Discovery is confirmed.")}</div>}
          </section>
        </div>
      ) : (
        <section className="outline-workspace-react">
          <div className="outline-hero"><div><span className="step-label">{text("步骤 1 · 综述结构", "Step 1 · Review structure")}</span><h2>{text("选择综述组织逻辑", "Choose the review structure")}</h2><p>{text("只借鉴参考综述的组织方式，不复制其主题标题和具体内容。", "Reuse only the organizational style of a reference review, not its topic headings or content.")}</p></div><span className={selectedStyle ? "badge" : "badge pending"}>{selectedStyle ? text(`当前：${selectedStyle}`, `Current: ${selectedStyle}`) : text("尚未选择", "Not selected")}</span></div>
          <div aria-live="polite">
            {chooseOutline.isError ? <div className="message message-error" role="alert">
              <strong>{text("大纲未应用：", "Outline was not applied: ")}</strong>{chooseOutline.error.message}
              {chooseOutline.error instanceof ApiError && chooseOutline.error.status === 409 ? <p>{text("项目状态发生变化，已重新获取当前状态。请重新点击需要的大纲。", "The project state changed and has been fetched again. Select the outline again.")}</p> : null}
            </div> : null}
            {chooseOutline.isSuccess ? <p className="message message-success" role="status">{chooseOutline.variables === "custom" ? text("已启用自定义大纲，请在下方填写章节并保存。", "Custom outline selected. Add sections below and save.") : text("大纲已应用，章节已载入下方编辑器。", "Outline applied. Sections are loaded in the editor below.")}</p> : null}
          </div>
          {topicOutlineCandidate ? <article className={selectedStyle === topicOutlineCandidate.outline_style ? "topic-outline-recommendation current" : "topic-outline-recommendation"}>
            <div className="topic-outline-recommendation-copy"><span className="step-label">{topicOutlineIntent?.system_recommended ? text("根据 Matrix 证据推荐", "Recommended from Matrix evidence") : text("根据你的 Topic 推荐", "Recommended from your Topic")}</span><h3>{text("主题驱动的组合大纲", "Topic-guided hybrid outline")}</h3><p>{topicOutlineIntent?.system_recommended ? text("Topic 未固定唯一章节轴，系统根据当前入选论文的正式事实推荐组织方式；仍在章节规划步骤统一确认。", "The Topic did not fix one chapter axis, so the system recommends an organization from formal facts in the selected papers. It is confirmed in the chapter planning step.") : text("系统读取了 Topic 中明确写出的组织要求，并结合当前 Matrix 分配论文。选择后仍可在下方逐节修改。", "The system read the explicit organization instructions in your Topic and assigned the current Matrix papers accordingly. Every section remains editable below.")}</p><div className="topic-outline-intent-list">
              {topicOutlineIntent?.primary_axis ? <span><strong>{text("主要组织轴", "Primary axis")}</strong>{topicOutlineIntent.primary_axis_label || outlineAxisLabel(topicOutlineIntent.primary_axis, text)}</span> : null}
              {topicOutlineIntent?.secondary_axes?.length ? <span><strong>{text("次级比较轴", "Secondary axes")}</strong>{topicOutlineIntent.secondary_axes.map((axis) => topicOutlineIntent.secondary_axis_labels?.[axis] || outlineAxisLabel(axis, text)).join(" + ")}</span> : null}
              {topicAxisExamples.length ? <span><strong>{text("组织轴示例", "Axis examples")}</strong>{topicAxisExamples.join(" · ")}</span> : null}
              {(topicOutlineIntent?.required_partitions || topicOutlineIntent?.partitions)?.length ? <span><strong>{text("分开讨论", "Separate discussion")}</strong>{(topicOutlineIntent.required_partitions || topicOutlineIntent.partitions || []).join(" / ")}</span> : null}
              {(topicOutlineIntent?.comparison_dimensions || topicOutlineIntent?.named_systems)?.length ? <span><strong>{text("比较示例", "Comparison examples")}</strong>{(topicOutlineIntent.comparison_dimensions || topicOutlineIntent.named_systems || []).join(" / ")}</span> : null}
              {(topicOutlineIntent?.focus_dimensions || topicOutlineIntent?.requested_outcomes)?.length ? <span><strong>{text("重点范围", "Focus dimensions")}</strong>{(topicOutlineIntent.focus_dimensions || topicOutlineIntent.requested_outcomes || []).join(" / ")}</span> : null}
            </div></div>
            <button className="button button-primary" type="button" {...outlineChoiceProps(String(topicOutlineCandidate.outline_style || "topic-guided"))}>{outlineChoiceText(String(topicOutlineCandidate.outline_style || "topic-guided"), text("使用推荐大纲", "Use recommended outline"), text("当前推荐大纲", "Current recommended outline"))}</button>
          </article> : null}
          <div className="outline-card-grid">{outlineStyles.map((style) => <article key={style.id} className={selectedStyle === style.id ? "outline-card current" : "outline-card"}><span>{style.icon}</span><h3>{text(style.titleZh, style.titleEn)}</h3><p>{text(style.descriptionZh, style.descriptionEn)}</p><button className="button button-secondary" type="button" {...outlineChoiceProps(style.id)}>{outlineChoiceText(style.id, text("使用此结构", "Use this structure"), text("当前选择", "Current selection"))}</button></article>)}</div>
          <details className="advanced-panel planning-reference-advanced">
            <summary>{text("上传参考综述以学习组织方式（可选）", "Upload a reference review for organization only (optional)")}</summary>
            <div className="advanced-panel-body"><section className="reference-upload"><div><h3>{text("上传综述，仅学习格式与写法", "Upload a review to learn format only")}</h3><p>{text("支持PDF、DOCX、Markdown或TXT。系统分两步处理：先提取层级、节奏和写作方式，再只根据当前主题与Matrix生成全新标题；不会复制、翻译或改写上传综述的标题和内容。", "Supports PDF, DOCX, Markdown, or TXT. The system first extracts hierarchy, pacing, and writing conventions, then generates new headings only from the current topic and Matrix. Uploaded headings and content are never copied, translated, or paraphrased.")}</p></div><label className="button button-secondary file-button">{uploadReference.isPending ? text("正在分析格式…", "Analyzing format…") : text("选择参考综述", "Choose reference review")}<input type="file" accept=".pdf,.docx,.md,.txt" disabled={uploadReference.isPending} onChange={(event) => { const file = event.target.files?.[0]; event.currentTarget.value = ""; if (file) uploadReference.mutate(file); }} /></label></section>
            {uploadReference.error ? <p className="message message-error">{uploadReference.error.message}</p> : null}
            {payload.legacy_reference_outline_count ? <p className="message message-warning">{text(`已隐藏 ${payload.legacy_reference_outline_count} 个旧版参考大纲，因为它们没有通过“只学格式”的内容隔离校验；如需使用，请重新上传原参考综述。`, `${payload.legacy_reference_outline_count} legacy reference outlines were hidden because they did not pass format-only content isolation. Upload the source review again to use it safely.`)}</p> : null}
            {payload.reference_outline_candidates?.length ? <div className="reference-candidates">{payload.reference_outline_candidates.map((candidate) => { const style = `reference:${candidate.candidate_id}`; return <button key={String(candidate.candidate_id)} className={selectedStyle === style ? "active" : ""} type="button" {...outlineChoiceProps(style)}><strong>{outlineChoiceText(style, String(candidate.source_name || candidate.candidate_id), String(candidate.source_name || candidate.candidate_id))}</strong><small>{text("仅学习格式 · 内容来自当前Matrix", "Format only · content from current Matrix")}</small></button>; })}</div> : null}</div>
          </details>
          <details className="surface scope-contract-editor">
            <summary className="scope-contract-heading">
              <div>
                <span className="step-label">{text("写作约束", "Writing contract")}</span>
                <h2>{text("综述范围与学术目标", "Review scope and academic objective")}</h2>
                <p>{text("用于统一后续大纲、章节论证与结论的研究方向。系统会根据主题和 Matrix 自动生成，无需单独确认；仅在方向不准确时修改，保存大纲时一并保存。", "Keeps the outline, section arguments, and conclusions aligned to one research direction. It is generated from the topic and Matrix with no separate confirmation; edit only when the direction is inaccurate, then save it with the outline.")}</p>
              </div>
              <span className={payload.scope_diagnostics?.can_confirm === false ? "badge pending" : "badge"}>{payload.scope_diagnostics?.can_confirm === false ? text("需要补充", "Needs attention") : text("已自动生成", "Generated")}</span>
            </summary>
            <div className="scope-contract-body"><div className="scope-contract-grid">
              <label className="outline-builder-field scope-contract-field">
                <span>{text("核心研究问题", "Central review question")}</span>
                <textarea rows={3} value={String(scopeDraft.target_question || "")} onChange={(event) => setScopeDraft((current) => ({ ...current, target_question: event.target.value }))} />
              </label>
              <label className="outline-builder-field scope-contract-field">
                <span>{text("综述目标与学术贡献", "Review objective and contribution")}</span>
                <textarea rows={3} value={String(scopeDraft.review_objective || "")} onChange={(event) => setScopeDraft((current) => ({ ...current, review_objective: event.target.value }))} />
              </label>
              <label className="outline-builder-field scope-contract-field scope-contract-field-compact">
                <span>{text("主要组织轴", "Primary navigation axis")}</span>
                <input value={String(scopeDraft.primary_navigation_axis || "").replaceAll("_", " ")} onChange={(event) => setScopeDraft((current) => ({ ...current, primary_navigation_axis: event.target.value }))} />
              </label>
              <label className="outline-builder-field scope-contract-field scope-contract-field-compact">
                <span>{text("目标读者", "Target readers")}</span>
                <input value={(scopeDraft.target_readers || []).join(", ")} onChange={(event) => setScopeDraft((current) => ({ ...current, target_readers: event.target.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean) }))} />
                <small>{text("多类读者请用逗号分隔", "Separate multiple reader groups with commas")}</small>
              </label>
              <label className="outline-builder-field scope-contract-field scope-contract-field-compact">
                <span>{text("检索截止日期", "Search cutoff date")}</span>
                <input type="date" value={String(scopeDraft.search_cutoff_date || "")} onChange={(event) => setScopeDraft((current) => ({ ...current, search_cutoff_date: event.target.value }))} />
                <small>{text("用于说明本综述实际覆盖到哪个日期，不代表全领域覆盖率。", "Records how current the selected corpus is; it does not imply global coverage.")}</small>
              </label>
            </div>
            {payload.scope_diagnostics?.issues?.map((issue) => <p className="message message-warning" key={issue.rule_id}>{issue.message}</p>)}
            <div className="coverage-diagnostics-summary">
              <div><strong>{payload.coverage_diagnostics?.selected_paper_count || 0}</strong><span>{text("已选论文", "Selected papers")}</span></div>
              <div><strong>{Object.keys(payload.coverage_diagnostics?.year_distribution || {}).length}</strong><span>{text("年份区间点", "Publication years")}</span></div>
              <div><strong>{Object.keys(payload.coverage_diagnostics?.source_distribution || {}).length}</strong><span>{text("来源期刊", "Source venues")}</span></div>
              <div><strong>{payload.coverage_diagnostics?.topic_clusters?.length || 0}</strong><span>{text("基础主题簇", "Basic topic clusters")}</span></div>
            </div>
            {payload.coverage_diagnostics?.warnings?.map((issue) => <p className="message message-warning" key={issue.rule_id}>{issue.rule_id === "coverage.search_cutoff_unrecorded" ? text("尚未记录检索截止日期。", "The search cutoff date has not been recorded.") : issue.rule_id === "coverage.publication_year_missing" ? text("部分已选论文缺少规范化发表年份。", "Some selected papers have no normalized publication year.") : text("覆盖信息不完整。", issue.message || "Coverage information is incomplete.")}</p>)}</div>
          </details>
          <section className="outline-editor-card"><div className="section-heading"><div><h2>{text("新手大纲编辑器", "Beginner outline editor")}</h2><p>{text("大标题和小标题必须对应当前检索主题；新手模式会自动生成系统需要的格式。", "Every heading and subheading must match the current discovery topic; beginner mode generates the required format automatically.")}</p></div><div className="outline-editor-actions"><button className="button button-secondary" type="button" disabled={!outlineReady || recommendOutline.isPending || saveOutline.isPending || chooseOutline.isPending} onClick={() => recommendOutline.mutate(outlineDraft)}>{recommendOutline.isPending ? text("正在分析主题覆盖…", "Analyzing evidence…") : text("为整个大纲推荐论文", "Recommend papers for the whole outline")}</button><button className="button button-primary" type="button" disabled={!outlineReady || saveOutline.isPending || recommendOutline.isPending || chooseOutline.isPending} onClick={() => saveOutline.mutate()}>{saveOutline.isPending ? text("保存中…", "Saving…") : text("保存大纲", "Save outline")}</button></div></div>{recommendationMessage ? <p className="message message-info">{recommendationMessage}</p> : null}<OutlineBuilder value={outlineDraft} papers={papers} onChange={setOutlineDraft} />{!outlineReady && outlineDraft.trim() ? <p className="message message-warning">{text("请至少添加一个章节，并补全章节标题。", "Add at least one section and complete every section title.")}</p> : null}{recommendOutline.error ? <p className="message message-error">{recommendOutline.error.message}</p> : null}{saveOutline.error ? <p className="message message-error">{saveOutline.error.message}</p> : null}</section>
          <details className="outline-options"><summary>{text("查看系统生成的候选大纲", "View system-generated outline candidates")}</summary><pre>{payload.outline_options_md || text("暂无候选大纲。", "No candidate outlines yet.")}</pre></details>
        </section>
      )}
    </>
  );
}

function BlueprintArgumentSummary({ section, paperTitles }: { section: BlueprintSection; paperTitles: Map<string, string> }) {
  const { text } = useUiText();
  const roles: Record<string, string> = {
    foundation: text("方法起点", "Foundation"), main_progress: text("主要进展", "Main progress"),
    scope_extension: text("范围扩展", "Scope extension"), mechanistic_evidence: text("机制依据", "Mechanistic evidence"),
    counterevidence: text("反证或反例", "Counterevidence"), background: text("背景", "Background"),
  };
  return <>
    <div className="blueprint-summary-grid">
      <section><h3>{section.thesis_status === "provisional" ? text("写作目标", "Writing objective") : text("核心论点", "Core argument")}</h3><p>{displayText(section.writing_objective || section.scientific_thesis?.text || section.section_thesis || section.section_goal) || "—"}</p></section>
      <section><h3>{text("已分配论文", "Assigned papers")}</h3><strong>{(section.primary_papers || section.major_papers || section.assigned_papers || []).length}</strong></section>
      <section><h3>{text("检索方向", "Retrieval directions")}</h3><strong>{section.retrieval_directions?.length ?? 0}</strong></section>
      <section><h3>{text("待回答问题", "Questions to answer")}</h3><strong>{section.questions_to_answer?.length ?? section.scientific_thesis?.open_questions?.length ?? 0}</strong></section>
    </div>
    {section.organizing_thread ? <section><h3>{text("小节论证主线", "Section thread")}</h3><p>{section.organizing_thread}</p>
      {section.paragraph_tasks?.length ? <ol>{section.paragraph_tasks.map((task, index) => <li key={index}>{task}</li>)}</ol> : null}
    </section> : null}
    {section.planning_notes?.map((note, index) => <p className="message message-info" key={index}>{note}</p>)}
    {section.evidence_readiness?.reason ? <p className={`message message-${section.generation_eligible ? "info" : "warning"}`}>{section.evidence_readiness.reason}</p> : null}
    {section.paper_roles?.length ? <section><h3>{text("论文在本章中的作用", "Paper roles in this section")}</h3>
      <ul>{section.paper_roles.map((paper) => <li key={paper.paper_id}><strong>{paperTitles.get(paper.paper_id) || paper.paper_id}</strong>
        {" · "}{roles[paper.role] || paper.role}{paper.presentation === "table" ? text(" · 表格为主", " · Table-focused")
          : paper.presentation === "supporting_citation" ? text(" · 辅助引用", " · Supporting citation") : ""}：{paper.reason}</li>)}</ul>
    </section> : null}
    {section.coverage_by_use?.length ? <section><h3>{text("论证证据覆盖", "Argument evidence coverage")}</h3>
      <ul>{section.coverage_by_use.map((use) => <li key={use.use_id}>{use.purpose}{" · "}
        {use.status === "supported" ? text("所需证据已覆盖", "Required evidence covered") : text("关键证据待补充", "Core evidence pending")}
        {use.missing_requirements?.length ? ` (${use.missing_requirements.join("、")})` : ""}</li>)}</ul>
    </section> : null}
    {section.scientific_claims?.length ? <section className="wide"><h3>{text("事实支持的科学断言", "Fact-grounded scientific claims")}</h3>
      <ol>{section.scientific_claims.map((claim) => {
        const supported = claim.support_status === "supported" && Boolean(claim.fact_ids?.length) && Boolean(claim.evidence_refs?.length);
        const paperNames = (claim.primary_papers || []).map((id) => paperTitles.get(id) || id).join("；");
        return <li key={claim.claim_id}><strong>{claim.required_for_section === false ? text("支撑材料", "Supporting material") : text("核心论点", "Core argument")}
          {" · "}{supported ? text("可写", "Writeable") : text("待正文核实", "To be checked during drafting")}</strong>{" · "}
          {supported ? claim.allowed_assertion || claim.proposition : text(`需要补充 ${claim.required_fact_roles?.join("、") || "相关"} 事实`, `Needs ${claim.required_fact_roles?.join(", ") || "relevant"} facts`)}
          {paperNames ? <small> · {paperNames}</small> : null}</li>;
      })}</ol>
    </section> : null}
    {section.questions_to_answer?.length ? <section><h3>{text("本章需要回答的问题", "Questions to answer")}</h3><ul>{section.questions_to_answer.map((question) => <li key={question}>{question}</li>)}</ul></section> : null}
    {section.retrieval_directions?.length ? <section><h3>{text("正文检索方向", "Retrieval directions for drafting")}</h3><ul>{section.retrieval_directions.map((direction) => <li key={direction}>{direction}</li>)}</ul></section> : null}
    {section.depth_contract ? <p className="message message-info">{text(
      `篇幅参考：${section.depth_contract.target_paragraph_count || 0} 段，${section.depth_contract.target_word_min || 0}–${section.depth_contract.target_word_max || 0} 词。比较和表格按本章论证需要安排。`,
      `Length guide: ${section.depth_contract.target_paragraph_count || 0} paragraphs, ${section.depth_contract.target_word_min || 0}–${section.depth_contract.target_word_max || 0} words. Comparisons and tables follow the section's argument.`)}</p> : null}
  </>;
}

function BlueprintWorkspace({
  payload,
  onRestorePrevious,
  restoring,
}: {
  payload: PlanningPayload;
  onRestorePrevious?: (artifactId: string) => void;
  restoring?: boolean;
}) {
  const { text } = useUiText();
  const sections = payload.section_blueprint?.sections || [];
  const [selectedId, setSelectedId] = useState(String(sections[0]?.section_id || ""));
  const [advancedDetail, setAdvancedDetail] = useState<"raw" | "plan" | "outline">("raw");
  const section = sections.find((item) => String(item.section_id) === selectedId) || sections[0];
  const issues = [...(payload.scope_diagnostics?.issues || []), ...(payload.taxonomy_diagnostics?.issues || [])];
  const adjustments = payload.section_blueprint?.auto_routing_adjustments || [];
  const restructure = payload.section_blueprint?.restructure_record;
  const resolvedOutline = payload.section_blueprint?.resolved_outline_md || payload.selected_outline_md;
  const adjustedPaperCount = new Set(adjustments.flatMap((item) => item.paper_ids || [])).size;
  const adjustedTargets = [...new Set(adjustments.map((item) => item.target_section).filter((target): target is string => Boolean(target)))];
  const paperTitles = new Map(
    (payload.blueprint_candidate_inputs?.literature_matrix?.rows || payload.literature_matrix?.rows || []).map((paper) => [
      paper.paper_id,
      typeof paper.title === "string" ? paper.title : paper.paper_id,
    ]),
  );
  return (
    <div className="blueprint-grid-react">
      {payload.section_blueprint?.unused_papers?.length ? <details className="advanced-panel" style={{ gridColumn: "1 / -1" }}>
        <summary>{text(`${payload.section_blueprint.unused_papers.length} 篇论文未纳入本次综述 · 查看原因`, `${payload.section_blueprint.unused_papers.length} papers not included · View reasons`)}</summary>
        <div className="advanced-panel-body">
          <p>{text("确认章节规划时一并采用此清单；论文仍保留在文献库中。", "This list is included when you confirm the chapter plan; the papers remain in your library.")}</p>
          <ul>{payload.section_blueprint.unused_papers.map((item) => <li key={item.paper_id}><strong>{paperTitles.get(item.paper_id) || item.paper_id}</strong>：{item.reason}</li>)}</ul>
        </div>
      </details> : null}
      <section className="pane blueprint-section-list">
        <div className="pane-head"><div><span className="step-label">{text("章节列表", "Chapter list")}</span><h2>{sections.length} {text("个章节", "sections")}</h2></div></div>
        <div className="keyword-list">{sections.map((item) => {
          const papers = item.primary_papers || item.major_papers || item.assigned_papers || [];
          const planningLabel = item.planning_status === "planned"
            ? text("规划完成", "Plan complete")
            : item.planning_status === "incomplete"
              ? text("规划未完成", "Plan incomplete")
              : text("待规划", "Awaiting planning");
          return <button key={String(item.section_id)} type="button" className={item === section ? "active" : ""} onClick={() => setSelectedId(String(item.section_id))}>
            <strong>{String(item.section_id || "")} · {String(item.title || text("无标题", "Untitled"))}</strong>
            <small>{text(`已分配 ${papers.length} 篇论文`, `${papers.length} assigned papers`)} · {planningLabel}</small>
          </button>;
        })}</div>
      </section>
      <section className="pane blueprint-detail-react"><div className="pane-head blueprint-detail-head"><div><span className="step-label">{text("章节规划摘要", "Chapter plan summary")}</span><h2>{section?.title || text("章节规划", "Chapter plan")}</h2></div></div>{issues.length ? <div className="planning-diagnostics">{issues.map((issue) => <p className="message message-warning" key={`${issue.rule_id}-${issue.message}`}>{issue.message}</p>)}</div> : <div className="blueprint-health-row"><span className="blueprint-health-status"><i />{text("范围与论文分配检查通过", "Scope and paper assignment checks passed")}</span>{adjustments.length ? <details className="blueprint-routing-details"><summary><strong>{text(`${adjustedPaperCount} 篇论文路由已调整`, `${adjustedPaperCount} paper routes adjusted`)}</strong><span>{text("查看记录", "View log")}</span></summary><div className="blueprint-routing-detail-body"><p>{text("系统依据已核验事实和原文证据完成调整，无需逐项确认。", "Routes were adjusted from verified facts and source-addressable evidence; no separate confirmation is required.")}</p><div>{adjustedTargets.map((target) => <span key={target}>{target}</span>)}</div></div></details> : <span className="blueprint-routing-none">{text("无需调整论文路由", "No route adjustments needed")}</span>}</div>}
        {restructure?.is_restructure ? <details className="blueprint-restructure-note"><summary>{text("本版本包含结构调整", "This version contains structural changes")}</summary><p>{restructure.application_mode === "auto_applied_before_section_generation" ? text("章节尚未生成，系统已安全应用新结构；旧章节规划仍可回滚。", "No section prose existed, so the new structure was applied safely; the prior chapter plan remains available for rollback.") : text("旧结构和章节映射已保存。存在下游内容时，本版本仍使用现有章节规划确认流程，不会静默覆盖人工内容。", "The old structure and section map were retained. When downstream content exists, the chapter plan confirmation flow is used and manual content is not silently overwritten.")}</p><ul>{(restructure.section_mapping || []).filter((item) => item.previous_section_id || item.current_section_id).map((item, index) => <li key={`${item.previous_section_id || "new"}-${item.current_section_id || "retired"}-${index}`}>{item.previous_title || text("新增章节", "New section")} → {item.current_title || text("已撤销", "Retired")}</li>)}</ul>{restructure.rollback_supported && restructure.previous_blueprint_artifact_id && onRestorePrevious ? <button className="button button-secondary" type="button" disabled={restoring} onClick={() => onRestorePrevious(restructure.previous_blueprint_artifact_id!)}>{restoring ? text("正在恢复…", "Restoring…") : text("恢复上一版本", "Restore previous version")}</button> : null}</details> : null}
        {section ? <BlueprintArgumentSummary section={section} paperTitles={paperTitles} /> : <div className="empty-state">{text("请先生成章节规划。", "Generate a chapter plan first.")}</div>}
        {section?.scientific_thesis?.argument_purpose ? <div className="blueprint-summary-grid">
          <section><h3>{text("科学问题", "Scientific question")}</h3><p>{displayText(section.review_problem)}</p></section>
          <section><h3>{text("论证目的", "Argument purpose")}</h3><p>{section.scientific_thesis.argument_purpose}</p></section>
          <section><h3>{text("比较轴与适用边界", "Comparison axes and boundaries")}</h3><p>{[...(section.scientific_thesis.comparison_axes || []), ...(section.scientific_thesis.boundaries || [])].join(" · ")}</p></section>
          <section><h3>{text("仍待补充的问题", "Open evidence questions")}</h3><p>{section.scientific_thesis.open_questions?.join(" · ") || text("本次未提出额外问题", "No additional question proposed in this analysis")}</p></section>
        </div> : null}
        <details className="advanced-panel blueprint-advanced-detail"><summary>{text("查看完整章节规划 与生成依据", "View full chapter plan and generation inputs")}</summary><div className="advanced-panel-body"><nav className="advanced-tab-list"><button type="button" className={advancedDetail === "raw" ? "active" : ""} onClick={() => setAdvancedDetail("raw")}>{text("完整字段", "Full fields")}</button><button type="button" className={advancedDetail === "plan" ? "active" : ""} onClick={() => setAdvancedDetail("plan")}>{text("写作计划", "Writing plan")}</button><button type="button" className={advancedDetail === "outline" ? "active" : ""} onClick={() => setAdvancedDetail("outline")}>{text("选定大纲", "Selected outline")}</button></nav>{advancedDetail === "raw" ? <div className="blueprint-json-fields">{section ? Object.entries(section).filter(([, value]) => value !== null && value !== "" && (!Array.isArray(value) || value.length)).map(([key, value]) => <section key={key}><h3>{key.replaceAll("_", " ")}</h3><pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></section>) : null}</div> : <pre className="markdown-preview">{advancedDetail === "plan" ? payload.section_writing_plan_md : resolvedOutline}</pre>}</div></details>
      </section>
    </div>
  );
}

function ChapterPlanningActions({ payload, tab, generating, confirming, error, onGenerate, onConfirm, onOpen, refresh }: {
  payload: PlanningPayload;
  tab: "matrix" | "blueprint";
  generating: boolean;
  confirming: boolean;
  error?: string;
  onGenerate: () => void;
  onConfirm: () => void;
  onOpen: () => void;
  refresh: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const jobs = payload.blueprint_jobs || [];
  const activeJob = jobs.find((job) => jobIsActive(job.status));
  const latestJob = activeJob || jobs[0];
  const cancel = useMutation({
    mutationFn: () => apiRequest(`/api/v1/jobs/${encodeURIComponent(activeJob!.id)}/cancel`, {
      method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() },
    }),
    onSuccess: refresh,
  });
  const progress = activeJob?.result?.blueprint_progress as { phase?: string; active_sections?: string[] } | undefined;
  const pipeline = activeJob?.result?.planning_pipeline as {
    phase?: "fact_enrichment" | "chapter_planning" | "completed";
  } | undefined;
  const factIntegration = (activeJob?.result?.integrated_fact_enrichment
    || latestJob?.result?.integrated_fact_enrichment) as {
    status?: "completed" | "degraded" | "skipped" | "current" | "not_requested";
    enriched_paper_count?: number;
    reason?: string;
  } | undefined;
  const academic = payload.section_blueprint?.academic_planning;
  const sectionCount = payload.section_blueprint?.sections?.length || 0;
  const complete = Boolean(payload.blueprint_current && academic?.status === "completed");
  const incomplete = academic?.status === "incomplete";
  const busy = generating || Boolean(activeJob);
  const canGenerate = !busy && !confirming && payload.outline_current;
  const canResume = !complete && payload.outline_current && (incomplete || latestJob?.available_actions.includes("retry"));
  const title = busy && pipeline?.phase === "fact_enrichment"
    ? text("正在分析章节所需科学事实", "Analyzing scientific facts needed for the chapter plan")
    : activeJob?.status === "queued" ? text("章节规划已排队", "Chapter plan queued") : busy ? text("章节规划正在生成", "Generating chapter plan")
    : complete ? text(`章节规划已完成 · ${sectionCount} 个章节`, `Chapter plan complete · ${sectionCount} sections`)
    : incomplete ? text("章节规划尚未完成", "Chapter plan incomplete")
    : sectionCount ? text("章节规划需要更新", "Chapter plan needs updating")
    : text("生成章节规划", "Generate chapter plan");
  const description = busy && pipeline?.phase === "fact_enrichment"
    ? text("系统正从当前论文全文中提取并核验证据；失败时会自动退回原文段落检索，不会阻断规划。", "The system is extracting and verifying evidence from the current full texts. If this step fails, planning falls back to source-passage retrieval without being blocked.")
    : activeJob?.status === "queued" ? text("若科学事实正在提取，系统会等待完成后自动接续规划，无需重复提交。", "If fact extraction is active, planning will continue automatically when it completes. No resubmission is needed.") : busy ? text("已完成的步骤会保留，可停止后继续。", "Completed steps are saved; you can stop and continue later.")
    : !payload.outline_current ? text("请先在“大纲选择与上传”中选择或保存当前大纲。", "Choose or save the current outline first.")
    : sectionCount && !payload.blueprint_current ? text("当前显示的是旧章节规划。请根据当前文献和大纲重新生成后再确认。", "The displayed chapter plan is outdated. Regenerate it from the current papers and outline before confirming.")
    : incomplete ? text(`未完成章节：${academic?.incomplete_sections?.join("、") || "待生成"}。继续生成会复用已完成步骤。`, `Unfinished sections: ${academic?.incomplete_sections?.join(", ") || "pending"}. Continue to resume pending work.`)
    : complete ? text("确认后应用暂定论点与论文安排；证据缺口留待正文阶段处理。", "Confirmation applies provisional arguments and paper assignments; evidence gaps are addressed during drafting.")
    : text("复用已提取且有效的事实，只对缺失或失效的证据补充分析，再安排章节写作目标与检索方向。", "Reuse valid extracted facts and analyze missing or outdated evidence before planning chapter objectives and retrieval.");
  return <section className="stage-action-bar" aria-live="polite">
    <div><strong>{title}</strong><p>{description}</p>
      {activeJob ? <>{pipeline?.phase === "fact_enrichment" ? <p>{text("正在核验当前主题需要的事实依据", "Verifying the facts needed for the current topic")}</p> : progress?.phase === "structure" ? <p>{text("正在组织全篇结构", "Planning the overall structure")}</p> : progress?.active_sections?.length ? <p>{text("正在规划章节：", "Planning sections: ")}{progress.active_sections.join("、")}</p> : null}<p>{activeJob.progress_current}/{activeJob.progress_total || "—"} {pipeline?.phase === "fact_enrichment" ? text("篇论文已分析", "papers analyzed") : text("章节已处理", "sections processed")}</p>
        <progress aria-label={pipeline?.phase === "fact_enrichment" ? text("科学事实分析进度", "Scientific fact analysis progress") : text("章节规划进度", "Chapter planning progress")} value={activeJob.progress_current} max={activeJob.progress_total || 1} /></> : null}
      {!busy && factIntegration?.status === "degraded" ? <p className="message message-warning">{text("本次事实分析未完全完成，章节规划已使用原文段落检索安全继续。", "Fact analysis was incomplete, so chapter planning continued safely with source-passage retrieval.")}</p> : null}
      {!busy && payload.section_blueprint?.fact_source_issues?.length ? <details className="message message-info">
        <summary>{text(`已自动处理 ${payload.section_blueprint.fact_source_issues.length} 条事实来源问题，规划可继续`, `Automatically handled ${payload.section_blueprint.fact_source_issues.length} fact-source issues; planning can continue`)}</summary>
        <p>{text("有效的旧事实仍保留；无法核实的新内容暂不作为写作依据，不需要为此重新生成全部资料。", "Valid previous facts were retained; unverified new content is withheld from writing evidence. A full restart is not required.")}</p>
        <ul>{payload.section_blueprint.fact_source_issues.map((issue) => <li key={`${issue.paper_id}:${issue.fact_id}`}>
          <strong>{buildPaperDisplayLabels(payload.literature_matrix?.rows || []).get(issue.paper_id) || issue.paper_id}</strong>
          {" · "}{issue.action === "retained_previous" ? text("已保留有效旧事实", "Valid previous fact retained") : text("该事实暂不采用", "Fact withheld")}
          <p>{issue.value}</p>
          <small>{issue.reasons.map((reason) => ({ verification_outdated: text("审核记录与当前事实不一致", "Verification does not match the current fact"), unregistered_source: text("无法定位登记的原文片段", "Registered source passage could not be located"), source_excerpt_mismatch: text("引文与原文不匹配", "Quotation does not match the source"), source_lineage_mismatch: text("原文版本不一致", "Source version differs"), numeric_token_mismatch: text("数字未得到引文支持", "Numbers are not supported by the quotation") }[reason] || text("原文支持校验未通过", "Source-support validation failed"))).join("；")}</small>
        </li>)}</ul>
      </details> : null}
      {!busy && latestJob?.error_message ? <details open={!complete}><summary>{text("上次生成未完成 · 查看原因", "Previous attempt did not finish · View reason")}</summary><p>{latestJob.error_message}</p></details> : null}
      {error || cancel.error ? <p className="message message-error" role="alert">{error || cancel.error?.message}</p> : null}
    </div>
    {activeJob ? <button className="button button-secondary" type="button" disabled={cancel.isPending || activeJob.cancellation_requested} onClick={() => cancel.mutate()}>{activeJob.cancellation_requested ? text("正在停止…", "Stopping…") : text("停止生成", "Stop generation")}</button>
      : tab === "matrix" && sectionCount && payload.blueprint_current ? <button className="button button-primary" type="button" onClick={onOpen}>{text("查看章节规划", "Review chapter plan")}</button>
      : <button className={`button ${sectionCount ? "button-secondary" : "button-primary"}`} type="button" disabled={!canGenerate} onClick={onGenerate}>
        {generating ? text("正在生成…", "Generating…") : canResume ? text("继续生成", "Continue generation") : sectionCount ? text("重新生成", "Regenerate") : text("生成章节规划", "Generate chapter plan")}
      </button>}
    {tab === "blueprint" && sectionCount ? <button className="button button-primary" type="button" disabled={confirming || busy || !complete} onClick={onConfirm}>{confirming ? text("确认中…", "Confirming…") : text("确认并进入章节", "Confirm and enter sections")}</button> : null}
  </section>;
}

export function PlanningPage() {
  const { text } = useUiText();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") === "blueprint" ? "blueprint" : "matrix";
  const { selected: project } = useSelectedProject();
  const planning = useQuery({
    queryKey: ["planning", project?.project_id || ""],
    queryFn: () => apiRequest<PlanningPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning`),
    enabled: Boolean(project),
    refetchInterval: (query) => [...(query.state.data?.matrix_enrichment?.jobs || []), ...(query.state.data?.blueprint_jobs || [])].some((job) => jobIsActive(job.status)) ? 1500 : false,
  });
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["planning", project?.project_id || ""] });
    return planning.refetch();
  };
  usePlanningCompletionSync(project?.project_id || "", [
    ...(planning.data?.matrix_enrichment?.jobs || []),
    ...(planning.data?.blueprint_jobs || []),
  ], planning.refetch);
  const generateBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint/jobs`, {
      method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision }),
      headers: { "Idempotency-Key": newIdempotencyKey() },
    }),
    onSuccess: async () => {
      await refresh();
      const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next);
    },
  });
  const confirmBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint/confirm`, { method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision, artifact_id: planning.data!.blueprint_artifact_id }) }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(`/sections?project=${encodeURIComponent(project!.project_id)}`);
    },
  });
  const restoreBlueprint = useMutation({
    mutationFn: (artifactId: string) => apiRequest(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint/restore`,
      { method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision, artifact_id: artifactId }) },
    ),
    onSuccess: refresh,
  });
  const matrixEnrichmentPublishFailed = Boolean(
    planning.data?.matrix_enrichment?.failed_publish_with_pending_rows
  );
  const allMatrixFactsFailed = Boolean(
    planning.data?.matrix_enrichment?.all_failed
  );
  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 3 · 分析与规划", "Stage 3 · Analysis and planning")}</p><h1>{text("Matrix与综述大纲", "Matrix and review outline")}</h1><p className="muted">{text("从确认文献形成Matrix，选择大纲逻辑，再生成可审核的章节规划。", "Build the matrix from confirmed papers, choose an outline logic, then generate a reviewable chapter plan.")}</p></div><ProjectSelector /></div>
      <nav className="workspace-step-tabs"><button type="button" className={tab === "matrix" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.set("tab", "matrix"); setSearchParams(next); }}>1 {text("文献Matrix与大纲", "Literature matrix and outline")}</button><button type="button" className={tab === "blueprint" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next); }}>2 {text("章节规划", "Chapter plan")}</button></nav>
      {planning.isPending ? <div className="empty-state">{text("正在加载Planning产物…", "Loading planning artifacts…")}</div> : null}
      {planning.error ? <ErrorState error={planning.error} onRetry={() => planning.refetch()} /> : null}
      {planning.data && matrixEnrichmentPublishFailed ? <section className="message message-warning planning-limited-mode"><div><strong>{text("科学事实已经提取，但尚未写入 Matrix", "Scientific facts were extracted but not published")}</strong><p>{text("请回到“文献 Matrix”点击“继续未完成分析”。系统会复用已完成的检查点，不会重新调用模型。", "Return to the Literature Matrix and choose Resume unfinished analysis. The completed checkpoint will be reused without another model call.")}</p></div></section> : null}
      {planning.data && allMatrixFactsFailed ? <section className="message message-warning"><strong>{text("科学事实提取尚未成功", "Scientific facts are not yet available")}</strong><p>{text("可以继续生成暂定章节规划，证据缺口留待正文阶段处理；也可以回到 Matrix 重试提取。", "You can generate a provisional chapter plan and address evidence gaps during drafting, or retry extraction in Matrix.")}</p></section> : null}
      {planning.data && project ? <>
        {tab === "matrix" ? <MatrixWorkspace payload={planning.data} projectId={project.project_id} refresh={refresh} /> : null}
        <ChapterPlanningActions payload={planning.data} tab={tab} generating={generateBlueprint.isPending} confirming={confirmBlueprint.isPending}
          error={(generateBlueprint.error || confirmBlueprint.error || restoreBlueprint.error)?.message}
          onGenerate={() => generateBlueprint.mutate()} onConfirm={() => confirmBlueprint.mutate()}
          onOpen={() => { const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next); }} refresh={refresh} />
        {tab === "blueprint" ? <BlueprintWorkspace payload={planning.data} restoring={restoreBlueprint.isPending} onRestorePrevious={(artifactId) => restoreBlueprint.mutate(artifactId)} /> : null}
      </> : null}
    </main>
  );
}
