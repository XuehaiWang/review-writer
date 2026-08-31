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
  front_matter: { title?: string; authors?: string[]; affiliations?: string[]; abstract?: string; keywords?: string[]; field_states?: Record<string, "generated" | "user_modified" | "user_omitted" | "missing">; generation_warnings?: string[] };
  front_matter_artifact_id: string;
  front_matter_current: boolean;
  validation: Record<string, unknown> & { valid?: boolean; blocking_issues?: string[]; warning_issues?: string[]; release_integrity_issues?: string[] };
  release: Record<string, unknown> & { status?: string };
  release_ready: boolean;
  pending_issue_count: number;
  pending_issues: string[];
  pending_issue_details: Array<{ target_type: string; target_id: string; issues: string[] }>;
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

type FinalTab = "preparation" | "conclusion" | "overview" | "final" | "audit" | "release" | "pdf";

function actionFromJobType(jobType?: string): FinalAction {
  const action = String(jobType || "").replace(/^final\./, "");
  return action === "conclusion" || action === "overview" || action === "export" || action === "pdf" ? action : "build";
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
  const [tab, setTab] = useState<FinalTab>("final");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [selectedJob, setSelectedJob] = useState({ projectId: "", jobId: "" });
  const [startingAction, setStartingAction] = useState<FinalAction>("build");
  const [overviewTitle, setOverviewTitle] = useState("");
  const [overviewSubtitle, setOverviewSubtitle] = useState("");
  const [overviewLabels, setOverviewLabels] = useState("");
  const [articleTitle, setArticleTitle] = useState("");
  const [articleAuthors, setArticleAuthors] = useState("");
  const [articleAffiliations, setArticleAffiliations] = useState("");
  const [articleAbstract, setArticleAbstract] = useState("");
  const [articleKeywords, setArticleKeywords] = useState("");
  const [omittedFrontMatter, setOmittedFrontMatter] = useState<string[]>([]);
  const [pdfLanguage, setPdfLanguage] = useState<"en" | "zh-CN">("en");
  const downloadedJob = useRef("");
  const pendingDownloadJob = useRef("");

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
    setArticleTitle(payload?.front_matter?.title || "");
    setArticleAuthors((payload?.front_matter?.authors || []).join("\n"));
    setArticleAffiliations((payload?.front_matter?.affiliations || []).join("\n"));
    setArticleAbstract(payload?.front_matter?.abstract || "");
    setArticleKeywords((payload?.front_matter?.keywords || []).join("\n"));
    setOmittedFrontMatter(Object.entries(payload?.front_matter?.field_states || {}).filter(([, status]) => status === "user_omitted").map(([field]) => field));
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
      const action = actionFromJobType(completed.job_type);
      if (action === "conclusion") { setTab("conclusion"); setShowAdvanced(true); }
      if (action === "overview") { setTab("overview"); setShowAdvanced(false); }
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
    mutationFn: (action: FinalAction) => apiRequest<Job>(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/${action}-jobs`,
      { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...jsonBody(action === "pdf" ? { language_profile: pdfLanguage } : {}) },
    ),
    onSuccess: (started, action) => {
      rememberJob(started.id);
      if (action === "export" || action === "pdf") pendingDownloadJob.current = started.id;
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
  const saveFrontMatter = useMutation({
    mutationFn: () => apiRequest(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/front-matter`,
      { method: "PUT", ...jsonBody({
        revision: payload!.revision,
        title: articleTitle.trim(),
        authors: articleAuthors.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
        affiliations: articleAffiliations.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
        abstract: articleAbstract.trim(),
        keywords: articleKeywords.split(/\r?\n|[,，;]/).map((value) => value.trim()).filter(Boolean),
        omitted_fields: omittedFrontMatter,
      }) },
    ),
    onSuccess: refresh,
  });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${encodeURIComponent(currentJobId || "")}/cancel`, { method: "POST" }) });
  const active = runJob.isPending || Boolean(currentJob && jobIsActive(currentJob.status));
  const error = saveFrontMatter.error || saveOverview.error || cancel.error || (currentJob?.status === "failed" ? new Error(currentJob.error_message || text("终稿任务失败。", "Final-stage task failed.")) : null);
  const tabs: Array<[FinalTab, string]> = [
    ["preparation", text("终稿准备", "Final preparation")],
    ["conclusion", text("结论", "Conclusion")],
    ["overview", text("综述总览图", "Review overview figure")],
    ["final", text("最终稿", "Final draft")],
    ["audit", text("终稿审计", "Final audit")],
    ["release", text("发布报告", "Release report")],
    ["pdf", text("PDF 与 QA", "PDF and QA")],
  ];
  const mainTabs = tabs.filter(([value]) => ["final", "overview", "pdf"].includes(value));
  const advancedTabs = tabs.filter(([value]) => ["preparation", "conclusion", "audit", "release"].includes(value));
  const approval = payload?.draft_approval.record || payload?.draft_approval || {};

  return <main className="workspace page-container workspace-page final-page">
    <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 7 · 终稿合并与导出", "Stage 7 · Final assembly and export")}</p><h1>{text("终稿生成、审计与 Word/PDF 导出", "Final generation, audit, and Word/PDF export")}</h1><p className="muted">{text("Word 保持原有路径；PDF 使用同一终稿内容状态和受控 LuaLaTeX 模板。", "Word keeps its existing path; PDF uses the same final content state and a controlled LuaLaTeX template.")}</p></div><ProjectSelector /></div>
    {final.isPending ? <div className="empty-state">{text("正在加载终稿产物…", "Loading final artifacts…")}</div> : null}
    {final.error ? <ErrorState error={final.error} onRetry={() => final.refetch()} /> : null}
    {payload ? <><div className="final-grid-react">
      <aside className="pane final-list-react"><div className="pane-head"><div><span className="step-label">{text("终稿产物", "Final outputs")}</span><h2>{project?.slug || project?.project_id}</h2></div></div><div className="draft-flow-list">{mainTabs.map(([value, label]) => <button key={value} className={tab === value ? "active" : ""} type="button" onClick={() => { setTab(value); setShowAdvanced(false); }}><strong>{label}</strong></button>)}<details className="workflow-advanced-nav" open={showAdvanced} onToggle={(event) => setShowAdvanced(event.currentTarget.open)}><summary>{text("准备、结论与检查报告", "Preparation, conclusion, and reports")}</summary><div>{advancedTabs.map(([value, label]) => <button key={value} className={tab === value ? "active" : ""} type="button" onClick={() => { setTab(value); setShowAdvanced(true); }}><strong>{label}</strong></button>)}</div></details></div></aside>
      <section className="pane final-main-react"><div className="pane-head"><div><span className="step-label">{payload.freshness.stale ? text("已过期", "Out of date") : text("当前", "Current")}</span><h2>{tabs.find(([value]) => value === tab)?.[1]}</h2></div></div><div className="final-document-react">
        {payload.final_current && payload.release_current ? <div className={payload.pending_issue_count ? "message message-warning" : "message message-success"}>{payload.pending_issue_count ? <>{text(`终稿已生成 · 还有 ${payload.pending_issue_count} 项待处理。`, `Final draft generated · ${payload.pending_issue_count} item(s) still need attention.`)} <button className="button button-quiet" type="button" onClick={() => { setTab("release"); setShowAdvanced(true); }}>{text("查看问题明细", "View issue details")}</button></> : text("终稿已生成。", "Final draft generated.")}</div> : null}
        {tab === "preparation" ? <>
          <div className="final-preparation-cards"><article className={payload.draft_approval_current ? "good" : "bad"}><h3>{payload.draft_approval_current ? text("初稿已确认", "Draft approved") : text("需要先确认初稿", "Draft approval required")}</h3>{approval.score !== undefined ? <p>{text("分数", "Score")}: {String(approval.score)} / {String(approval.goal || "")}</p> : null}</article><article><strong>{text("结论", "Conclusion")}</strong><StatusPill exists={Boolean(payload.conclusion_artifact_id)} current={payload.conclusion_current} /></article><article><strong>{text("综述总览图", "Review overview figure")}</strong><StatusPill exists={payload.overview_figure_exists} current={payload.overview_figure_current} /></article><article><strong>{text("最终稿", "Final draft")}</strong><StatusPill exists={Boolean(payload.final_artifact_id)} current={payload.final_current} optional={false} /></article></div>
          <section className="front-matter-editor"><header><div><span className="step-label">{text("文章前置信息", "Article front matter")}</span><h3>{text("标题、作者、单位、摘要与关键词", "Title, authors, affiliations, abstract, and keywords")}</h3></div><StatusPill exists={Boolean(payload.front_matter_artifact_id)} current={payload.front_matter_current} /></header><p className="muted">{text("生成最终稿时会根据当前正文自动生成摘要和关键词，并将账户显示名称作为作者候选；人工修改后的字段不会被覆盖。摘要不会读取结论、挑战、未来展望或参考文献。", "Building the final draft generates an abstract and keywords from the current body and uses the account display name as an author candidate. User-edited fields are never overwritten, and the abstract excludes conclusions, challenges, future directions, and references.")}</p>{payload.front_matter.generation_warnings?.map((warning) => <p className="message message-warning" key={warning}>{warning}</p>)}<div className="front-matter-grid"><label className="front-matter-wide">{text("文章标题", "Article title")}<input value={articleTitle} onChange={(event) => setArticleTitle(event.target.value)} /></label><label>{text("作者（每行一位）", "Authors (one per line)")}<textarea rows={5} disabled={omittedFrontMatter.includes("authors")} value={articleAuthors} onChange={(event) => setArticleAuthors(event.target.value)} /></label><label>{text("作者单位（每行一个）", "Affiliations (one per line)")}<textarea rows={5} disabled={omittedFrontMatter.includes("affiliations")} value={articleAffiliations} onChange={(event) => setArticleAffiliations(event.target.value)} /></label><label className="front-matter-wide">{text("摘要", "Abstract")}<textarea rows={8} disabled={omittedFrontMatter.includes("abstract")} value={articleAbstract} onChange={(event) => setArticleAbstract(event.target.value)} /></label><label className="front-matter-wide">{text("关键词（每行一个，也可用逗号分隔）", "Keywords (one per line or comma-separated)")}<textarea rows={4} disabled={omittedFrontMatter.includes("keywords")} value={articleKeywords} onChange={(event) => setArticleKeywords(event.target.value)} /></label></div><details className="advanced-panel"><summary>{text("明确省略可选字段", "Explicitly omit optional fields")}</summary><div className="advanced-panel-body"><p className="muted">{text("只有勾选后系统才会持续省略；单纯留空仍允许下次生成最终稿时自动补全。", "Checked fields remain omitted. Leaving a field blank still allows the next final build to generate it.")}</p>{[["authors", text("省略作者", "Omit authors")], ["affiliations", text("省略单位", "Omit affiliations")], ["abstract", text("省略摘要", "Omit abstract")], ["keywords", text("省略关键词", "Omit keywords")]].map(([field, label]) => <label className="check-label" key={field}><input type="checkbox" checked={omittedFrontMatter.includes(field)} onChange={(event) => setOmittedFrontMatter((current) => event.target.checked ? [...new Set([...current, field])] : current.filter((value) => value !== field))} />{label}</label>)}</div></details><button className="button button-primary" type="button" disabled={!payload.draft_approval_current || !articleTitle.trim() || saveFrontMatter.isPending} onClick={() => saveFrontMatter.mutate()}>{saveFrontMatter.isPending ? text("正在保存…", "Saving…") : text("保存文章前置信息", "Save article front matter")}</button>{payload.front_matter_artifact_id && !payload.final_current ? <p className="message message-warning">{text("文章前置信息已更新；请重新生成最终稿后再导出。", "Front matter changed; rebuild the final draft before export.")}</p> : null}</section>
          <section className="evidence-boundary-card"><header><span className="step-label">{text("范围与证据边界", "Scope and evidence boundary")}</span><h3>{text("基于当前确认语料的叙述性专题综述", "Narrative review of the confirmed corpus")}</h3></header><p>{text("本稿只声明覆盖用户确认的论文集合，不声称穷尽全领域文献。", payload.evidence_boundary.statement || "This review is limited to the user-confirmed corpus and does not claim exhaustive global coverage.")}</p><dl><div><dt>{text("确认论文", "Selected papers")}</dt><dd>{payload.evidence_boundary.selected_paper_count || 0}</dd></div><div><dt>{text("可写主论文", "Writeable primary papers")}</dt><dd>{payload.evidence_boundary.writeable_primary_paper_count || 0}</dd></div><div><dt>{text("未解决主论文", "Unresolved primary papers")}</dt><dd>{payload.evidence_boundary.unresolved_primary_paper_ids?.length || 0}</dd></div><div><dt>{text("问题级缺口", "Question-level gaps")}</dt><dd>{payload.evidence_boundary.corpus_gap_questions?.length || 0}</dd></div></dl>{payload.evidence_boundary.warnings?.length ? <details><summary>{text(`查看 ${payload.evidence_boundary.warnings.length} 项边界警告`, `View ${payload.evidence_boundary.warnings.length} boundary warnings`)}</summary><ul>{payload.evidence_boundary.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></details> : <p className="message message-success">{text("当前未记录额外证据边界警告。", "No additional evidence-boundary warnings are recorded.")}</p>}</section>
        </> : null}
        {tab === "conclusion" ? <MarkdownView content={payload.conclusion_generated_md} empty={text("尚未生成结论；也可直接生成最终稿。", "No conclusion generated; you can also build the final draft directly.")} /> : null}
        {tab === "overview" ? payload.overview_figure_exists ? <><figure className="overview-figure-react"><img src={payload.overview_figure_url} alt={text("综述总览图", "Review overview figure")} /></figure><section className="overview-text-editor"><h3>{text("可编辑总览图文字", "Editable overview figure text")}</h3><label>{text("标题", "Title")}<input value={overviewTitle} onChange={(event) => setOverviewTitle(event.target.value)} /></label><label>{text("副标题", "Subtitle")}<input value={overviewSubtitle} onChange={(event) => setOverviewSubtitle(event.target.value)} /></label><label>{text("标签（每行一个）", "Labels (one per line)")}<textarea rows={7} value={overviewLabels} onChange={(event) => setOverviewLabels(event.target.value)} /></label><button className="button button-primary" type="button" disabled={!overviewTitle.trim() || saveOverview.isPending} onClick={() => saveOverview.mutate()}>{text("保存总览图文字", "Save overview text")}</button></section></> : <div className="empty-state">{text("尚未生成总览图；也可直接生成最终稿。", "No overview figure generated; you can also build the final draft directly.")}</div> : null}
        {tab === "final" ? <MarkdownView content={payload.final_draft_md} empty={text("尚未生成最终稿。", "Final draft not generated yet.")} /> : null}
        {tab === "audit" ? <MarkdownView content={payload.final_audit_report_md} empty={text("尚未执行终稿审计。", "Final audit has not run.")} /> : null}
        {tab === "release" ? <MarkdownView content={payload.release_report_md} empty={text("尚未生成发布报告。", "Release report not generated yet.")} /> : null}
        {tab === "pdf" ? <div className="pdf-qa-summary"><h3>{text("期刊型 PDF 渲染状态", "Journal-style PDF render status")}</h3>{payload.final_pdf_exists ? <><p><strong>{text("语言", "Language")}:</strong> {payload.pdf_language_profile}</p><p><strong>{text("编译器", "Compiler")}:</strong> {String(payload.render_manifest?.compiler || "LuaLaTeX")}</p><p><strong>{text("自动 QA", "Automatic QA")}:</strong> {String(payload.pdf_qa?.status || "")}</p><p><strong>{text("页数", "Pages")}:</strong> {String(payload.pdf_qa?.page_count || "")}</p><p><strong>{text("字体全部嵌入", "All fonts embedded")}:</strong> {payload.pdf_qa?.all_fonts_embedded ? text("是", "Yes") : text("否", "No")}</p><div className="final-download-row"><a className="button button-primary" href={payload.pdf_url} download={`final_draft.${payload.pdf_language_profile || "en"}.pdf`}>{text("下载当前 PDF", "Download current PDF")}</a><a className="button button-secondary" href={payload.tex_url} download="manuscript.tex">{text("下载 LaTeX 源文件", "Download LaTeX source")}</a></div></> : <div className="empty-state">{text("尚未生成 PDF。选择语言后一次点击即可后台编译和自动 QA。", "No PDF generated yet. Choose a language and compile with automatic QA in one click.")}</div>}</div> : null}
      </div></section>
      <aside className="pane final-actions-react"><div className="pane-head"><div><span className="step-label">{text("生成操作", "Generation actions")}</span><h2>{text("生成操作", "Generation actions")}</h2></div></div><div className="gate-body">
        <p>{text("所有操作严格绑定当前人工确认的初稿版本。", "Every operation is strictly bound to the currently approved draft version.")}</p>
        <button className="button button-secondary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("conclusion")}>{text("生成结论", "Generate conclusion")}</button>
        <button className="button button-secondary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("overview")}>{text("生成总览图", "Generate overview figure")}</button>
        <button className="button button-primary" type="button" disabled={!payload.draft_approval_current || active} onClick={() => startJob("build")}>{text("生成最终稿", "Generate final draft")}</button>
        <button className="button button-primary" type="button" disabled={!payload.final_current || !payload.release_current || active} onClick={() => startJob("export")}>{text("生成并下载Word", "Generate and download Word")}</button>
        <label className="pdf-language-field">{text("PDF 语言", "PDF language")}<select value={pdfLanguage} onChange={(event) => setPdfLanguage(event.target.value as "en" | "zh-CN")}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label>
        <button className="button button-primary" type="button" disabled={!payload.final_current || !payload.release_current || active} onClick={() => startJob("pdf")}>{text("生成并下载 LaTeX PDF", "Generate and download LaTeX PDF")}</button>
        <button className="button button-quiet danger" type="button" disabled={!currentJob || !jobIsActive(currentJob.status) || cancel.isPending} onClick={() => cancel.mutate()}>{text("取消当前任务", "Cancel current task")}</button>
        {runJob.isPending ? <FinalJobStatus startingAction={startingAction} /> : runJob.error ? <FinalJobStatus startingAction={startingAction} submissionError={runJob.error} /> : currentJob ? <FinalJobStatus job={currentJob} startingAction={currentAction} /> : null}
        <div className="final-status-summary"><div><strong>{text("初稿", "Draft")}</strong><StatusPill exists current={payload.draft_approval_current} optional={false} /></div><div><strong>{text("结论", "Conclusion")}</strong><StatusPill exists={Boolean(payload.conclusion_artifact_id)} current={payload.conclusion_current} /></div><div><strong>{text("总览图", "Overview figure")}</strong><StatusPill exists={payload.overview_figure_exists} current={payload.overview_figure_current} /></div><div><strong>{text("最终稿", "Final draft")}</strong><StatusPill exists={Boolean(payload.final_artifact_id)} current={payload.final_current} optional={false} /></div><div><strong>{text("发布", "Release")}</strong><StatusPill exists={Boolean(payload.release?.status)} current={payload.release_current} optional={false} /></div><div><strong>PDF</strong><StatusPill exists={Boolean(payload.pdf_url)} current={payload.final_pdf_exists} /></div></div>
        {payload.final_draft_docx_exists ? <a className="button button-secondary" href={payload.docx_url} download="final_draft.docx">{text("下载当前DOCX", "Download current DOCX")}</a> : null}
        {payload.final_draft_docx_stale ? <p className="message message-warning">{text("现有Word已过期，请重新生成并下载。", "The existing Word file is stale. Regenerate and download it.")}</p> : null}
        {payload.final_pdf_stale ? <p className="message message-warning">{text("现有 PDF 已过期，请重新生成。", "The existing PDF is stale. Regenerate it.")}</p> : null}
      </div></aside>
    </div>{error ? <p className="message message-error">{error.message}</p> : null}</> : null}
  </main>;
}
