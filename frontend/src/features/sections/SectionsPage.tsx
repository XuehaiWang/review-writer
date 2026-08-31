import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { MarkdownView } from "../../components/MarkdownView";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels, replacePaperIdsForDisplay } from "../../utils/paperLabels";
import { SectionJobProgress } from "./SectionJobProgress";
import { findSectionJobForDisplay, replaceSectionJobSnapshot } from "./sectionJobResume";
import { sectionReadinessLabel } from "./sectionStatusLabels";

type SectionTask = Record<string, unknown> & {
  section_id?: string;
  heading?: string;
  core_argument?: string;
  section_role?: string;
  primary_papers?: string[];
  supporting_papers?: string[];
  context_papers?: string[];
  allowed_papers?: string[];
  must_cover_points?: string[];
  scientific_claims?: Array<{
    claim_id?: string;
    proposition?: string;
    required_for_section?: boolean;
  }>;
  writing_requirements?: Array<{
    requirement_id?: string;
    type?: string;
    instruction?: string;
  }>;
  avoid_points?: string[];
  figure_need?: unknown;
};

type SectionFile = {
  section_id: string;
  name: string;
  logical_name: string;
  artifact_id: string;
  content: string;
};

type SectionPaper = {
  paper_id: string;
  title?: string;
  authors?: string[];
  keywords?: string[];
};

type EvidenceSection = {
  section_id?: string;
  heading?: string;
  query?: string;
  retrieval_mode?: string;
  status?: string;
  hit_count?: number;
  claim_eligible_hit_count?: number;
  paper_count?: number;
  writeable_primary_papers?: string[];
  context_only_primary_papers?: string[];
  unresolved_primary_papers?: string[];
  corpus_gap_questions?: string[];
  primary_paper_states?: Array<{
    paper_id?: string;
    status?: string;
    diagnostic?: string;
    index_status?: string;
    chunk_count?: number;
  }>;
  query_plans?: Array<{
    question_id?: string;
    status?: string;
    websearch_query?: string;
    diagnostics_by_primary_paper?: Record<string, string>;
  }>;
  hits?: Array<{
    paper_id?: string;
    paper_title?: string;
    evidence_id?: string;
    evidence_key?: string;
    evidence_level?: string;
    chunk_id?: string;
    content?: string;
    page_start?: number | null;
    page_end?: number | null;
    section_path?: string[];
    match_reason?: string;
    match_type?: string;
    claim_eligible?: boolean;
    question_ids?: string[];
    retrieval_passes?: string[];
    is_neighbor?: boolean;
  }>;
};

type DraftParagraph = {
  paragraph_id?: string;
  text?: string;
  evidence?: Array<{ paper_id?: string; chunk_ids?: string[]; claim?: string }>;
  claim_realizations?: Array<{ claim_id?: string; text?: string; citation_group?: string[] }>;
};

type SynthesisComponent = {
  component_id?: string;
  component_type?: string;
  necessity?: string;
  purpose?: string;
  status?: string;
  summary?: string;
  evidence_keys?: string[];
};

type WritingParagraph = {
  paragraph_id?: string;
  theme?: string;
  argument_role?: string;
  objective?: string;
  reader_takeaway?: string;
  positive_synthesis?: string;
  claim_ids?: string[];
};

type WritingClaim = {
  claim_id?: string;
  paragraph_id?: string;
  claim?: string;
  claim_kind?: string;
  epistemic_status?: string;
  support_status?: string;
  citation_group?: string[];
  evidence_refs?: Array<{ evidence_id?: string; evidence_key?: string }>;
  evidence_ceiling?: string;
};

type NarrativeDiagnostics = {
  status?: "complete" | "shallow" | string;
  paragraph_count?: number;
  target_paragraph_count?: number;
  comparison_paragraph_count?: number;
  minimum_comparison_paragraphs?: number;
  missing_requirements?: string[];
};

type DraftReview = {
  decision?: string;
  issues?: Array<{ type?: string; severity?: string; reason?: string }>;
  repair_objective?: string;
};

type SectionReadiness = {
  status?: string;
  missing_required_claim_ids?: string[];
  structure_gaps?: string[];
  depth_sufficient?: boolean;
};

type SectionDraftState = {
  section_id?: string;
  generation_mode?: string;
  section_readiness?: SectionReadiness;
  depth_diagnostics?: {
    actual_word_count?: number;
    minimum_word_count?: number;
    sufficient?: boolean;
  };
  paragraphs?: DraftParagraph[];
  validations?: Array<{ rule_id?: string; status?: string; target_id?: string }>;
  reviews?: DraftReview[];
};

type SectionsPayload = {
  project_id: string;
  section_tasks: SectionTask[];
  section_files: SectionFile[];
  section_drafts_md: string;
  section_drafting_report_md: string;
  papers: SectionPaper[];
  paper_display_labels?: Record<string, string>;
  revision: number;
  handoff: { drafts_stale: boolean; has_existing_drafts: boolean; current: boolean };
  report: { current_task_count: number; current_output_count: number; jobs: Job[] };
  evidence_package?: {
    sections?: EvidenceSection[];
  } | null;
  synthesis_state?: {
    planning_mode?: string;
    sections?: Array<{ section_id?: string; summary?: string; components?: SynthesisComponent[] }>;
  } | null;
  writing_plan?: {
    planning_mode?: string;
    sections?: Array<{ section_id?: string; route?: string; overview_intent?: string; paragraphs?: WritingParagraph[]; claims?: WritingClaim[]; narrative_diagnostics?: NarrativeDiagnostics }>;
  } | null;
  section_drafts?: {
    sections?: SectionDraftState[];
  } | null;
};

type WorkspaceTab = "synthesis" | "writing" | "evidence" | "section" | "merged" | "tasks" | "review" | "report";

function wordCount(value?: string) {
  return String(value || "").trim().split(/\s+/).filter(Boolean).length;
}

function taskId(task?: SectionTask) {
  return String(task?.section_id || task?.heading || "");
}

function taskPaperSummary(task: SectionTask | undefined, text: (zh: string, en: string) => string) {
  const primaryCount = task?.primary_papers?.length || 0;
  const supportingCount = task?.supporting_papers?.length || 0;
  const contextCount = task?.context_papers?.length || 0;
  const availableCount = task?.allowed_papers?.length || 0;
  const role = String(task?.section_role || "body");
  const parts = [text(`${primaryCount} 篇主要论文`, `${primaryCount} primary papers`)];
  if (role === "introduction" || role === "conclusion") {
    parts.push(text("综合章节", "synthesis section"));
  }
  if (supportingCount) parts.push(text(`${supportingCount} 篇支持论文`, `${supportingCount} supporting papers`));
  if (contextCount) parts.push(text(`${contextCount} 篇背景论文`, `${contextCount} context papers`));
  if (!supportingCount && !contextCount && availableCount !== primaryCount) {
    parts.push(text(`${availableCount} 篇可用证据`, `${availableCount} available papers`));
  }
  return parts.join(" · ");
}

function TaskRequirements({ task, paperLabels }: { task?: SectionTask; paperLabels: Map<string, string> }) {
  const { text } = useUiText();
  if (!task) return <div className="empty-state">{text("当前Blueprint没有可用的章节写作任务。", "The current blueprint has no section-writing tasks.")}</div>;
  const figures = Array.isArray(task.figure_need) ? task.figure_need : task.figure_need ? [task.figure_need] : [];
  const scientificClaims = task.scientific_claims?.length
    ? task.scientific_claims.map((item) => item.proposition || item.claim_id || "").filter(Boolean)
    : task.must_cover_points || [];
  const writingRequirements = (task.writing_requirements || [])
    .map((item) => item.instruction || item.type || item.requirement_id || "")
    .filter(Boolean);
  return (
    <article className="task-sheet-react">
      <header><span className="step-label">{task.section_id || text("章节", "Section")}</span><h2>{task.heading || task.section_id}</h2><p>{task.core_argument || text("未指定核心论点。", "No core argument specified.")}</p></header>
      <div className="task-requirement-grid">
        <section><h3>{text("分配论文", "Assigned papers")}</h3><div className="chip-list">{task.allowed_papers?.length ? task.allowed_papers.map((paper) => <span key={paper} title={text(`内部论文 ID：${paper}`, `Internal paper ID: ${paper}`)}>{paperLabels.get(paper) || paper}</span>) : <em>{text("尚未分配", "Not assigned")}</em>}</div></section>
        <section><h3>{text("图像要求", "Figure requirements")}</h3>{figures.length ? figures.map((figure, index) => <pre key={index}>{typeof figure === "string" ? figure : JSON.stringify(figure, null, 2)}</pre>) : <p>{text("未指定图像要求。", "No figure requirements specified.")}</p>}</section>
        <section className="wide"><h3>{text("科学命题", "Scientific claims")}</h3>{scientificClaims.length ? <ol>{scientificClaims.map((item) => <li key={item}>{item}</li>)}</ol> : <p>{text("当前没有预设的必证命题；系统将从证据中形成受限论点。", "No required proposition is predeclared; bounded claims will be formed from the evidence.")}</p>}</section>
        <section className="wide"><h3>{text("写作要求", "Writing requirements")}</h3>{writingRequirements.length ? <ol>{writingRequirements.map((item) => <li key={item}>{item}</li>)}</ol> : <p>{text("使用本阶段的通用综合规则。", "Use the stage's general synthesis rules.")}</p>}</section>
        <section className="wide"><h3>{text("写作边界", "Writing boundaries")}</h3>{task.avoid_points?.length ? <ul>{task.avoid_points.map((item) => <li key={item}>{item}</li>)}</ul> : <p>{text("未指定。", "Not specified.")}</p>}</section>
      </div>
    </article>
  );
}

function EvidenceView({ section, paragraphs, paperLabels }: { section?: EvidenceSection; paragraphs?: DraftParagraph[]; paperLabels: Map<string, string> }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节没有已发布的证据包。", "This section has no published evidence package.")}</div>;
  const hits = section.hits || [];
  const retrievalLabel = section.retrieval_mode === "lexical"
    ? text("问题级全文证据", "Question-level full-text evidence")
    : section.retrieval_mode === "abstract_only"
      ? text("仅摘要证据", "Abstract-only evidence")
      : section.retrieval_mode === "insufficient_evidence"
        ? text("暂无可写证据", "No writeable evidence")
        : text("兼容回退", "Compatibility fallback");
  const matchLabel = (value?: string) => ({
    direct_match: text("直接命中", "Direct match"),
    fact_card_evidence: text("Matrix 事实证据", "Matrix fact evidence"),
    table_or_figure: text("表格/图像证据", "Table/figure evidence"),
    neighbor_context: text("相邻上下文", "Adjacent context"),
    abstract_only: text("仅摘要", "Abstract only"),
    coverage_only: text("仅覆盖，不可支持论点", "Coverage only; not claim evidence"),
  }[String(value || "")] || value || text("未分类", "Unclassified"));
  return (
    <div className="section-evidence-view">
      <header><span className="step-label">{text("检索证据", "Retrieved evidence")}</span><h2>{section.heading || section.section_id}</h2><p>{section.query}</p><div className="chip-list"><span>{retrievalLabel}</span><span>{text(`${section.claim_eligible_hit_count || 0} 个可引用段落`, `${section.claim_eligible_hit_count || 0} claim-ready passages`)}</span><span>{text(`${section.paper_count || 0} 篇可写论文`, `${section.paper_count || 0} writeable papers`)}</span></div></header>
      {section.primary_paper_states?.length ? <section className="evidence-diagnostic-summary"><h3>{text("主论文写作状态", "Primary-paper writing status")}</h3><div className="chip-list">{section.primary_paper_states.map((item) => <span key={item.paper_id} className={`status-pill ${item.status === "writeable" ? "ok" : "warning"}`} title={`${item.diagnostic || ""} · ${item.index_status || ""} · ${item.chunk_count || 0} chunks`}>{paperLabels.get(String(item.paper_id || "")) || item.paper_id} · {item.status}</span>)}</div>{section.corpus_gap_questions?.length ? <p>{text(`全章缺少：${section.corpus_gap_questions.join("、")}`, `Corpus gaps: ${section.corpus_gap_questions.join(", ")}`)}</p> : null}</section> : null}
      {section.query_plans?.length ? <details className="evidence-query-plans"><summary>{text(`查看 ${section.query_plans.length} 个问题级查询与诊断`, `View ${section.query_plans.length} question-level queries and diagnostics`)}</summary>{section.query_plans.map((plan) => <article key={plan.question_id}><strong>{plan.question_id} · {plan.status}</strong><code>{plan.websearch_query}</code></article>)}</details> : null}
      {hits.length ? <div className="section-evidence-list">{hits.map((hit, index) => {
        const page = hit.page_start ? (hit.page_end && hit.page_end !== hit.page_start ? `${hit.page_start}–${hit.page_end}` : String(hit.page_start)) : text("未知", "Unknown");
        const supported = (paragraphs || []).filter((paragraph) => paragraph.evidence?.some((item) => item.chunk_ids?.includes(String(hit.chunk_id || ""))));
        return <article key={hit.evidence_key || hit.chunk_id || index} className={hit.claim_eligible === false ? "context-only-evidence" : undefined}><div><strong>{paperLabels.get(String(hit.paper_id || "")) || hit.paper_title || hit.paper_id}</strong><span>{text("页", "Page")} {page} · {(hit.section_path || []).join(" › ") || text("未标注章节", "Unlabelled section")}</span></div><p>{hit.content}</p><footer><code>{hit.evidence_id || hit.chunk_id}</code><em>{matchLabel(hit.match_type)} · {hit.evidence_level || hit.match_reason}</em></footer>{supported.length ? <details><summary>{text(`支持 ${supported.length} 个正文段落`, `Supports ${supported.length} draft paragraphs`)}</summary>{supported.map((paragraph) => <p key={paragraph.paragraph_id} className="supported-paragraph"><b>{paragraph.paragraph_id}</b> {paragraph.text}</p>)}</details> : null}</article>;
      })}</div> : <div className="empty-state">{section.retrieval_mode === "insufficient_evidence" ? text("当前章节没有可支持正文论点的证据。请按诊断重建索引、调整分类或补充论文。", "This section has no evidence that can support draft claims. Follow the diagnostics to rebuild indexes, adjust classification, or add papers.") : text("当前没有可显示的全文证据。", "There is no full-text evidence to display.")}</div>}
    </div>
  );
}

function SynthesisView({ section, mode }: { section?: { summary?: string; components?: SynthesisComponent[] }; mode?: string }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节尚无综合状态。", "This section has no synthesis state yet.")}</div>;
  return <div className="academic-state-view"><header><span className="step-label">{text("自动综合", "Automatic synthesis")}</span><h2>{text("章节知识综合", "Section knowledge synthesis")}</h2><p>{section.summary || text("综合组件已按当前证据准备。", "Synthesis components were prepared from current evidence.")}</p><small>{mode}</small></header><div className="academic-card-list">{(section.components || []).map((component) => <article key={component.component_id}><div><strong>{component.component_type}</strong><span className={`status-pill ${component.status === "supported" ? "ok" : "warning"}`}>{component.status}</span></div><p>{component.summary || component.purpose}</p><footer><span>{component.necessity}</span><span>{text(`${component.evidence_keys?.length || 0} 个证据键`, `${component.evidence_keys?.length || 0} evidence keys`)}</span></footer></article>)}</div></div>;
}

function WritingPlanView({ section, paperLabels }: { section?: { route?: string; overview_intent?: string; paragraphs?: WritingParagraph[]; claims?: WritingClaim[]; narrative_diagnostics?: NarrativeDiagnostics }; paperLabels: Map<string, string> }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节尚无写作计划。", "This section has no writing plan yet.")}</div>;
  const claims = new Map((section.claims || []).map((claim) => [claim.claim_id, claim]));
  const narrative = section.narrative_diagnostics;
  return <div className="academic-state-view"><header><span className="step-label">{text(`路线 ${section.route || "—"}`, `Route ${section.route || "—"}`)}</span><h2>{text("段落与 Claim/Citation 计划", "Paragraph and Claim/Citation plan")}</h2><p>{section.overview_intent}</p>{narrative ? <div className="chip-list"><span className={`status-pill ${narrative.status === "complete" ? "ok" : "warning"}`}>{narrative.status === "complete" ? text("叙事结构完整", "Narrative structure complete") : text("叙事结构偏浅", "Narrative structure shallow")}</span><span>{text(`${narrative.paragraph_count || 0}/${narrative.target_paragraph_count || 0} 段`, `${narrative.paragraph_count || 0}/${narrative.target_paragraph_count || 0} paragraphs`)}</span><span>{text(`${narrative.comparison_paragraph_count || 0}/${narrative.minimum_comparison_paragraphs || 0} 个比较段`, `${narrative.comparison_paragraph_count || 0}/${narrative.minimum_comparison_paragraphs || 0} comparison paragraphs`)}</span></div> : null}</header><div className="academic-card-list">{(section.paragraphs || []).map((paragraph) => <article key={paragraph.paragraph_id}><div><strong>{paragraph.paragraph_id} · {paragraph.argument_role}</strong></div><h3>{paragraph.theme}</h3><p>{paragraph.objective}</p><blockquote>{paragraph.reader_takeaway}</blockquote>{(paragraph.claim_ids || []).map((claimId) => { const claim = claims.get(claimId); return <details key={claimId}><summary>{claimId} · {claim?.claim_kind}</summary><p>{claim?.claim}</p><div className="chip-list">{(claim?.citation_group || []).map((paperId) => <span key={paperId}>{paperLabels.get(paperId) || paperId}</span>)}<span>{claim?.epistemic_status}</span><span>{claim?.support_status}</span></div><small>{claim?.evidence_ceiling}</small></details>; })}</article>)}</div></div>;
}

function ReviewView({ section }: { section?: SectionDraftState }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节尚无审校状态。", "This section has no review state yet.")}</div>;
  const reviews = section.reviews || [];
  const readiness = String(section.section_readiness?.status || "");
  const depth = section.depth_diagnostics;
  return <div className="academic-state-view"><header><span className="step-label">{text("自动审校", "Automatic review")}</span><h2>{reviews[0]?.decision || text("待审校", "Pending")}</h2><div className="chip-list">{readiness ? <span className={`status-pill ${readiness === "scientific_complete" ? "ok" : "warning"}`}>{sectionReadinessLabel(readiness, text)}</span> : null}<span>{section.generation_mode || text("标准生成", "Standard")}</span>{depth ? <span>{text(`${depth.actual_word_count || 0}/${depth.minimum_word_count || 0} 词`, `${depth.actual_word_count || 0}/${depth.minimum_word_count || 0} words`)}</span> : null}</div><p>{reviews[0]?.repair_objective || text("Claim、引用与段落身份校验已完成。", "Claim, citation, and paragraph identity validation is complete.")}</p></header><div className="academic-card-list">{(section.validations || []).map((validation, index) => <article key={`${validation.rule_id}-${index}`}><div><strong>{validation.rule_id}</strong><span className={`status-pill ${validation.status === "pass" ? "ok" : "warning"}`}>{validation.status}</span></div><p>{validation.target_id}</p></article>)}{reviews.flatMap((review) => review.issues || []).map((issue, index) => <article key={`${issue.type}-${index}`}><div><strong>{issue.type}</strong><span className="status-pill warning">{issue.severity}</span></div><p>{issue.reason}</p></article>)}</div></div>;
}

export function SectionsPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { selected: project } = useSelectedProject();
  const [selectedId, setSelectedId] = useState("");
  const [tab, setTab] = useState<WorkspaceTab>("section");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [jobId, setJobId] = useState("");
  const sections = useQuery({
    queryKey: ["sections", project?.project_id || ""],
    queryFn: () => apiRequest<SectionsPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/sections`),
    enabled: Boolean(project),
  });
  const payload = sections.data;
  const taskOnly = Boolean(payload?.handoff.drafts_stale || !payload?.section_files.length);
  const tasks = payload?.section_tasks || [];
  const files = payload?.section_files || [];
  const reportJob = findSectionJobForDisplay(
    payload?.report.jobs || [],
    tasks,
    Boolean(payload?.handoff.current),
  );
  const polledJob = useJob(jobId || reportJob?.id || "");
  const currentJob = payload?.handoff.current ? reportJob : polledJob.data || reportJob;
  const currentJobActive = Boolean(currentJob && jobIsActive(currentJob.status));
  const reportJobs = useMemo(
    () => replaceSectionJobSnapshot(payload?.report.jobs || [], currentJob),
    [currentJob, payload?.report.jobs],
  );
  const canResumeCurrentJob = Boolean(
    currentJob
    && (
      (currentJob.available_actions || []).includes("retry")
      || ["failed", "cancelled", "interrupted"].includes(currentJob.status)
    )
    && currentJob.result.section_checkpoint,
  );
  const liveOutputCount = currentJob && jobIsActive(currentJob.status)
    ? currentJob.progress_current
    : payload?.report.current_output_count || 0;
  const liveTaskCount = currentJob?.progress_total || payload?.report.current_task_count || 0;
  const paperLabels = useMemo(() => {
    const supplied = new Map(Object.entries(payload?.paper_display_labels || {}));
    return supplied.size ? supplied : buildPaperDisplayLabels(payload?.papers || []);
  }, [payload?.paper_display_labels, payload?.papers]);

  useEffect(() => {
    if (!payload) return;
    const requestedSection = String(searchParams.get("section") || "");
    if (requestedSection) {
      const requestedFile = files.find(
        (file) => file.section_id === requestedSection || file.name === requestedSection
      );
      const requestedTask = tasks.find((task) => taskId(task) === requestedSection);
      if (requestedFile || requestedTask) {
        setSelectedId(taskOnly ? taskId(requestedTask) : requestedFile?.name || requestedSection);
        setTab("evidence");
        setShowAdvanced(true);
        return;
      }
    }
    const valid = taskOnly
      ? tasks.some((task) => taskId(task) === selectedId)
      : files.some((file) => file.name === selectedId);
    if (!valid) setSelectedId(taskOnly ? taskId(tasks[0]) : files[0]?.name || "");
    if (taskOnly && tab !== "tasks" && tab !== "report") setTab("tasks");
  }, [files, payload, searchParams, selectedId, tab, taskOnly, tasks]);

  useEffect(() => {
    if (!currentJob || jobIsActive(currentJob.status)) return;
    if (currentJob.status === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["sections", project?.project_id || ""] });
    }
  }, [currentJob, project?.project_id, queryClient]);

  const activeTask = useMemo(() => {
    const file = files.find((item) => item.name === selectedId);
    const id = taskOnly ? selectedId : file?.section_id || selectedId.replace(/\.md$/i, "");
    return tasks.find((task) => taskId(task) === id || task.heading === id) || tasks[0];
  }, [files, selectedId, taskOnly, tasks]);
  const activeFile = files.find((file) => file.name === selectedId) || files[0];
  const activeEvidence = payload?.evidence_package?.sections?.find((item) => item.section_id === taskId(activeTask));
  const activeSynthesis = payload?.synthesis_state?.sections?.find((item) => item.section_id === taskId(activeTask));
  const activeWritingPlan = payload?.writing_plan?.sections?.find((item) => item.section_id === taskId(activeTask));
  const activeDraftState = payload?.section_drafts?.sections?.find((item) => item.section_id === taskId(activeTask));
  const activeDraftParagraphs = activeDraftState?.paragraphs;
  const displayedActiveContent = replacePaperIdsForDisplay(activeFile?.content, paperLabels);
  const displayedMergedContent = replacePaperIdsForDisplay(payload?.section_drafts_md, paperLabels);

  const generate = useMutation({
    mutationFn: () => canResumeCurrentJob
      ? apiRequest<Job>(`/api/v1/jobs/${encodeURIComponent(currentJob!.id)}/retry`, {
        method: "POST",
      })
      : apiRequest<Job>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/sections/jobs`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        ...jsonBody({}),
      }),
    onSuccess: (job) => {
      setJobId(job.id);
      setTab("report");
      setShowAdvanced(true);
    },
  });
  const confirm = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/sections/confirm`, {
      method: "POST",
      ...jsonBody({ revision: payload!.revision }),
    }),
    onSuccess: () => navigate(`/images?tab=review&project=${encodeURIComponent(project!.project_id)}`),
  });

  const mainTabs: Array<[WorkspaceTab, string]> = taskOnly
    ? [["tasks", text("写作准备", "Writing preparation")]]
    : [["section", text("章节草稿", "Section draft")], ["merged", text("合并预览", "Merged preview")]];
  const advancedTabs: Array<[WorkspaceTab, string]> = taskOnly
    ? [["report", text("生成报告", "Generation report")]]
    : [["synthesis", text("综合", "Synthesis")], ["writing", text("写作计划", "Writing Plan")], ["evidence", text("证据", "Evidence")], ["review", text("审校", "Review")], ["tasks", text("写作要求", "Writing requirements")], ["report", text("生成报告", "Generation report")]];
  const error = generate.error || confirm.error || (currentJob?.status === "failed" ? new Error(currentJob.error_message || text("章节生成失败。", "Section generation failed.")) : null);

  return (
    <main className="workspace page-container workspace-page sections-page">
      <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 4 · 章节撰写", "Stage 4 · Section drafting")}</p><h1>{text("章节撰写", "Section drafting")}</h1><p className="muted">{text("使用当前Blueprint任务生成章节，并在进入图像阶段前人工审核。", "Generate sections from the current blueprint tasks and review them before entering the figure stage.")}</p></div><ProjectSelector /></div>
      {sections.isPending ? <div className="empty-state">{text("正在加载章节产物…", "Loading section artifacts…")}</div> : null}
      {sections.error ? <ErrorState error={sections.error} onRetry={() => sections.refetch()} /> : null}
      {payload ? <>
        {payload.handoff.drafts_stale ? <p className="message message-warning">{text("Blueprint已更新，旧章节草稿保留在磁盘但不会作为当前流程内容显示。请重新生成。", "The blueprint changed. Old section drafts remain on disk but are not current workflow content. Regenerate them.")}</p> : null}
        {currentJob && currentJobActive && tab !== "report" ? <div className="section-live-progress-banner"><SectionJobProgress job={currentJob} /></div> : null}
        <div className="sections-grid-react">
          <section className="pane section-list-react"><div className="pane-head"><div><span className="step-label">{text("章节", "Sections")}</span><h2>{tasks.length} {text("个章节", "sections")}</h2></div></div><div className="paper-list">{(taskOnly ? tasks : files).map((item) => {
            const id = taskOnly ? taskId(item as SectionTask) : (item as SectionFile).name;
            const task = taskOnly ? item as SectionTask : tasks.find((candidate) => taskId(candidate) === (item as SectionFile).section_id);
            const file = taskOnly ? undefined : item as SectionFile;
            return <button key={id} type="button" className={id === selectedId ? "paper-row active" : "paper-row"} onClick={() => { setSelectedId(id); if (!taskOnly) { setTab("section"); setShowAdvanced(false); } }}><span className="paper-row-main"><strong>{task?.heading || task?.section_id || file?.name}</strong><small>{file ? `${wordCount(file.content)} words` : taskPaperSummary(task, text)}</small></span><span className={file ? "status-dot ok" : "status-dot warning"} /></button>;
          })}</div></section>
          <section className="pane section-preview-react"><nav className="detail-tabs">{mainTabs.map(([value, label]) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => { setTab(value); setShowAdvanced(false); }}>{label}</button>)}</nav><details className="advanced-panel section-advanced-tabs" open={showAdvanced} onToggle={(event) => setShowAdvanced(event.currentTarget.open)}><summary>{text("生成依据与检查详情", "Generation inputs and checks")}</summary><div className="advanced-panel-body advanced-tab-list">{advancedTabs.map(([value, label]) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => { setTab(value); setShowAdvanced(true); }}>{label}</button>)}</div></details><div className="section-preview-content">
            {tab === "synthesis" ? <SynthesisView section={activeSynthesis} mode={payload.synthesis_state?.planning_mode} /> : null}
            {tab === "writing" ? <WritingPlanView section={activeWritingPlan} paperLabels={paperLabels} /> : null}
            {tab === "section" ? <MarkdownView content={displayedActiveContent} empty={text("当前章节尚未生成。", "This section has not been generated.")} /> : null}
            {tab === "merged" ? <MarkdownView content={displayedMergedContent} empty={text("当前没有合并预览。", "No merged preview is available.")} /> : null}
            {tab === "evidence" ? <EvidenceView section={activeEvidence} paragraphs={activeDraftParagraphs} paperLabels={paperLabels} /> : null}
            {tab === "review" ? <ReviewView section={activeDraftState} /> : null}
            {tab === "tasks" ? <TaskRequirements task={activeTask} paperLabels={paperLabels} /> : null}
            {tab === "report" ? <div className="job-report"><h2>{text("章节生成报告", "Section generation report")}</h2><p>{liveOutputCount}/{liveTaskCount} {currentJobActive ? text("章已实时完成", "sections completed live") : text("个当前章节产物", "current section artifacts")}</p>{currentJob ? <SectionJobProgress job={currentJob} /> : <div className="empty-state">{text("尚未启动章节生成。", "Section generation has not started.")}</div>}{reportJobs.map((job) => <details key={job.id}><summary>{job.status} · {job.id}</summary><p>{job.progress_current}/{job.progress_total} · {job.error_message || text("无错误", "No errors")}</p></details>)}</div> : null}
          </div></section>
          <aside className="pane section-gate-react"><div className="pane-head"><div><span className="step-label">{text("审核门", "Review gate")}</span><h2>{text("人工审核", "Human review")}</h2></div></div><div className="gate-body"><p>{payload.handoff.current ? text("当前草稿已生成，可审核后进入图像阶段。", "Current drafts are ready for review before the figure stage.") : currentJob && jobIsActive(currentJob.status) ? text("章节正在生成中。", "Sections are being generated.") : text("请从当前写作要求生成章节草稿。", "Generate section drafts from the current writing requirements.")}</p><ul><li>{text("每节是完整综述段落，不是提纲。", "Each section contains complete review prose, not outline fragments.")}</li><li>{text("引用来自该节允许论文。", "Citations come from papers allowed for that section.")}</li><li>{text("保留证据边界与不确定性。", "Evidence boundaries and uncertainty are preserved.")}</li><li>{text("图像需求与段落论证一致。", "Figure needs align with paragraph arguments.")}</li></ul></div></aside>
        </div>
        <div className="stage-action-bar"><div><strong>{payload.handoff.current ? text("确认章节", "Confirm sections") : canResumeCurrentJob ? text("继续未完成章节", "Resume unfinished sections") : text("生成所有章节", "Generate all sections")}</strong><p>{payload.handoff.current ? text("确认当前版本后进入图像处理。", "Confirm the current version to enter figure processing.") : currentJobActive ? text(`生成中 ${currentJob!.progress_current}/${currentJob!.progress_total}`, `Generating ${currentJob!.progress_current}/${currentJob!.progress_total}`) : canResumeCurrentJob ? text(`已保留 ${currentJob!.progress_current}/${currentJob!.progress_total} 个章节，重试时仅继续未完成章节。`, `${currentJob!.progress_current}/${currentJob!.progress_total} sections are checkpointed; retry resumes only unfinished sections.`) : text("根据当前Blueprint写作要求生成全部章节。", "Generate every section from the current blueprint requirements.")}</p></div>{payload.handoff.current ? <button className="button button-primary" type="button" disabled={confirm.isPending} onClick={() => confirm.mutate()}>{confirm.isPending ? text("确认中…", "Confirming…") : text("确认并进入图像处理", "Confirm and enter figure processing")}</button> : <button className="button button-primary" type="button" disabled={generate.isPending || currentJobActive} onClick={() => generate.mutate()}>{currentJobActive ? text("正在生成…", "Generating…") : generate.isPending ? text("正在提交…", "Submitting…") : canResumeCurrentJob ? text("继续生成", "Resume generation") : text("生成所有章节草稿", "Generate all section drafts")}</button>}{error ? <span className="message message-error">{error.message}</span> : null}</div>
      </> : null}
    </main>
  );
}
