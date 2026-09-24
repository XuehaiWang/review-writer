import { LocalizedError } from "../../components/LocalizedError";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { ACTIVE_JOB_POLL_INTERVAL_MS } from "../../api/polling";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { MarkdownView } from "../../components/MarkdownView";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { FinalJobStatus, finalActionFromJobType, type FinalAction } from "./FinalJobStatus";
import { readFinalJobId, writeFinalJobId } from "./finalJobPersistence";

import { FinalManuscriptPreview, type FinalVersion } from "./FinalManuscriptPreview";
import { FinalIssuesPanel, type FinalIssue } from "./FinalIssuesPanel";

type FinalPayload = {
  versions?: FinalVersion[];
  project_id: string;
  revision: number;
  status: string;
  draft_approval_current: boolean;
  draft_approval: Record<string, unknown> & { record?: Record<string, unknown> };
  final_draft_md: string;
  final_artifact_id: string;
  final_current: boolean;
  front_matter: { title?: string; authors?: string[]; affiliations?: string[]; abstract?: string; keywords?: string[]; field_states?: Record<string, "generated" | "user_modified" | "user_omitted" | "missing">; generation_warnings?: string[] };
  front_matter_artifact_id: string;
  front_matter_current: boolean;
  validation: Record<string, unknown> & { valid?: boolean; blocking_issues?: string[]; warning_issues?: string[]; release_integrity_issues?: string[] };
  release: Record<string, unknown> & { status?: string };
  release_ready: boolean;
  pending_issue_count: number;
  pending_issues: string[];
  pending_issue_details: FinalIssue[];
  evidence_boundary: {
    review_type?: string;
    coverage_claim?: string;
    selected_paper_count?: number;
    writeable_primary_paper_count?: number;
    unresolved_primary_paper_ids?: string[];
    context_only_primary_paper_ids?: string[];
    corpus_gap_questions?: string[];
    unverified_manual_paragraph_ids?: string[];
    warnings?: string[];
    statement?: string;
  };
  release_current: boolean;
  docx_url: string;
  final_draft_docx_exists: boolean;
  final_draft_docx_stale: boolean;
  pdf_url: string;
  tex_url: string;
  pdf_language_profile: string;
  final_pdf_exists: boolean;
  final_pdf_stale: boolean;
  render_manifest: Record<string, unknown> & { template?: string; template_version?: string; language_profile?: string; compiler?: string; shell_escape?: boolean };
  pdf_qa: Record<string, unknown> & { status?: string; page_count?: number; all_fonts_embedded?: boolean; blocking_issues?: unknown[]; warning_issues?: unknown[] };
  active_final_job_id: string;
  active_final_job_type: string;
  latest_final_job_id: string;
  latest_final_job_type: string;
  latest_final_job_status: string;
  final_audit_report_md: string;
  release_report_md: string;
  freshness: { draft_stale: boolean; final_stale: boolean; release_stale: boolean; pdf_stale?: boolean; stale: boolean };
};

type FinalTab = "preparation" | "final" | "audit" | "pdf";

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
  const [tab, setTab] = useState<FinalTab>("final");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [selectedJob, setSelectedJob] = useState({ projectId: "", jobId: "" });
  const [startingAction, setStartingAction] = useState<FinalAction>("build");
  const [articleAuthors, setArticleAuthors] = useState("");
  const [articleAffiliations, setArticleAffiliations] = useState("");
  const [pdfLanguage, setPdfLanguage] = useState<"en" | "zh-CN">("en");
  const downloadedJob = useRef("");
  const pendingDownloadJob = useRef("");
  const autoAssemblyAttempts = useRef(new Set<string>());

  const final = useQuery({
    queryKey: ["final", project?.project_id || ""],
    queryFn: () => apiRequest<FinalPayload>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/final`),
    enabled: Boolean(project),
    refetchInterval: (query) => query.state.data?.active_final_job_id ? ACTIVE_JOB_POLL_INTERVAL_MS : false,
    refetchIntervalInBackground: true,
  });
  const payload = final.data;
  const localJobId = selectedJob.projectId === project?.project_id ? selectedJob.jobId : "";
  const storedJobId = readFinalJobId(project?.project_id || "");
  const storedCurrentJobId = storedJobId && (storedJobId === payload?.active_final_job_id || storedJobId === payload?.latest_final_job_id) ? storedJobId : "";
  const currentJobId = payload?.active_final_job_id || localJobId || payload?.latest_final_job_id || storedCurrentJobId;
  const job = useJob(currentJobId || "");
  const currentJob = job.data;
  const currentAction = finalActionFromJobType(currentJob?.job_type || payload?.active_final_job_type || payload?.latest_final_job_type);
  const refresh = async () => queryClient.invalidateQueries({ queryKey: ["final", project?.project_id || ""] });

  const rememberJob = (jobId: string) => {
    if (!project?.project_id || !jobId) return;
    writeFinalJobId(project.project_id, jobId);
    setSelectedJob({ projectId: project.project_id, jobId });
  };

  useEffect(() => {
    setArticleAuthors((payload?.front_matter?.authors || []).join("\n"));
    setArticleAffiliations((payload?.front_matter?.affiliations || []).join("\n"));
  }, [payload?.front_matter]);

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
      const action = finalActionFromJobType(completed.job_type);
      if (action === "build") { setTab("final"); setShowAdvanced(false); }
      if (action === "pdf") { setTab("pdf"); setShowAdvanced(false); }
      if ((action === "export" || action === "pdf") && pendingDownloadJob.current === completed.id && downloadedJob.current !== completed.id) {
        downloadedJob.current = completed.id;
        pendingDownloadJob.current = "";
        const artifactId = String(action === "pdf" ? completed.result?.pdf_artifact_id : completed.result?.docx_artifact_id || "");
        const href = artifactId ? `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content` : "";
        if (href) {
          const link = document.createElement("a");
          link.href = href;
          link.download = String(completed.result?.download_name || (action === "pdf" ? "final_draft.pdf" : "final_draft.docx"));
          document.body.append(link);
          link.click();
          link.remove();
        }
      }
    })();
  }, [job.data?.id, job.data?.status]);

  const runJob = useMutation({
    mutationFn: ({ action, idempotencyKey }: { action: FinalAction; idempotencyKey?: string }) => apiRequest<Job>(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/${action}-jobs`,
      { method: "POST", ...jsonBody(action === "pdf" ? { language_profile: pdfLanguage } : {}), headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey || newIdempotencyKey() } },
    ),
    onSuccess: (started, { action }) => {
      rememberJob(started.id);
      if (action === "export" || action === "pdf") pendingDownloadJob.current = started.id;
      void refresh();
    },
  });
  const startJob = (action: FinalAction) => {
    autoAssemblyAttempts.current.add(project!.project_id);
    setStartingAction(action);
    runJob.mutate({ action });
  };
  const saveFrontMatter = useMutation({
    mutationFn: () => apiRequest(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/front-matter`,
      { method: "PUT", ...jsonBody({
        revision: payload!.revision,
        authors: articleAuthors.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
        affiliations: articleAffiliations.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      }) },
    ),
    onSuccess: refresh,
  });
  const resume = useMutation({
    mutationFn: () => apiRequest<Job>(`/api/v1/jobs/${encodeURIComponent(currentJobId || "")}/retry`, {
      method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() },
    }),
    onSuccess: (started) => {
      runJob.reset();
      rememberJob(started.id);
      if (["final.export", "final.pdf"].includes(started.job_type)) pendingDownloadJob.current = started.id;
      void refresh();
    },
  });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${encodeURIComponent(currentJobId || "")}/cancel`, { method: "POST" }) });
  const active = runJob.isPending || resume.isPending || Boolean(payload?.active_final_job_id) || Boolean(currentJob && jobIsActive(currentJob.status));
  useEffect(() => {
    if (!project || !payload || payload.final_artifact_id || !payload.draft_approval_current || active) return;
    const key = project.project_id;
    if (autoAssemblyAttempts.current.has(key)) return;
    // Failed/cancelled attempts require an explicit retry, including after a page reload.
    if (payload.latest_final_job_type === "final.build" && ["failed", "cancelled", "interrupted", "succeeded"].includes(payload.latest_final_job_status)) return;
    autoAssemblyAttempts.current.add(key);
    setStartingAction("build");
    runJob.mutate({ action: "build", idempotencyKey: `final-initial:${project.project_id}:${payload.revision}` });
  }, [project?.project_id, payload, active]);
  const canExport = Boolean(payload?.final_current && payload?.release_current && !active);
  const wordReady = Boolean(payload?.final_draft_docx_exists && !payload.final_draft_docx_stale);
  const pdfReady = Boolean(payload?.final_pdf_exists && !payload.final_pdf_stale && payload.pdf_language_profile === pdfLanguage);
  const publicationChanged = articleAuthors !== (payload?.front_matter?.authors || []).join("\n")
    || articleAffiliations !== (payload?.front_matter?.affiliations || []).join("\n");
  const error = saveFrontMatter.error || cancel.error || resume.error;
  const tabs: Array<[FinalTab, string]> = [
    ["preparation", text("出版信息", "Publication information")],
    ["final", text("最终稿", "Final draft")],
    ["audit", text("检查详情", "Checks")],
    ["pdf", text("PDF 与 QA", "PDF and QA")],
  ];
  const mainTabs = tabs.filter(([value]) => ["final", "pdf"].includes(value));
  const advancedTabs = tabs.filter(([value]) => ["preparation", "audit"].includes(value));
  const approval = payload?.draft_approval.record || payload?.draft_approval || {};

  return <main className="workspace page-container workspace-page final-page">
    <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 7 · 终稿合并与导出", "Stage 7 · Final assembly and export")}</p><h1>{text("终稿预览与导出", "Final preview and export")}</h1><p className="muted">{text("汇总已确认的初稿，检查格式并导出 Word 或 PDF。", "Assemble the approved Draft, check formatting, and export Word or PDF.")}</p></div><ProjectSelector /></div>
    {final.isPending ? <div className="empty-state">{text("正在加载终稿产物…", "Loading final artifacts…")}</div> : null}
    {final.error ? <ErrorState error={final.error} onRetry={() => final.refetch()} /> : null}
    {payload ? <><div className="final-grid-react">
      <aside className="pane final-list-react"><div className="pane-head"><div><span className="step-label">{text("终稿产物", "Final outputs")}</span><h2>{project?.slug || project?.project_id}</h2></div></div><div className="draft-flow-list">{mainTabs.map(([value, label]) => <button key={value} className={tab === value ? "active" : ""} type="button" onClick={() => { setTab(value); setShowAdvanced(false); }}><strong>{label}</strong></button>)}<details className="workflow-advanced-nav" open={showAdvanced} onToggle={(event) => setShowAdvanced(event.currentTarget.open)}><summary>{text("出版信息与检查详情", "Publication information and checks")}</summary><div>{advancedTabs.map(([value, label]) => <button key={value} className={tab === value ? "active" : ""} type="button" onClick={() => { setTab(value); setShowAdvanced(true); }}><strong>{label}</strong></button>)}</div></details></div></aside>
      <section className="pane final-main-react"><div className="pane-head"><div><span className="step-label">{payload.freshness.stale ? text("已过期", "Out of date") : text("当前", "Current")}</span><h2>{tabs.find(([value]) => value === tab)?.[1]}</h2></div></div><div className="final-document-react">
        {payload.final_current && payload.release_current ? <div className={payload.pending_issue_count ? "message message-warning" : "message message-success"}>{payload.pending_issue_count ? <>{text(`终稿已生成，可下载 · ${payload.pending_issue_count} 项内容建议核对。`, `Final available for download · ${payload.pending_issue_count} finding(s) to review.`)} <button className="button button-quiet" type="button" onClick={() => { setTab("audit"); setShowAdvanced(true); }}>{text("查看处理建议", "View suggested actions")}</button></> : text("终稿已生成。", "Final draft generated.")}</div> : null}
        {tab === "preparation" ? <>
          <div className="final-preparation-cards"><article className={payload.draft_approval_current ? "good" : "bad"}><h3>{payload.draft_approval_current ? text("初稿已确认", "Draft approved") : text("需要先确认初稿", "Draft approval required")}</h3>{approval.score !== undefined ? <p>{text("分数", "Score")}: {String(approval.score)} / {String(approval.goal || "")}</p> : null}</article><article><strong>{text("最终稿", "Final draft")}</strong><StatusPill exists={Boolean(payload.final_artifact_id)} current={payload.final_current} optional={false} /></article></div>
          <section className="front-matter-editor"><h3>{text("作者与单位", "Authors and affiliations")}</h3><p>{text("文章文字请在初稿修改；终稿只装配已确认内容，不自动生成摘要或结论。", "Edit prose in Draft. Final assembles approved content without generating summaries.")}</p>
            <label>{text("作者（每行一位）", "Authors (one per line)")}<textarea rows={4} value={articleAuthors} onChange={e => setArticleAuthors(e.target.value)} /></label>
            <label>{text("单位（每行一个）", "Affiliations (one per line)")}<textarea rows={4} value={articleAffiliations} onChange={e => setArticleAffiliations(e.target.value)} /></label>
            <button className="button button-primary" disabled={!payload.draft_approval_current || active || !publicationChanged || saveFrontMatter.isPending} onClick={() => saveFrontMatter.mutate()}>{text("保存出版信息", "Save publication information")}</button>
          </section>
          <section className="evidence-boundary-card"><header><span className="step-label">{text("范围与证据边界", "Scope and evidence boundary")}</span><h3>{text("基于当前确认语料的叙述性专题综述", "Narrative review of the confirmed corpus")}</h3></header><p>{text("本稿只声明覆盖用户确认的论文集合，不声称穷尽全领域文献。", payload.evidence_boundary.statement || "This review is limited to the user-confirmed corpus and does not claim exhaustive global coverage.")}</p><dl><div><dt>{text("确认论文", "Selected papers")}</dt><dd>{payload.evidence_boundary.selected_paper_count || 0}</dd></div><div><dt>{text("可写主论文", "Writeable primary papers")}</dt><dd>{payload.evidence_boundary.writeable_primary_paper_count || 0}</dd></div><div><dt>{text("未解决主论文", "Unresolved primary papers")}</dt><dd>{payload.evidence_boundary.unresolved_primary_paper_ids?.length || 0}</dd></div><div><dt>{text("问题级缺口", "Question-level gaps")}</dt><dd>{payload.evidence_boundary.corpus_gap_questions?.length || 0}</dd></div></dl>{payload.evidence_boundary.warnings?.length ? <details><summary>{text(`查看 ${payload.evidence_boundary.warnings.length} 项边界警告`, `View ${payload.evidence_boundary.warnings.length} boundary warnings`)}</summary><ul>{payload.evidence_boundary.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details> : <p className="message message-success">{text("当前未记录额外证据边界警告。", "No additional evidence-boundary warnings are recorded.")}</p>}</section>
        </> : null}
        {tab === "final" ? <FinalManuscriptPreview key={project!.project_id} projectId={project!.project_id} markdown={payload.final_draft_md} versions={payload.versions || []} /> : null}
        {tab === "audit" ? <><FinalIssuesPanel projectId={project!.project_id} issues={payload.pending_issue_details || []} busy={active || !payload.draft_approval_current} onSync={async () => { setStartingAction("build"); await runJob.mutateAsync({ action: "build" }); }} /><details className="advanced-panel"><summary>{text("完整技术报告（供排查）", "Full technical report")}</summary><MarkdownView content={payload.final_audit_report_md} empty={text("尚未执行终稿检查。", "No final checks yet.")} /><MarkdownView content={payload.release_report_md} /></details></> : null}
        {tab === "pdf" ? <div className="pdf-qa-summary"><h3>{text("期刊型 PDF 渲染状态", "Journal-style PDF render status")}</h3>{payload.final_pdf_exists ? <><p><strong>{text("语言", "Language")}:</strong> {payload.pdf_language_profile}</p><p><strong>{text("编译器", "Compiler")}:</strong> {String(payload.render_manifest?.compiler || "LuaLaTeX")}</p><p><strong>{text("自动 QA", "Automatic QA")}:</strong> {String(payload.pdf_qa?.status || "")}</p><p><strong>{text("页数", "Pages")}:</strong> {String(payload.pdf_qa?.page_count || "")}</p><p><strong>{text("字体全部嵌入", "All fonts embedded")}:</strong> {payload.pdf_qa?.all_fonts_embedded ? text("是", "Yes") : text("否", "No")}</p><div className="final-download-row"><a className="button button-secondary" href={payload.tex_url} download="manuscript.tex">{text("下载 LaTeX 源文件", "Download LaTeX source")}</a></div></> : <div className="empty-state">{text("尚未生成 PDF。选择语言后一次点击即可后台编译和自动 QA。", "No PDF generated yet. Choose a language and compile with automatic QA in one click.")}</div>}</div> : null}
      </div></section>
      <aside className="pane final-actions-react"><div className="pane-head"><div><span className="step-label">{text("同步与导出", "Sync and export")}</span><h2>{text("同步与导出", "Sync and export")}</h2></div></div><div className="gate-body">
        <p>{text("首次进入自动组装已确认的初稿，不重新撰写正文。后续修改按需同步，未接受的候选不会加入终稿。", "The approved Draft is assembled automatically on first entry without rewriting. Sync later changes when needed; unaccepted candidates are excluded.")}</p>
        {!payload.draft_approval_current ? <p className="message message-warning">{text("请先在初稿确认已保存的正文，再同步终稿。", "Approve the saved Draft before synchronizing Final.")}</p> : null}
        {payload.final_artifact_id && !payload.final_current ? <p className="message message-warning">{text("初稿或出版信息已更新，请同步后导出。", "Draft or publication information changed. Synchronize before exporting.")}</p> : null}
        {!payload.final_current ? <button className="button button-primary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("build")}>{payload.final_artifact_id ? text("同步最新初稿", "Sync latest Draft") : text("重试组装终稿", "Retry final assembly")}</button> : null}
        {wordReady && canExport ? <a className="button button-primary" href={payload.docx_url} download="final_draft.docx">{text("下载 Word", "Download Word")}</a> : <button className="button button-primary" type="button" disabled={!canExport} onClick={() => startJob("export")}>{text("下载 Word", "Download Word")}</button>}
        <label className="pdf-language-field">{text("PDF 语言", "PDF language")}<select value={pdfLanguage} onChange={(event) => setPdfLanguage(event.target.value as "en" | "zh-CN")}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label>
        {pdfReady && canExport ? <a className="button button-primary" href={payload.pdf_url} download={`final_draft.${pdfLanguage}.pdf`}>{text("下载 PDF", "Download PDF")}</a> : <button className="button button-primary" type="button" disabled={!canExport} onClick={() => startJob("pdf")}>{text("下载 PDF", "Download PDF")}</button>}
        <p className="muted">{text("首次下载或内容更新后自动生成文件；已有最新文件直接下载。", "Files are generated on first download or after changes; current files download directly.")}</p>
        {currentJob && jobIsActive(currentJob.status) ? <button className="button button-quiet danger" type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消当前任务", "Cancel current task")}</button> : null}
        {runJob.isPending ? <FinalJobStatus startingAction={startingAction} /> : runJob.error ? <FinalJobStatus startingAction={startingAction} submissionError={runJob.error} /> : currentJob ? <FinalJobStatus job={currentJob} startingAction={currentAction} onResume={() => resume.mutate()} resuming={resume.isPending} /> : null}
        {currentJob?.status === "succeeded" && currentJob.result?.candidate_pending === true ? <p role="status">{text("生成期间终稿已有更新。本次结果已保留在版本历史，可查看后采用。", "The manuscript changed during generation. This result is available in version history for review and adoption.")}</p> : null}
        <div className="final-status-summary"><div><strong>{text("初稿", "Draft")}</strong><StatusPill exists current={payload.draft_approval_current} optional={false} /></div><div><strong>{text("最终稿", "Final draft")}</strong><StatusPill exists={Boolean(payload.final_artifact_id)} current={payload.final_current} optional={false} /></div><div><strong>{text("发布", "Release")}</strong><StatusPill exists={Boolean(payload.release?.status)} current={payload.release_current} optional={false} /></div><div><strong>PDF</strong><StatusPill exists={Boolean(payload.pdf_url)} current={payload.final_pdf_exists && !payload.final_pdf_stale} /></div></div>
        {payload.final_draft_docx_stale ? <p className="message message-warning">{text("现有Word已过期，请重新生成并下载。", "The existing Word file is stale. Regenerate and download it.")}</p> : null}
        {payload.final_pdf_stale ? <p className="message message-warning">{text("现有 PDF 已过期，请重新生成。", "The existing PDF is stale. Regenerate it.")}</p> : null}
      </div></aside>
    </div>{error ? <p className="message message-error"><LocalizedError error={error} /></p> : null}</> : null}
  </main>;
}
