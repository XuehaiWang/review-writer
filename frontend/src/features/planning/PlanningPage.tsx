import { LocalizedError } from "../../components/LocalizedError";
import { useEffect, useMemo, useState } from "react";
import { useFormDraft } from "../../hooks/useFormDraft";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, apiRequest, jsonBody } from "../../api/client";
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
import { TopicRecommendationPending, type TopicRecommendationState } from "./TopicRecommendationPending";
import { displayText } from "./planningText";

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

export type BlueprintSection = Record<string, unknown> & {
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

export type PlanningPayload = {
  topic_recommendation?: TopicRecommendationState | null;
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
  outline_ready_for_chapter_planning?: boolean;
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
  blueprint_approved?: boolean;
  active_blueprint_approved?: boolean;
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
  { id: "custom", icon: "E", titleZh: "自定义大纲", titleEn: "Custom outline", descriptionZh: "在下方按模块填写，也可使用代码填写。保存后才会应用。", descriptionEn: "Edit modules below or use the code editor. Changes apply when saved." },
];

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

function MatrixWorkspace({ payload, projectId, refresh, mode }: { payload: PlanningPayload; projectId: string; refresh: () => Promise<unknown>; mode: "reading" | "outline" }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [outlineSyncFailed, setOutlineSyncFailed] = useState(false);
  const [filter, setFilter] = useState("");
  const papers = payload.literature_matrix?.rows || [];
  const [selectedId, setSelectedId] = useState(papers[0]?.paper_id || "");
  const selected = papers.find((paper) => paper.paper_id === selectedId) || papers[0];
  const reading = useFormDraft(`reading:${projectId}:${selected?.paper_id || "none"}`, { note: selected?.main_content || "", complete: Boolean(selected?.full_reading_complete || selected?.reading_complete) });
  const { note, complete } = reading.value;
  const setNote = (note: string) => reading.set({ ...reading.value, note });
  const setComplete = (complete: boolean) => reading.set({ ...reading.value, complete });
  const outline = useFormDraft(`outline:${projectId}`, payload.selected_outline_md || "");
  const outlineDraft = outline.value;
  const setOutlineDraft = outline.set;
  const [outlineSaveMessage, setOutlineSaveMessage] = useState("");
  const [customEditing, setCustomEditing] = useState(false);
  const scope = useFormDraft<ScopeContract>(`scope:${projectId}`, payload.scope_contract || {});
  const scopeDraft = scope.value;
  const setScopeDraft = scope.set;
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

  const saveReading = useMutation({
    onMutate: () => reading.checkpoint(),
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/matrix/${encodeURIComponent(selected!.paper_id)}`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, main_content: note, most_relevant_figure: selected?.most_relevant_figure || null, mark_complete: complete }),
    }),
    onSuccess: async (_result, _variables, committed) => { await refresh(); committed?.(); },
  });
  const chooseOutline = useMutation({
    onMutate: () => outline.checkpoint(),
    mutationFn: (outlineStyle: string) => apiRequest<Partial<PlanningPayload>>(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: outlineStyle,
        ...(outlineStyle === "topic-guided" ? { candidate_outline_md: payload.outline_candidates?.find(candidate => candidate.source === "topic")?.outline_md } : {}),
      }),
    }),
    onSuccess: (result, style, committed) => {
      setCustomEditing(false);
      setOutlineSyncFailed(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      queryClient.setQueryData<PlanningPayload>(["planning", projectId], current => current ? {
        ...current, ...result, outline_current: true,
        outline_selection: { ...current.outline_selection, outline_style: style },
      } : current);
      void refresh().then(() => { committed?.(); setOutlineSyncFailed(false); }).catch(() => setOutlineSyncFailed(true));
    },
    onError: async (error) => {
      if (error instanceof ApiError && error.status === 409) await refresh();
    },
  });
  const saveOutline = useMutation<{ selected_outline_md: string; section_id_repairs?: Array<{ section_index: number }> }, Error, void, () => void>({
    onMutate: () => {
      const outlineCommitted = outline.checkpoint();
      const scopeCommitted = scope.checkpoint();
      return () => { outlineCommitted(); scopeCommitted(); };
    },
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: customEditing ? "custom" : String(payload.outline_selection?.outline_style || "custom"), outline_md: outlineDraft, scope_contract: scopeDraft }),
    }),
    onSuccess: (result, _variables, committed) => {
      setCustomEditing(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      const repairedSections = result.section_id_repairs || [];
      setOutlineSaveMessage(repairedSections.length
        ? text(
          `大纲已保存；已自动修复 ${repairedSections.length} 处重复的章节标识（第 ${repairedSections.map(item => item.section_index).join("、")} 节），章节内容未改变。`,
          `Outline saved. Repaired ${repairedSections.length} duplicate section identifier(s) in section(s) ${repairedSections.map(item => item.section_index).join(", ")}; section content was unchanged.`,
        )
        : text("大纲已保存。", "Outline saved."));
      return refresh().then(() => committed?.());
    },
  });
  const recommendOutline = useMutation<OutlineRecommendationResponse, Error, string>({
    mutationFn: (sourceOutline) => apiRequest<OutlineRecommendationResponse>(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline/recommendations`, {
      method: "POST",
      ...jsonBody({ revision: payload.matrix_revision, outline_md: sourceOutline }),
    }),
    onSuccess: (result, sourceOutline) => { if (outlineDraft === sourceOutline) setOutlineDraft(applyOutlinePaperRecommendations(outlineDraft, result.sections)); },
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
  const selectedPreset = outlineStyles.find(style => style.id === selectedStyle);
  const outlineDirty = outlineDraft !== (payload.selected_outline_md || "") || JSON.stringify(scopeDraft) !== JSON.stringify(payload.scope_contract || {});
  const isCurrentOutline = (style: string) => selectedStyle === style
    && (payload.outline_current !== false || style === "custom");
  const outlineChoiceProps = (style: string) => {
    const busy = chooseOutline.isPending || saveOutline.isPending || recommendOutline.isPending;
    if (style === "custom") return {
      disabled: busy || customEditing,
      onClick: () => setCustomEditing(true),
      "aria-busy": false,
    };
    return {
      disabled: busy || (isCurrentOutline(style) && !customEditing && !outlineDirty),
      onClick: () => {
        if (outlineDirty && !window.confirm(text("当前大纲有未保存的修改。放弃修改并切换大纲？", "This outline has unsaved changes. Discard them and switch outlines?"))) return;
        setOutlineSaveMessage("");
        chooseOutline.mutate(style);
      },
      "aria-busy": chooseOutline.isPending && chooseOutline.variables === style,
    };
  };
  const outlineChoiceText = (style: string, label: string, currentLabel: string) =>
    style === "custom" && customEditing ? text("正在编辑", "Editing")
      : chooseOutline.isPending && chooseOutline.variables === style ? text("正在应用…", "Applying…")
      : isCurrentOutline(style) ? currentLabel : label;
  const selectedStyleLabel = selectedStyle.startsWith("reference:")
    ? String(payload.reference_outline_candidates?.find(candidate => `reference:${candidate.candidate_id}` === selectedStyle)?.source_name || text("参考综述大纲", "Reference review outline"))
    : selectedStyle === "topic-guided" ? text("主题推荐", "Topic recommendation")
      : selectedPreset ? text(selectedPreset.titleZh, selectedPreset.titleEn) : selectedStyle;
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
      ? text("本篇事实已经完成提取，但尚未显示在文献分析中；请使用左侧的“继续未完成分析”。", "This paper was extracted but has not appeared in Paper analysis. Use Resume unfinished analysis on the left.")
      : selectedFactStatus === "pending"
      ? text("生成章节规划时会自动分析当前主题需要的科学事实；原文证据仍是最终依据。", "Chapter planning will automatically analyze the facts needed for the current topic; source passages remain authoritative.")
        : selected?.fact_enrichment?.extraction_status === "failed" || selected?.fact_enrichment?.status === "failed"
          ? text("分析未完成，不代表论文没有证据。请查看上方原因和重试操作。", "Analysis is incomplete, not evidence of absent findings. See the explanation and retry action above.")
          : text("当前没有已核验事实；仍可查看原文，不能据此认定论文未报告相关内容。", "No verified facts are available yet; consult the source rather than treating this as absence of reported findings.");

  return (
    <>
      {(mode === "reading" ? reading.dirty : outline.dirty || scope.dirty) ? <div className="message message-info">
        {text("有尚未保存的编辑，离开页面后可继续。", "Unsaved edits are retained when you leave this page.")}
        <button type="button" className="button button-quiet" disabled={saveReading.isPending || saveOutline.isPending || chooseOutline.isPending} onClick={() => {
          if (mode === "reading") reading.clear(); else { outline.clear(); scope.clear(); }
        }}>{text("撤销未保存编辑", "Discard unsaved edits")}</button>
      </div> : null}
      {reading.storageFailed || outline.storageFailed || scope.storageFailed ? <p className="message message-warning">{text("浏览器未能暂存编辑，请保存后再离开。", "Browser storage is unavailable. Save before leaving.")}</p> : null}
      {mode === "reading" ? (
        <div className="planning-grid">
          <section className="pane planning-list-pane">
            <div className="pane-head matrix-fact-head"><div><span className="step-label">{text("文献分析", "Paper analysis")}</span><h2>{papers.length} {text("篇论文", "papers")}</h2><p>{enrichmentActive ? text(`正在提取科学事实 ${enrichmentJob?.progress_current || 0}/${enrichmentJob?.progress_total || papers.length}`, `Extracting scientific facts ${enrichmentJob?.progress_current || 0}/${enrichmentJob?.progress_total || papers.length}`) : text("查看每篇论文的研究事实、原文依据和分类结果；章节规划会复用这些证据。", "Review each paper's findings, source evidence, and classification; chapter planning reuses this evidence.")}</p><div className="matrix-fact-counts"><span className="complete">{text("已完成", "Completed")} {factCounts.complete}</span><span className="pending">{text("分析中", "Analyzing")} {factCounts.running}</span><span className="pending">{text("待分析或核验", "Pending analysis or verification")} {factCounts.pending}</span><span className="failed">{text("需恢复", "Recovery needed")} {factCounts.failed}</span></div><div className="matrix-header-actions">{bibliographyIssueCount ? <button type="button" className="matrix-bibliography-note" onClick={() => setSelectedId(papers.find((paper) => paper.bibliography_identity?.verified === false)?.paper_id || selectedId)}><strong>{text(`书目待核验 ${bibliographyIssueCount}`, `${bibliographyIssueCount} bibliography records pending`)}</strong><span>{text("点击定位并解决，不阻断内部写作。", "Open the affected paper and resolve it without blocking internal writing.")}</span></button> : null}
              <MatrixAnalysisStatus key={projectId} papers={papers} projectId={projectId} busy={enrichmentActive || Boolean(payload.blueprint_jobs?.some(job => jobIsActive(job.status)))} refresh={refresh} recoveryJobId={enrichmentFailed && hasRecoveryCheckpoint && enrichmentJob?.error_code === "STATE_CONFLICT" && enrichmentJob.available_actions?.includes("retry") ? enrichmentJob.id : undefined} />
            </div></div></div>
            <div className="matrix-list-notices" aria-live="polite">
              {enrichmentActive && enrichmentJob ? <MatrixLiveProgress job={enrichmentJob} papers={papers} /> : null}
              {enrichmentFailed ? <div className="message message-error matrix-enrichment-error"><strong>{enrichmentJob?.error_message === "模型服务响应超时，已完成内容已保留。" ? text("模型服务响应超时，已完成内容已保留。", "The model service timed out. Completed content has been retained.") : enrichmentJob?.error_code === "STATE_CONFLICT" && hasRecoveryCheckpoint ? text("事实已经提取，但尚未显示在文献分析中。", "Facts were extracted but have not appeared in Paper analysis.") : text("上次科学事实分析未完成。", "The previous scientific fact analysis did not finish.")}</strong><p>{enrichmentJob?.error_message === "模型服务响应超时，已完成内容已保留。" ? text("点击“继续未完成分析”恢复。", "Choose Resume unfinished analysis to continue.") : enrichmentJob?.error_code === "STATE_CONFLICT" && hasRecoveryCheckpoint ? text("任务期间论文分析状态发生变化。点击“继续未完成分析”可复用检查点，不会重新调用模型。", "Paper analysis changed during the job. Recover the checkpoint without calling the model again.") : text("生成章节规划时会自动重新分析；该问题不会阻止继续规划。", "Chapter planning will analyze the evidence again automatically; this does not block planning.")}</p>{enrichmentJob?.error_message && enrichmentJob.error_message !== "模型服务响应超时，已完成内容已保留。" ? <details><summary>{text("技术详情", "Technical details")}</summary><p>{enrichmentJob.error_message}</p></details> : null}</div> : null}
            </div>
            <input className="pane-search" type="search" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={text("搜索论文", "Search papers")} />
            <div className="paper-list">{visiblePapers.map((paper) => { const status = analysisState(paper, activeFactPaperIds.has(paper.paper_id)); return <button type="button" key={paper.paper_id} title={text(`内部论文 ID：${paper.paper_id}；事实状态：${factStatusLabel(status, text)}`, `Internal paper ID: ${paper.paper_id}; fact status: ${factStatusLabel(status, text)}`)} className={paper.paper_id === selected?.paper_id ? "paper-row active" : "paper-row"} onClick={() => setSelectedId(paper.paper_id)}><span className="paper-row-main"><strong>{paperLabels.get(paper.paper_id) || paper.paper_id} · {displayText(paper.title)}</strong><small>{paper.authors?.join(", ")}</small></span><span className={`status-pill ${factStatusClass(status)}`}>{factStatusLabel(status, text)}</span></button>; })}</div>
          </section>
          <section className="pane planning-detail-pane">

            {selected?.bibliography_identity?.verified === false ? <><div className="detail-status-notice warning planning-bibliography-notice" role="status"><strong>{text("规范书目信息待核验", "Canonical bibliography pending")}</strong><p>{selected.bibliography_identity.missing_fields?.length ? text(`待补字段：${selected.bibliography_identity.missing_fields.join("、")}。可继续内部写作，但终稿发布前需要解决。`, `Missing fields: ${selected.bibliography_identity.missing_fields.join(", ")}. Internal writing may continue, but this must be resolved before release.`) : text("题名、期刊、年份或 DOI 尚未完成核验。该论文仍可用于文献分析和内部写作，但终稿发布前需要确认。", "The title, venue, year, or DOI is not yet verified. The paper remains usable in Paper analysis and internal writing, but must be confirmed before Final release.")}</p></div><BibliographyResolutionPanel paper={selected} onChanged={refresh} /></> : null}
            {selected ? <><div className="pane-head paper-title"><div><span className="step-label" title={text(`内部论文 ID：${selected.paper_id}`, `Internal paper ID: ${selected.paper_id}`)}>{paperLabels.get(selected.paper_id) || selected.paper_id}</span><h2>{displayText(selected.title)}</h2><p>{[selected.authors?.join(", "), selected.year, selected.journal, selected.doi].filter(Boolean).join(" · ")}</p></div><span className={`status-pill ${factStatusClass(selectedFactStatus)}`}>{factStatusLabel(selectedFactStatus, text)}</span></div><div className="planning-detail-content"><MatrixAnalysisStatus key={selected.paper_id} paper={selected} papers={papers} projectId={projectId} busy={enrichmentActive || Boolean(payload.blueprint_jobs?.some((job) => jobIsActive(job.status)))} refresh={refresh} />{selected.fact_enrichment?.processing ? <p className="message message-info">{text(`已核验 ${selected.fact_enrichment.verified_fact_count || 0} 条 · 待核验 ${selected.fact_enrichment.pending_fact_count || 0} 条`, `${selected.fact_enrichment.verified_fact_count || 0} checked · ${selected.fact_enrichment.pending_fact_count || 0} awaiting verification`)}{selected.fact_enrichment.processing.verification === "pending" ? text("。当前章节规划任务可从已有检查点继续核验，已完成事实会保留。", ". The current chapter-planning task can resume verification from its checkpoint; completed facts are retained.") : null}</p> : null}<section className="reading-field"><h3>{text("摘要", "Abstract")}</h3><p>{displayText(selected.abstract) || text("没有摘要。", "No abstract available.")}</p></section><section className="matrix-fact-section"><MatrixEvidenceUse paper={selected} /><h3>{text("已核验事实依据", "Verified source-grounded facts")}</h3>{formalClassificationTags.length ? <div className="message message-success"><strong>{text("正式证据分类", "Formal evidence classification")}</strong><p>{formalClassificationTags.map((tag) => `${tag.axis_label || tag.axis_id}: ${tag.partition_label || tag.partition_id}`).join(" · ")}</p><small>{text("每个正式分类均绑定事实 ID 与原文证据；阶段 02 初步分组不会直接用于正文 Claim。", "Every formal tag is bound to fact IDs and source evidence. Stage 02 grouping cannot directly support manuscript Claims.")}</small></div> : null}{actionRequiredOutcomes.length ? <div className="message message-warning"><strong>{text("仍需处理的分类问题", "Classification issues still requiring attention")}</strong><p>{actionRequiredOutcomes.map((outcome) => `${outlineAxisLabel(String(outcome.axis_id || ""), text)}: ${outcome.status}${outcome.reason ? ` — ${outcome.reason}` : ""}`).join(" · ")}</p></div> : null}{!formalClassificationTags.length && provisionalClassificationTags.length ? <p className="message message-info">{text("阶段 02 有初步分组，但尚未通过全文事实验证，因此只作为检索提示。", "Stage 02 has preliminary grouping, but it has not passed full-text fact validation and remains a retrieval hint only.")}</p> : null}{selected.topic_partition_classification?.status === "classified" ? <div className="message message-success"><strong>{text(`Topic 分区：${selected.topic_partition_classification.partition}`, `Topic partition: ${selected.topic_partition_classification.partition}`)}</strong><p>{text(`证据约束分类置信度 ${Math.round(Number(selected.topic_partition_classification.confidence || 0) * 100)}%。`, `Evidence-bound classification confidence ${Math.round(Number(selected.topic_partition_classification.confidence || 0) * 100)}%.`)}</p>{selected.topic_partition_classification.support_excerpt ? <details><summary>{text("查看分类原文依据", "View classification evidence")}</summary><blockquote>{selected.topic_partition_classification.support_excerpt}</blockquote><small>{selected.topic_partition_classification.evidence_ceiling}</small></details> : null}</div> : null}{(automaticallyHandledOutcomes.length || unresolvedTopicPartition) ? <details className="advanced-panel matrix-auto-resolution"><summary>{text("系统已自动处理分类边界（无需操作）", "Classification boundaries handled automatically (no action needed)")}</summary><div className="advanced-panel-body"><p>{text("系统已执行定向补证；仍无正面证据的维度不会被强制归类，论文会按已证实的反应、产物或方法事实自动路由，不影响后续写作。", "The system ran a targeted evidence check. Dimensions still lacking positive evidence are not forced; the paper is routed by verified reaction, product, or method facts without blocking writing.")}</p>{automaticallyHandledOutcomes.length ? <ul>{automaticallyHandledOutcomes.map((outcome) => <li key={`${outcome.axis_id}-${outcome.status}`}><strong>{outlineAxisLabel(String(outcome.axis_id || ""), text)}</strong>: {outcome.reason || outcome.status}</li>)}</ul> : null}{unresolvedTopicPartition && selected.topic_partition_classification.boundary_reason ? <p>{selected.topic_partition_classification.boundary_reason}</p> : null}</div></details> : null}{selected?.fact_enrichment?.status === "limited" ? <p className="message message-warning">{text("当前只有摘要级证据，不能据此扩展实验条件、机理或详细定量结论。", "Only abstract-level evidence is available; do not extend it into detailed conditions, mechanisms, or quantitative claims.")}</p> : null}{facts.length ? <div className="matrix-fact-list">{facts.map((fact) => { const ref = fact.evidence_refs?.[0]; const support = fact.support_level || (fact.epistemic_status === "abstract_level_report" ? "abstract_limited" : "direct"); return <article key={fact.fact_id || `${fact.field_id}-${ref?.chunk_id}`}><div><strong>{String(fact.field_id || "fact").replaceAll("_", " ")}</strong><span>{text(`支持：${support === "direct" ? "直接证据" : support === "abstract_limited" ? "仅摘要" : "仅上下文"}`, `Support: ${support.replaceAll("_", " ")}`)} · {ref?.page_start ? text(`第 ${ref.page_start} 页`, `Page ${ref.page_start}`) : fact.source_channel || fact.epistemic_status}</span></div><p>{fact.value}</p><details><summary>{text("查看原文依据与证据上限", "View source support and evidence ceiling")}</summary><blockquote>{fact.support_excerpt}</blockquote><small>{fact.assertion_ceiling || fact.evidence_ceiling}</small></details></article>; })}</div> : <p className="muted">{emptyFactMessage}</p>}</section><details className="advanced-panel matrix-reading-advanced"><summary>{text("全文阅读笔记与图像信息（可选）", "Full-text notes and figure data (optional)")}</summary><div className="advanced-panel-body"><section className="reading-field"><h3>{text("全文阅读笔记", "Full-text reading notes")}</h3><textarea rows={14} value={note} onChange={(event) => setNote(event.target.value)} placeholder={text("转化、条件、证据、范围、限制与综述相关性", "Transformation, conditions, evidence, scope, limitations, and relevance")} /></section><label className="check-label"><input type="checkbox" checked={complete} onChange={(event) => setComplete(event.target.checked)} />{text("已完成该论文全文阅读", "Full-text reading completed")}</label><button className="button button-primary" type="button" disabled={saveReading.isPending} onClick={() => saveReading.mutate()}>{saveReading.isPending ? text("保存中…", "Saving…") : text("保存阅读笔记", "Save reading notes")}</button>{saveReading.error ? <p className="message message-error">{saveReading.error.message}</p> : null}<details className="figure-data"><summary>{text("最相关图像信息", "Most relevant figure")}</summary><pre>{JSON.stringify(selected.most_relevant_figure || {}, null, 2)}</pre></details></div></details></div></> : <div className="empty-state">{text("Discovery确认后会在这里显示Matrix。", "The matrix appears here after Discovery is confirmed.")}</div>}
          </section>
        </div>
      ) : (
        <section className="outline-workspace-react">
          <div className="outline-hero"><div><span className="step-label">{text("步骤 1 · 综述结构", "Step 1 · Review structure")}</span><h2>{text("选择一种大纲", "Choose one outline")}</h2><p>{text("以下是并列的组织方式；选择后都可在同一个大纲编辑器中调整。", "These are alternative ways to organize the review. Edit any selected structure in the same outline editor below.")}</p></div><div className="outline-selection-status"><span className={selectedStyle ? "badge" : "badge pending"}>{selectedStyle ? text(`当前使用：${selectedStyleLabel}`, `Current: ${selectedStyleLabel}`) : text("尚未选择", "Not selected")}</span>{outlineDirty ? <span className="badge pending">{text("修改未保存", "Unsaved changes")}</span> : null}</div></div>
          <div aria-live="polite">
            {outlineSyncFailed ? <p className="message message-warning" role="status">{text("大纲已保存，页面信息同步失败，请刷新查看。", "Outline saved, but page synchronization failed. Refresh to view it.")}</p> : null}
            {chooseOutline.isError ? <div className="message message-error" role="alert">
              <strong>{text("大纲未应用：", "Outline was not applied: ")}</strong><LocalizedError error={chooseOutline.error} />
              {chooseOutline.error instanceof ApiError && chooseOutline.error.status === 409 ? <p>{text("项目状态发生变化，已重新获取当前状态。请重新点击需要的大纲。", "The project state changed and has been fetched again. Select the outline again.")}</p> : null}
            </div> : null}
            {customEditing ? <p className="message message-info" role="status">{text("已进入自定义编辑。当前已保存大纲会继续保留，只有点击“保存大纲”后才会替换。", "Custom editing is open. The saved outline remains active until you select Save outline.")}</p> : null}
            {chooseOutline.isSuccess ? <p className="message message-success" role="status">{text("大纲已应用，章节已载入下方编辑器。", "Outline applied. Sections are loaded in the editor below.")}</p> : null}
          </div>
          <div className="outline-card-grid">
          {topicOutlineCandidate ? <article className={selectedStyle === topicOutlineCandidate.outline_style ? "outline-card topic-outline-recommendation current" : "outline-card topic-outline-recommendation"}>
            <div className="topic-outline-recommendation-copy"><span className="outline-card-icon">★</span><h3>{text("主题推荐", "Topic recommendation")}</h3><p>{text("根据主题和入选论文生成组合结构，选择后可继续调整。", "A combined structure based on your topic and selected papers; editable after selection.")}</p><details className="outline-card-details"><summary>{text("查看推荐依据", "Recommendation details")}</summary><div className="topic-outline-intent-list">
              {topicOutlineIntent?.primary_axis ? <span><strong>{text("主要组织轴", "Primary axis")}</strong>{topicOutlineIntent.primary_axis_label || outlineAxisLabel(topicOutlineIntent.primary_axis, text)}</span> : null}
              {topicOutlineIntent?.secondary_axes?.length ? <span><strong>{text("次级比较轴", "Secondary axes")}</strong>{topicOutlineIntent.secondary_axes.map((axis) => topicOutlineIntent.secondary_axis_labels?.[axis] || outlineAxisLabel(axis, text)).join(" + ")}</span> : null}
              {topicAxisExamples.length ? <span><strong>{text("组织轴示例", "Axis examples")}</strong>{topicAxisExamples.join(" · ")}</span> : null}
              {(topicOutlineIntent?.required_partitions || topicOutlineIntent?.partitions)?.length ? <span><strong>{text("分开讨论", "Separate discussion")}</strong>{(topicOutlineIntent.required_partitions || topicOutlineIntent.partitions || []).join(" / ")}</span> : null}
              {(topicOutlineIntent?.comparison_dimensions || topicOutlineIntent?.named_systems)?.length ? <span><strong>{text("比较示例", "Comparison examples")}</strong>{(topicOutlineIntent.comparison_dimensions || topicOutlineIntent.named_systems || []).join(" / ")}</span> : null}
              {(topicOutlineIntent?.focus_dimensions || topicOutlineIntent?.requested_outcomes)?.length ? <span><strong>{text("重点范围", "Focus dimensions")}</strong>{(topicOutlineIntent.focus_dimensions || topicOutlineIntent.requested_outcomes || []).join(" / ")}</span> : null}
            </div></details></div>
            <button className="button button-primary" type="button" {...outlineChoiceProps(String(topicOutlineCandidate.outline_style || "topic-guided"))}>{outlineChoiceText(String(topicOutlineCandidate.outline_style || "topic-guided"), text("使用推荐大纲", "Use recommended outline"), text("当前推荐大纲", "Current recommended outline"))}</button>
          </article> : <TopicRecommendationPending projectId={projectId} state={payload.topic_recommendation} refresh={refresh} />}
          {outlineStyles.map((style) => <article key={style.id} className={selectedStyle === style.id ? "outline-card current" : "outline-card"}><span className="outline-card-icon">{style.icon}</span><h3>{text(style.titleZh, style.titleEn)}</h3><p>{text(style.descriptionZh, style.descriptionEn)}</p><button className="button button-secondary" type="button" {...outlineChoiceProps(style.id)}>{outlineChoiceText(style.id, text("使用此结构", "Use this structure"), text("当前选择", "Current selection"))}</button></article>)}
          <article className={selectedStyle.startsWith("reference:") ? "outline-card outline-reference-card current" : "outline-card outline-reference-card"}>
            <span className="outline-card-icon">↥</span><h3>{text("上传参考综述", "Upload reference review")}</h3><p>{text("仅学习组织方式；上传后生成候选，不自动替换当前大纲。", "Learn organization only. Uploading creates a candidate without replacing the current outline.")}</p>
            <details className="outline-card-details"><summary>{text("支持的格式与使用说明", "Formats and usage")}</summary><p>{text("支持 PDF、DOCX、Markdown 和 TXT。系统只借鉴层级与节奏，使用当前主题和论文生成新标题，不复制参考综述内容。", "Supports PDF, DOCX, Markdown and TXT. The system borrows hierarchy and pacing, then creates new headings from your topic and papers without copying source content.")}</p></details>
            {uploadReference.error ? <p className="message message-error"><LocalizedError error={uploadReference.error} /></p> : null}
            {payload.legacy_reference_outline_count ? <p className="message message-warning">{text(`已隐藏 ${payload.legacy_reference_outline_count} 个旧版参考大纲，因为它们没有通过“只学格式”的内容隔离校验；如需使用，请重新上传原参考综述。`, `${payload.legacy_reference_outline_count} legacy reference outlines were hidden because they did not pass format-only content isolation. Upload the source review again to use it safely.`)}</p> : null}
            {payload.reference_outline_candidates?.length ? <div className="reference-candidates">{payload.reference_outline_candidates.map((candidate) => { const style = `reference:${candidate.candidate_id}`; return <button key={String(candidate.candidate_id)} className={selectedStyle === style ? "active" : ""} type="button" {...outlineChoiceProps(style)}><strong>{outlineChoiceText(style, String(candidate.source_name || candidate.candidate_id), String(candidate.source_name || candidate.candidate_id))}</strong><small>{text("仅学习组织方式 · 内容来自当前论文", "Organization only · content from current papers")}</small></button>; })}</div> : null}
            <label className="button button-secondary file-button">{uploadReference.isPending ? text("正在分析格式…", "Analyzing format…") : text("选择参考综述", "Choose reference review")}<input type="file" accept=".pdf,.docx,.md,.txt" disabled={uploadReference.isPending} onChange={(event) => { const file = event.target.files?.[0]; event.currentTarget.value = ""; if (file) uploadReference.mutate(file); }} /></label>
          </article></div>
          <details className="surface scope-contract-editor">
            <summary className="scope-contract-heading">
              <div>
                <span className="step-label">{text("写作约束", "Writing contract")}</span>
                <h2>{text("综述范围与学术目标", "Review scope and academic objective")}</h2>
                <p>{text("用于统一后续大纲、章节论证与结论的研究方向。系统会根据主题和论文证据自动生成，无需单独确认；仅在方向不准确时修改，保存大纲时一并保存。", "Keeps the outline, section arguments, and conclusions aligned to one research direction. It is generated from the topic and paper evidence with no separate confirmation; edit only when the direction is inaccurate, then save it with the outline.")}</p>
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
          <section className="outline-editor-card"><div className="section-heading"><div><h2>{text("大纲编辑器", "Outline editor")}</h2><p>{text("调整章节、层级和写作目标；可在模块填写与代码填写之间切换。", "Adjust sections, hierarchy, and writing goals in the module or code editor.")}</p></div><div className="outline-editor-actions"><button className="button button-secondary" type="button" disabled={!outlineReady || recommendOutline.isPending || saveOutline.isPending || chooseOutline.isPending} onClick={() => recommendOutline.mutate(outlineDraft)}>{recommendOutline.isPending ? text("正在分析主题覆盖…", "Analyzing evidence…") : text("为整个大纲推荐论文", "Recommend papers for the whole outline")}</button><button className="button button-primary" type="button" disabled={!outlineReady || saveOutline.isPending || recommendOutline.isPending || chooseOutline.isPending} onClick={() => saveOutline.mutate()}>{saveOutline.isPending ? text("保存中…", "Saving…") : text("保存大纲", "Save outline")}</button></div></div>{outlineSaveMessage ? <p className="message message-info" role="status">{outlineSaveMessage}</p> : null}{recommendationMessage ? <p className="message message-info">{recommendationMessage}</p> : null}<OutlineBuilder value={outlineDraft} papers={papers} onChange={(value) => { setOutlineSaveMessage(""); setOutlineDraft(value); }} />{!outlineReady && outlineDraft.trim() ? <p className="message message-warning">{text("请至少添加一个章节，并补全章节标题。", "Add at least one section and complete every section title.")}</p> : null}{recommendOutline.error ? <p className="message message-error"><LocalizedError error={recommendOutline.error} /></p> : null}{saveOutline.error ? <p className="message message-error"><LocalizedError error={saveOutline.error} /></p> : null}</section>
          <details className="outline-options"><summary>{text("查看系统生成的候选大纲", "View system-generated outline candidates")}</summary><pre>{payload.outline_options_md || text("暂无候选大纲。", "No candidate outlines yet.")}</pre></details>
        </section>
      )}
    </>
  );
}

export function PlanningPage() {
  const { text } = useUiText();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const mode = searchParams.get("view") === "outline" ? "outline" : "reading";
  const { selected: project } = useSelectedProject();
  useEffect(() => {
    if (searchParams.get("tab") === "blueprint") {
      navigate(`/sections?project=${encodeURIComponent(project?.project_id || searchParams.get("project") || "")}`, { replace: true });
    }
  }, [navigate, project?.project_id, searchParams]);
  const planning = useQuery({
    queryKey: ["planning", project?.project_id || ""],
    queryFn: () => apiRequest<PlanningPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning`),
    enabled: Boolean(project) && searchParams.get("tab") !== "blueprint",
    refetchInterval: (query) => (query.state.data?.matrix_enrichment?.jobs || []).some((job) => jobIsActive(job.status)) ? 1500 : false,
  });
  const refresh = () => planning.refetch({ throwOnError: true });
  usePlanningCompletionSync(project?.project_id || "", planning.data?.matrix_enrichment?.jobs || [], planning.refetch);
  const matrixEnrichmentPublishFailed = Boolean(
    planning.data?.matrix_enrichment?.failed_publish_with_pending_rows
  );
  const allMatrixFactsFailed = Boolean(
    planning.data?.matrix_enrichment?.all_failed
  );
  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 3 · 分析与大纲", "Stage 3 · Paper analysis and outline")}</p><h1>{text("文献分析与大纲", "Paper analysis and outline")}</h1><p className="muted">{text("查看论文证据，选择并保存文章大纲。章节规划将在下一阶段生成。", "Review paper evidence and save the outline. Chapter planning follows in the next stage.")}</p></div><ProjectSelector /></div>
      <nav className="workspace-step-tabs"><button type="button" className={mode === "reading" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.delete("tab"); next.set("view", "reading"); setSearchParams(next); }}>{text("文献分析", "Paper analysis")}</button><button type="button" className={mode === "outline" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.delete("tab"); next.set("view", "outline"); setSearchParams(next); }}>{text("选择大纲", "Choose outline")}</button></nav>
      {planning.isPending ? <div className="empty-state">{text("正在加载文献分析与大纲…", "Loading paper analysis and outline…")}</div> : null}
      {planning.error ? <ErrorState error={planning.error} onRetry={() => planning.refetch()} /> : null}
      {planning.data && matrixEnrichmentPublishFailed ? <section className="message message-warning planning-limited-mode"><div><strong>{text("科学事实已经提取，但尚未显示在文献分析中", "Scientific facts were extracted but not published")}</strong><p>{text("请回到“文献分析”点击“继续未完成分析”。系统会复用已完成的检查点，不会重新调用模型。", "Return to Paper analysis and choose Resume unfinished analysis. The completed checkpoint will be reused without another model call.")}</p></div></section> : null}
      {planning.data && allMatrixFactsFailed ? <section className="message message-warning"><strong>{text("科学事实提取尚未成功", "Scientific facts are not yet available")}</strong><p>{text("可以继续生成暂定章节规划，证据缺口留待正文阶段处理；也可以回到文献分析重试提取。", "You can generate a provisional chapter plan and address evidence gaps during drafting, or retry extraction in Paper analysis.")}</p></section> : null}
      {planning.data && project ? <>
        <MatrixWorkspace payload={planning.data} projectId={project.project_id} refresh={refresh} mode={mode} />
        {mode === "outline" && Boolean(planning.data.outline_ready_for_chapter_planning ?? (
          planning.data.outline_current && planning.data.selected_outline_md?.trim() && planning.data.outline_selection?.outline_complete !== false
        ))
          ? <div className="stage-action-bar"><div><strong>{text("大纲已准备好", "Outline ready")}</strong><p>{text("进入下一阶段生成章节规划；当前大纲与论文证据会继续保留。", "Continue to chapter planning. Your outline and paper evidence remain saved.")}</p></div><button className="button button-primary" type="button" onClick={() => navigate(`/sections?project=${encodeURIComponent(project.project_id)}`)}>{text("进入章节写作", "Continue to chapter writing")}</button></div>
          : null}
      </> : null}
    </main>
  );
}
