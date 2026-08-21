import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { MarkdownView } from "../../components/MarkdownView";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels, replacePaperIdsForDisplay } from "../../utils/paperLabels";
import { SectionJobProgress } from "./SectionJobProgress";
import { findResumableSectionJob } from "./sectionJobResume";

type SectionTask = Record<string, unknown> & {
  section_id?: string;
  heading?: string;
  core_argument?: string;
  allowed_papers?: string[];
  must_cover_points?: string[];
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
  paper_count?: number;
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

type DraftReview = {
  decision?: string;
  issues?: Array<{ type?: string; severity?: string; reason?: string }>;
  repair_objective?: string;
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
    sections?: Array<{ section_id?: string; route?: string; overview_intent?: string; paragraphs?: WritingParagraph[]; claims?: WritingClaim[] }>;
  } | null;
  section_drafts?: {
    sections?: Array<{ section_id?: string; paragraphs?: DraftParagraph[]; validations?: Array<{ rule_id?: string; status?: string; target_id?: string }>; reviews?: DraftReview[] }>;
  } | null;
};

type WorkspaceTab = "synthesis" | "writing" | "evidence" | "section" | "merged" | "tasks" | "review" | "report";

function wordCount(value?: string) {
  return String(value || "").trim().split(/\s+/).filter(Boolean).length;
}

function taskId(task?: SectionTask) {
  return String(task?.section_id || task?.heading || "");
}

function TaskRequirements({ task, paperLabels }: { task?: SectionTask; paperLabels: Map<string, string> }) {
  const { text } = useUiText();
  if (!task) return <div className="empty-state">{text("当前Blueprint没有可用的章节写作任务。", "The current blueprint has no section-writing tasks.")}</div>;
  const figures = Array.isArray(task.figure_need) ? task.figure_need : task.figure_need ? [task.figure_need] : [];
  return (
    <article className="task-sheet-react">
      <header><span className="step-label">{task.section_id || text("章节", "Section")}</span><h2>{task.heading || task.section_id}</h2><p>{task.core_argument || text("未指定核心论点。", "No core argument specified.")}</p></header>
      <div className="task-requirement-grid">
        <section><h3>{text("分配论文", "Assigned papers")}</h3><div className="chip-list">{task.allowed_papers?.length ? task.allowed_papers.map((paper) => <span key={paper} title={text(`内部论文 ID：${paper}`, `Internal paper ID: ${paper}`)}>{paperLabels.get(paper) || paper}</span>) : <em>{text("尚未分配", "Not assigned")}</em>}</div></section>
        <section><h3>{text("图像要求", "Figure requirements")}</h3>{figures.length ? figures.map((figure, index) => <pre key={index}>{typeof figure === "string" ? figure : JSON.stringify(figure, null, 2)}</pre>) : <p>{text("未指定图像要求。", "No figure requirements specified.")}</p>}</section>
        <section className="wide"><h3>{text("必须覆盖", "Must cover")}</h3>{task.must_cover_points?.length ? <ol>{task.must_cover_points.map((item) => <li key={item}>{item}</li>)}</ol> : <p>{text("未指定。", "Not specified.")}</p>}</section>
        <section className="wide"><h3>{text("写作边界", "Writing boundaries")}</h3>{task.avoid_points?.length ? <ul>{task.avoid_points.map((item) => <li key={item}>{item}</li>)}</ul> : <p>{text("未指定。", "Not specified.")}</p>}</section>
      </div>
    </article>
  );
}

function EvidenceView({ section, paragraphs, paperLabels }: { section?: EvidenceSection; paragraphs?: DraftParagraph[]; paperLabels: Map<string, string> }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节没有已发布的证据包。", "This section has no published evidence package.")}</div>;
  const hits = section.hits || [];
  return (
    <div className="section-evidence-view">
      <header><span className="step-label">{text("检索证据", "Retrieved evidence")}</span><h2>{section.heading || section.section_id}</h2><p>{section.query}</p><div className="chip-list"><span>{section.retrieval_mode === "lexical" ? text("词法全文检索", "Lexical full-text retrieval") : text("旧版前缀回退", "Legacy prefix fallback")}</span><span>{text(`${section.hit_count || 0} 个段落`, `${section.hit_count || 0} passages`)}</span><span>{text(`${section.paper_count || 0} 篇论文`, `${section.paper_count || 0} papers`)}</span></div></header>
      {hits.length ? <div className="section-evidence-list">{hits.map((hit, index) => {
        const page = hit.page_start ? (hit.page_end && hit.page_end !== hit.page_start ? `${hit.page_start}–${hit.page_end}` : String(hit.page_start)) : text("未知", "Unknown");
        const supported = (paragraphs || []).filter((paragraph) => paragraph.evidence?.some((item) => item.chunk_ids?.includes(String(hit.chunk_id || ""))));
        return <article key={hit.evidence_key || hit.chunk_id || index}><div><strong>{paperLabels.get(String(hit.paper_id || "")) || hit.paper_title || hit.paper_id}</strong><span>{text("页", "Page")} {page} · {(hit.section_path || []).join(" › ") || text("未标注章节", "Unlabelled section")}</span></div><p>{hit.content}</p><footer><code>{hit.evidence_id || hit.chunk_id}</code><em>{hit.evidence_level || (hit.is_neighbor ? text("相邻上下文", "Adjacent context") : hit.match_reason)}</em></footer>{supported.length ? <details><summary>{text(`支持 ${supported.length} 个正文段落`, `Supports ${supported.length} draft paragraphs`)}</summary>{supported.map((paragraph) => <p key={paragraph.paragraph_id} className="supported-paragraph"><b>{paragraph.paragraph_id}</b> {paragraph.text}</p>)}</details> : null}</article>;
      })}</div> : <div className="empty-state">{text("证据不足，当前版本使用兼容回退；请在文献库建立全文索引后重新生成。", "Evidence was insufficient and this version used the compatibility fallback. Build full-text indexes in Library and regenerate.")}</div>}
    </div>
  );
}

function SynthesisView({ section, mode }: { section?: { summary?: string; components?: SynthesisComponent[] }; mode?: string }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节尚无综合状态。", "This section has no synthesis state yet.")}</div>;
  return <div className="academic-state-view"><header><span className="step-label">{text("自动综合", "Automatic synthesis")}</span><h2>{text("章节知识综合", "Section knowledge synthesis")}</h2><p>{section.summary || text("综合组件已按当前证据准备。", "Synthesis components were prepared from current evidence.")}</p><small>{mode}</small></header><div className="academic-card-list">{(section.components || []).map((component) => <article key={component.component_id}><div><strong>{component.component_type}</strong><span className={`status-pill ${component.status === "supported" ? "ok" : "warning"}`}>{component.status}</span></div><p>{component.summary || component.purpose}</p><footer><span>{component.necessity}</span><span>{text(`${component.evidence_keys?.length || 0} 个证据键`, `${component.evidence_keys?.length || 0} evidence keys`)}</span></footer></article>)}</div></div>;
}

function WritingPlanView({ section, paperLabels }: { section?: { route?: string; overview_intent?: string; paragraphs?: WritingParagraph[]; claims?: WritingClaim[] }; paperLabels: Map<string, string> }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节尚无写作计划。", "This section has no writing plan yet.")}</div>;
  const claims = new Map((section.claims || []).map((claim) => [claim.claim_id, claim]));
  return <div className="academic-state-view"><header><span className="step-label">{text(`路线 ${section.route || "—"}`, `Route ${section.route || "—"}`)}</span><h2>{text("段落与 Claim/Citation 计划", "Paragraph and Claim/Citation plan")}</h2><p>{section.overview_intent}</p></header><div className="academic-card-list">{(section.paragraphs || []).map((paragraph) => <article key={paragraph.paragraph_id}><div><strong>{paragraph.paragraph_id} · {paragraph.argument_role}</strong></div><h3>{paragraph.theme}</h3><p>{paragraph.objective}</p><blockquote>{paragraph.reader_takeaway}</blockquote>{(paragraph.claim_ids || []).map((claimId) => { const claim = claims.get(claimId); return <details key={claimId}><summary>{claimId} · {claim?.claim_kind}</summary><p>{claim?.claim}</p><div className="chip-list">{(claim?.citation_group || []).map((paperId) => <span key={paperId}>{paperLabels.get(paperId) || paperId}</span>)}<span>{claim?.epistemic_status}</span><span>{claim?.support_status}</span></div><small>{claim?.evidence_ceiling}</small></details>; })}</article>)}</div></div>;
}

function ReviewView({ section }: { section?: { validations?: Array<{ rule_id?: string; status?: string; target_id?: string }>; reviews?: DraftReview[] } }) {
  const { text } = useUiText();
  if (!section) return <div className="empty-state">{text("当前章节尚无审校状态。", "This section has no review state yet.")}</div>;
  const reviews = section.reviews || [];
  return <div className="academic-state-view"><header><span className="step-label">{text("自动审校", "Automatic review")}</span><h2>{reviews[0]?.decision || text("待审校", "Pending")}</h2><p>{reviews[0]?.repair_objective || text("Claim、引用与段落身份校验已完成。", "Claim, citation, and paragraph identity validation is complete.")}</p></header><div className="academic-card-list">{(section.validations || []).map((validation, index) => <article key={`${validation.rule_id}-${index}`}><div><strong>{validation.rule_id}</strong><span className={`status-pill ${validation.status === "pass" ? "ok" : "warning"}`}>{validation.status}</span></div><p>{validation.target_id}</p></article>)}{reviews.flatMap((review) => review.issues || []).map((issue, index) => <article key={`${issue.type}-${index}`}><div><strong>{issue.type}</strong><span className="status-pill warning">{issue.severity}</span></div><p>{issue.reason}</p></article>)}</div></div>;
}

export function SectionsPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { selected: project } = useSelectedProject();
  const [selectedId, setSelectedId] = useState("");
  const [tab, setTab] = useState<WorkspaceTab>("section");
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
  const activeReportJob = payload?.report.jobs.find((job) => jobIsActive(job.status));
  const resumableReportJob = findResumableSectionJob(payload?.report.jobs || [], tasks);
  const reportJob = activeReportJob || resumableReportJob;
  const polledJob = useJob(jobId || reportJob?.id || "");
  const currentJob = polledJob.data || reportJob;
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
    const valid = taskOnly
      ? tasks.some((task) => taskId(task) === selectedId)
      : files.some((file) => file.name === selectedId);
    if (!valid) setSelectedId(taskOnly ? taskId(tasks[0]) : files[0]?.name || "");
    if (taskOnly && tab !== "tasks" && tab !== "report") setTab("tasks");
  }, [files, payload, selectedId, tab, taskOnly, tasks]);

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
    },
  });
  const confirm = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/sections/confirm`, {
      method: "POST",
      ...jsonBody({ revision: payload!.revision }),
    }),
    onSuccess: () => navigate(`/images?tab=review&project=${encodeURIComponent(project!.project_id)}`),
  });

  const availableTabs: Array<[WorkspaceTab, string]> = taskOnly
    ? [["tasks", text("写作要求", "Writing requirements")], ["report", text("生成报告", "Generation report")]]
    : [["synthesis", text("综合", "Synthesis")], ["writing", text("写作计划", "Writing Plan")], ["evidence", text("证据", "Evidence")], ["section", text("草稿", "Draft")], ["review", text("审校", "Review")], ["merged", text("合并预览", "Merged preview")], ["tasks", text("写作要求", "Writing requirements")], ["report", text("生成报告", "Generation report")]];
  const error = generate.error || confirm.error || (currentJob?.status === "failed" ? new Error(currentJob.error_message || text("章节生成失败。", "Section generation failed.")) : null);

  return (
    <main className="workspace page-container workspace-page sections-page">
      <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 4 · 章节撰写", "Stage 4 · Section drafting")}</p><h1>{text("章节撰写", "Section drafting")}</h1><p className="muted">{text("使用当前Blueprint任务生成章节，并在进入图像阶段前人工审核。", "Generate sections from the current blueprint tasks and review them before entering the figure stage.")}</p></div><ProjectSelector /></div>
      {sections.isPending ? <div className="empty-state">{text("正在加载章节产物…", "Loading section artifacts…")}</div> : null}
      {sections.error ? <ErrorState error={sections.error} onRetry={() => sections.refetch()} /> : null}
      {payload ? <>
        {payload.handoff.drafts_stale ? <p className="message message-warning">{text("Blueprint已更新，旧章节草稿保留在磁盘但不会作为当前流程内容显示。请重新生成。", "The blueprint changed. Old section drafts remain on disk but are not current workflow content. Regenerate them.")}</p> : null}
        <div className="sections-grid-react">
          <section className="pane section-list-react"><div className="pane-head"><div><span className="step-label">{text("章节", "Sections")}</span><h2>{tasks.length} {text("个章节", "sections")}</h2></div></div><div className="paper-list">{(taskOnly ? tasks : files).map((item) => {
            const id = taskOnly ? taskId(item as SectionTask) : (item as SectionFile).name;
            const task = taskOnly ? item as SectionTask : tasks.find((candidate) => taskId(candidate) === (item as SectionFile).section_id);
            const file = taskOnly ? undefined : item as SectionFile;
            return <button key={id} type="button" className={id === selectedId ? "paper-row active" : "paper-row"} onClick={() => { setSelectedId(id); if (!taskOnly && tab !== "tasks") setTab("section"); }}><span className="paper-row-main"><strong>{task?.heading || task?.section_id || file?.name}</strong><small>{file ? `${wordCount(file.content)} words` : `${task?.allowed_papers?.length || 0} ${text("篇论文", "papers")}`}</small></span><span className={file ? "status-dot ok" : "status-dot warning"} /></button>;
          })}</div></section>
          <section className="pane section-preview-react"><nav className="detail-tabs">{availableTabs.map(([value, label]) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{label}</button>)}</nav><div className="section-preview-content">
            {tab === "synthesis" ? <SynthesisView section={activeSynthesis} mode={payload.synthesis_state?.planning_mode} /> : null}
            {tab === "writing" ? <WritingPlanView section={activeWritingPlan} paperLabels={paperLabels} /> : null}
            {tab === "section" ? <MarkdownView content={displayedActiveContent} empty={text("当前章节尚未生成。", "This section has not been generated.")} /> : null}
            {tab === "merged" ? <MarkdownView content={displayedMergedContent} empty={text("当前没有合并预览。", "No merged preview is available.")} /> : null}
            {tab === "evidence" ? <EvidenceView section={activeEvidence} paragraphs={activeDraftParagraphs} paperLabels={paperLabels} /> : null}
            {tab === "review" ? <ReviewView section={activeDraftState} /> : null}
            {tab === "tasks" ? <TaskRequirements task={activeTask} paperLabels={paperLabels} /> : null}
            {tab === "report" ? <div className="job-report"><h2>{text("章节生成报告", "Section generation report")}</h2><p>{liveOutputCount}/{liveTaskCount} {currentJob && jobIsActive(currentJob.status) ? text("章已实时完成", "sections completed live") : text("个当前章节产物", "current section artifacts")}</p>{currentJob ? <SectionJobProgress job={currentJob} /> : <div className="empty-state">{text("尚未启动章节生成。", "Section generation has not started.")}</div>}{payload.report.jobs.map((job) => <details key={job.id}><summary>{job.status} · {job.id}</summary><p>{job.progress_current}/{job.progress_total} · {job.error_message || text("无错误", "No errors")}</p></details>)}</div> : null}
          </div></section>
          <aside className="pane section-gate-react"><div className="pane-head"><div><span className="step-label">{text("审核门", "Review gate")}</span><h2>{text("人工审核", "Human review")}</h2></div></div><div className="gate-body"><p>{payload.handoff.current ? text("当前草稿已生成，可审核后进入图像阶段。", "Current drafts are ready for review before the figure stage.") : currentJob && jobIsActive(currentJob.status) ? text("章节正在生成中。", "Sections are being generated.") : text("请从当前写作要求生成章节草稿。", "Generate section drafts from the current writing requirements.")}</p><ul><li>{text("每节是完整综述段落，不是提纲。", "Each section contains complete review prose, not outline fragments.")}</li><li>{text("引用来自该节允许论文。", "Citations come from papers allowed for that section.")}</li><li>{text("保留证据边界与不确定性。", "Evidence boundaries and uncertainty are preserved.")}</li><li>{text("图像需求与段落论证一致。", "Figure needs align with paragraph arguments.")}</li></ul></div></aside>
        </div>
        <div className="stage-action-bar"><div><strong>{payload.handoff.current ? text("确认章节", "Confirm sections") : canResumeCurrentJob ? text("继续未完成章节", "Resume unfinished sections") : text("生成所有章节", "Generate all sections")}</strong><p>{payload.handoff.current ? text("确认当前版本后进入图像处理。", "Confirm the current version to enter figure processing.") : currentJob && jobIsActive(currentJob.status) ? text(`生成中 ${currentJob.progress_current}/${currentJob.progress_total}`, `Generating ${currentJob.progress_current}/${currentJob.progress_total}`) : canResumeCurrentJob ? text(`已保留 ${currentJob!.progress_current}/${currentJob!.progress_total} 个章节，重试时仅继续未完成章节。`, `${currentJob!.progress_current}/${currentJob!.progress_total} sections are checkpointed; retry resumes only unfinished sections.`) : text("根据当前Blueprint写作要求生成全部章节。", "Generate every section from the current blueprint requirements.")}</p></div>{payload.handoff.current ? <button className="button button-primary" type="button" disabled={confirm.isPending} onClick={() => confirm.mutate()}>{confirm.isPending ? text("确认中…", "Confirming…") : text("确认并进入图像处理", "Confirm and enter figure processing")}</button> : <button className="button button-primary" type="button" disabled={generate.isPending || Boolean(currentJob && jobIsActive(currentJob.status))} onClick={() => generate.mutate()}>{currentJob && jobIsActive(currentJob.status) ? text("正在生成…", "Generating…") : generate.isPending ? text("正在提交…", "Submitting…") : canResumeCurrentJob ? text("继续生成", "Resume generation") : text("生成所有章节草稿", "Generate all section drafts")}</button>}{error ? <span className="message message-error">{error.message}</span> : null}</div>
      </> : null}
    </main>
  );
}
