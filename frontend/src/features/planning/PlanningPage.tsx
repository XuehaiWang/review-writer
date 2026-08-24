import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { queryKeys } from "../../api/queries";
import type { Job } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { useUiText } from "../../i18n/useUiText";
import { jobIsActive } from "../../hooks/useJob";
import { buildPaperDisplayLabels, OutlineBuilder, parseOutlineMarkdown, validateVisualOutline } from "./OutlineBuilder";

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
    review_status?: "not_required" | "needs_review" | "human_checked";
    confidence?: number;
    evidence_ceiling?: string;
    evidence_refs?: Array<{ page_start?: number | null; page_end?: number | null; chunk_id?: string }>;
  }>;
  fact_enrichment?: { status?: string; review_status?: string; fact_count?: number; error?: string };
};

type BlueprintSection = Record<string, unknown> & {
  section_id?: string;
  title?: string;
  section_thesis?: string;
  section_goal?: string;
  assigned_papers?: unknown[];
  major_papers?: unknown[];
  primary_papers?: unknown[];
  section_role?: string;
  paragraph_plan?: unknown[];
  review_claims?: unknown[];
  required_figures?: unknown[];
  figure_or_table_needs?: unknown[];
  evidence_readiness?: {
    status?: "ready" | "partial" | "insufficient" | "synthesis";
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

type AutoRoutingAdjustment = {
  source_section?: string;
  target_section?: string;
  paper_ids?: string[];
  method?: string;
  created_section?: boolean;
};

type PlanningPayload = {
  topic?: string;
  matrix_revision: number;
  blueprint_revision: number;
  matrix_artifact_id?: string;
  literature_matrix?: { rows?: MatrixPaper[] };
  selected_outline_md?: string;
  outline_current?: boolean;
  outline_options_md?: string;
  outline_selection?: Record<string, unknown>;
  reference_outline_candidates?: Array<Record<string, unknown> & { candidate_id?: string; source_name?: string }>;
  legacy_reference_outline_count?: number;
  section_blueprint?: {
    sections?: BlueprintSection[];
    resolved_outline_md?: string;
    auto_routing_adjustments?: AutoRoutingAdjustment[];
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
    complete: text("完整", "Complete"),
    partial: text("部分", "Partial"),
    limited: text("仅摘要", "Abstract only"),
    failed: text("失败", "Failed"),
    pending: text("待处理", "Pending"),
  } as Record<string, string>)[status] || status;
}

function factStatusClass(status: string): string {
  if (status === "complete") return "ok";
  if (status === "failed") return "danger";
  if (status === "pending") return "pending";
  return "warning";
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
  const enrichmentFailed = Boolean(enrichmentJob && ["failed", "cancelled", "interrupted"].includes(enrichmentJob.status));
  const hasRecoveryCheckpoint = Boolean(
    enrichmentJob?.result?.matrix_enrichment_checkpoint
    || enrichmentJob?.result?.section_checkpoint,
  );
  const factCounts = {
    complete: payload.matrix_enrichment?.counts?.complete || 0,
    partial: payload.matrix_enrichment?.counts?.partial || 0,
    limited: payload.matrix_enrichment?.counts?.limited || 0,
    pending: payload.matrix_enrichment?.counts?.pending || 0,
    failed: payload.matrix_enrichment?.counts?.failed || 0,
  };
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
  });
  const saveOutline = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/outline`, {
      method: "PUT",
      ...jsonBody({ revision: payload.matrix_revision, outline_style: String(payload.outline_selection?.outline_style || "custom"), outline_md: outlineDraft, scope_contract: scopeDraft }),
    }),
    onSuccess: refresh,
  });
  const uploadReference = useMutation({
    mutationFn: async (file: File) => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/reference-outlines`, {
      method: "POST",
      ...jsonBody({ revision: payload.matrix_revision, filename: file.name, content_base64: await fileBase64(file) }),
    }),
    onSuccess: refresh,
  });
  const enrichMatrix = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/matrix/enrichment/jobs`, {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
    }),
    onSuccess: refresh,
  });
  const recoverMatrix = useMutation({
    mutationFn: () => apiRequest<Job>(`/api/v1/jobs/${encodeURIComponent(enrichmentJob!.id)}/retry`, {
      method: "POST",
    }),
    onSuccess: refresh,
  });
  const visiblePapers = papers.filter((paper) => [paper.paper_id, paperLabels.get(paper.paper_id), displayText(paper.title), paper.keywords?.join(" "), paper.abstract].join(" ").toLowerCase().includes(filter.toLowerCase()));
  const selectedStyle = String(payload.outline_selection?.outline_style || "");
  const outlineReady = validateVisualOutline(parseOutlineMarkdown(outlineDraft)).ready;
  const facts = selected?.scientific_facts || [];
  const selectedFactStatus = String(selected?.fact_enrichment?.status || "pending");
  const emptyFactMessage = enrichmentActive
    ? text("正在从全文证据中提取本篇论文的科学事实。", "Scientific facts are being extracted from this paper's evidence.")
    : enrichmentFailed && hasRecoveryCheckpoint && selectedFactStatus === "pending"
      ? text("本篇事实已经完成提取，但尚未发布到 Matrix；请使用左侧的“恢复已有结果”。", "This paper was extracted but not published to the Matrix. Use Recover existing results on the left.")
      : selectedFactStatus === "pending"
        ? text("等待科学事实提取任务。", "Waiting for scientific fact extraction.")
        : selected?.fact_enrichment?.error || text("没有事实通过原文校验。", "No fact passed source validation.");

  return (
    <>
      <nav className="workspace-mode-tabs"><button type="button" className={mode === "reading" ? "active" : ""} onClick={() => setMode("reading")}>{text("文献Matrix", "Literature matrix")}</button><button type="button" className={mode === "outline" ? "active" : ""} onClick={() => setMode("outline")}>{text("大纲选择与上传", "Choose or upload outline")}</button></nav>
      {mode === "reading" ? (
        <div className="planning-grid">
          <section className="pane planning-list-pane">
            <div className="pane-head matrix-fact-head"><div><span className="step-label">{text("文献Matrix", "Literature matrix")}</span><h2>{papers.length} {text("篇论文", "papers")}</h2><p>{enrichmentActive ? text(`正在提取科学事实 ${enrichmentJob?.progress_current || 0}/${enrichmentJob?.progress_total || papers.length}`, `Extracting scientific facts ${enrichmentJob?.progress_current || 0}/${enrichmentJob?.progress_total || papers.length}`) : text("科学事实提取状态", "Scientific fact extraction status")}</p><div className="matrix-fact-counts"><span className="complete">{text("完整", "Complete")} {factCounts.complete}</span><span className="partial">{text("部分", "Partial")} {factCounts.partial}</span><span className="limited">{text("仅摘要", "Abstract only")} {factCounts.limited}</span><span className="pending">{text("待处理", "Pending")} {factCounts.pending}</span><span className="failed">{text("失败", "Failed")} {factCounts.failed}</span></div></div>{!enrichmentActive ? enrichmentFailed && enrichmentJob?.available_actions?.includes("retry") ? <button type="button" className="button button-secondary" disabled={recoverMatrix.isPending} onClick={() => recoverMatrix.mutate()}>{recoverMatrix.isPending ? text("恢复中…", "Recovering…") : hasRecoveryCheckpoint ? text("恢复已有结果", "Recover existing results") : text("重试提取", "Retry extraction")}</button> : <button type="button" className="button button-secondary" disabled={enrichMatrix.isPending} onClick={() => enrichMatrix.mutate()}>{enrichMatrix.isPending ? text("启动中…", "Starting…") : text("更新科学事实", "Refresh facts")}</button> : null}</div>
            {enrichmentFailed ? <div className="message message-error matrix-enrichment-error"><strong>{enrichmentJob?.error_code === "STATE_CONFLICT" && hasRecoveryCheckpoint ? text("事实已经提取，但尚未写入 Matrix。", "Facts were extracted but not published to the Matrix.") : text("科学事实任务未完成。", "The scientific fact task did not finish.")}</strong><p>{enrichmentJob?.error_code === "STATE_CONFLICT" && hasRecoveryCheckpoint ? text("Matrix状态在任务期间发生变化。点击“恢复已有结果”可复用检查点，不会重新调用模型。", "The Matrix state changed during the job. Recover the checkpoint without calling the model again.") : enrichmentJob?.error_message || text("请重试科学事实提取。", "Retry scientific fact extraction.")}</p></div> : null}
            {(enrichMatrix.error || recoverMatrix.error) ? <p className="message message-warning">{(enrichMatrix.error || recoverMatrix.error)?.message}</p> : null}
            <input className="pane-search" type="search" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={text("检索Matrix", "Search matrix")} />
            <div className="paper-list">{visiblePapers.map((paper) => { const status = String(paper.fact_enrichment?.status || "pending"); return <button type="button" key={paper.paper_id} title={text(`内部论文 ID：${paper.paper_id}；事实状态：${factStatusLabel(status, text)}`, `Internal paper ID: ${paper.paper_id}; fact status: ${factStatusLabel(status, text)}`)} className={paper.paper_id === selected?.paper_id ? "paper-row active" : "paper-row"} onClick={() => setSelectedId(paper.paper_id)}><span className="paper-row-main"><strong>{paperLabels.get(paper.paper_id) || paper.paper_id} · {displayText(paper.title)}</strong><small>{paper.authors?.join(", ")}</small></span><span className={`status-dot ${factStatusClass(status)}`} /></button>; })}</div>
          </section>
          <section className="pane planning-detail-pane">
            {selected ? <><div className="pane-head paper-title"><div><span className="step-label" title={text(`内部论文 ID：${selected.paper_id}`, `Internal paper ID: ${selected.paper_id}`)}>{paperLabels.get(selected.paper_id) || selected.paper_id}</span><h2>{displayText(selected.title)}</h2><p>{[selected.authors?.join(", "), selected.year, selected.journal, selected.doi].filter(Boolean).join(" · ")}</p></div><span className={`status-pill ${factStatusClass(selectedFactStatus)}`}>{factStatusLabel(selectedFactStatus, text)}</span></div><div className="planning-detail-content"><section className="reading-field"><h3>{text("摘要", "Abstract")}</h3><p>{displayText(selected.abstract) || text("没有摘要。", "No abstract available.")}</p></section><section className="matrix-fact-section"><h3>{text("可定位的科学事实", "Source-addressable scientific facts")}</h3>{selectedFactStatus === "limited" ? <p className="message message-warning">{text("当前只有摘要级证据，不能据此扩展实验条件、机理或详细定量结论。", "Only abstract-level evidence is available; do not extend it into detailed conditions, mechanisms, or quantitative claims.")}</p> : null}{selected?.fact_enrichment?.review_status === "needs_review" ? <p className="message message-warning">{text("部分自动提取事实置信度较低或字段不完整；可继续使用，但建议在关键论证前查看原文。", "Some extracted facts are low-confidence or incomplete. They remain usable within their support level, but inspect the source before key claims.")}</p> : null}{facts.length ? <div className="matrix-fact-list">{facts.map((fact) => { const ref = fact.evidence_refs?.[0]; const support = fact.support_level || (fact.epistemic_status === "abstract_level_report" ? "abstract_limited" : "direct"); return <article key={fact.fact_id || `${fact.field_id}-${ref?.chunk_id}`}><div><strong>{String(fact.field_id || "fact").replaceAll("_", " ")}</strong><span>{text(`支持：${support === "direct" ? "直接证据" : support === "abstract_limited" ? "仅摘要" : "仅上下文"}`, `Support: ${support.replaceAll("_", " ")}`)} · {ref?.page_start ? text(`第 ${ref.page_start} 页`, `Page ${ref.page_start}`) : fact.source_channel || fact.epistemic_status}</span></div><p>{fact.value}</p><details><summary>{text("查看原文依据与证据上限", "View source support and evidence ceiling")}</summary><blockquote>{fact.support_excerpt}</blockquote><small>{fact.evidence_ceiling}</small></details></article>; })}</div> : <p className="muted">{emptyFactMessage}</p>}</section><details className="advanced-panel matrix-reading-advanced"><summary>{text("全文阅读笔记与图像信息（可选）", "Full-text notes and figure data (optional)")}</summary><div className="advanced-panel-body"><section className="reading-field"><h3>{text("全文阅读笔记", "Full-text reading notes")}</h3><textarea rows={14} value={note} onChange={(event) => setNote(event.target.value)} placeholder={text("转化、条件、证据、范围、限制与综述相关性", "Transformation, conditions, evidence, scope, limitations, and relevance")} /></section><label className="check-label"><input type="checkbox" checked={complete} onChange={(event) => setComplete(event.target.checked)} />{text("已完成该论文全文阅读", "Full-text reading completed")}</label><button className="button button-primary" type="button" disabled={saveReading.isPending} onClick={() => saveReading.mutate()}>{saveReading.isPending ? text("保存中…", "Saving…") : text("保存阅读笔记", "Save reading notes")}</button>{saveReading.error ? <p className="message message-error">{saveReading.error.message}</p> : null}<details className="figure-data"><summary>{text("最相关图像信息", "Most relevant figure")}</summary><pre>{JSON.stringify(selected.most_relevant_figure || {}, null, 2)}</pre></details></div></details></div></> : <div className="empty-state">{text("Discovery确认后会在这里显示Matrix。", "The matrix appears here after Discovery is confirmed.")}</div>}
          </section>
        </div>
      ) : (
        <section className="outline-workspace-react">
          <div className="outline-hero"><div><span className="step-label">{text("步骤 1 · 综述结构", "Step 1 · Review structure")}</span><h2>{text("选择综述组织逻辑", "Choose the review structure")}</h2><p>{text("只借鉴参考综述的组织方式，不复制其主题标题和具体内容。", "Reuse only the organizational style of a reference review, not its topic headings or content.")}</p></div><span className={selectedStyle ? "badge" : "badge pending"}>{selectedStyle ? text(`当前：${selectedStyle}`, `Current: ${selectedStyle}`) : text("尚未选择", "Not selected")}</span></div>
          <div className="outline-card-grid">{outlineStyles.map((style) => <article key={style.id} className={selectedStyle === style.id ? "outline-card current" : "outline-card"}><span>{style.icon}</span><h3>{text(style.titleZh, style.titleEn)}</h3><p>{text(style.descriptionZh, style.descriptionEn)}</p><button className="button button-secondary" type="button" disabled={chooseOutline.isPending || selectedStyle === style.id} onClick={() => chooseOutline.mutate(style.id)}>{selectedStyle === style.id ? text("当前选择", "Current selection") : text("使用此结构", "Use this structure")}</button></article>)}</div>
          <details className="advanced-panel planning-reference-advanced">
            <summary>{text("上传参考综述以学习组织方式（可选）", "Upload a reference review for organization only (optional)")}</summary>
            <div className="advanced-panel-body"><section className="reference-upload"><div><h3>{text("上传综述，仅学习格式与写法", "Upload a review to learn format only")}</h3><p>{text("支持PDF、DOCX、Markdown或TXT。系统分两步处理：先提取层级、节奏和写作方式，再只根据当前主题与Matrix生成全新标题；不会复制、翻译或改写上传综述的标题和内容。", "Supports PDF, DOCX, Markdown, or TXT. The system first extracts hierarchy, pacing, and writing conventions, then generates new headings only from the current topic and Matrix. Uploaded headings and content are never copied, translated, or paraphrased.")}</p></div><label className="button button-secondary file-button">{uploadReference.isPending ? text("正在分析格式…", "Analyzing format…") : text("选择参考综述", "Choose reference review")}<input type="file" accept=".pdf,.docx,.md,.txt" disabled={uploadReference.isPending} onChange={(event) => { const file = event.target.files?.[0]; event.currentTarget.value = ""; if (file) uploadReference.mutate(file); }} /></label></section>
            {uploadReference.error ? <p className="message message-error">{uploadReference.error.message}</p> : null}
            {payload.legacy_reference_outline_count ? <p className="message message-warning">{text(`已隐藏 ${payload.legacy_reference_outline_count} 个旧版参考大纲，因为它们没有通过“只学格式”的内容隔离校验；如需使用，请重新上传原参考综述。`, `${payload.legacy_reference_outline_count} legacy reference outlines were hidden because they did not pass format-only content isolation. Upload the source review again to use it safely.`)}</p> : null}
            {payload.reference_outline_candidates?.length ? <div className="reference-candidates">{payload.reference_outline_candidates.map((candidate) => { const style = `reference:${candidate.candidate_id}`; return <button key={String(candidate.candidate_id)} className={selectedStyle === style ? "active" : ""} type="button" disabled={chooseOutline.isPending || selectedStyle === style} onClick={() => chooseOutline.mutate(style)}><strong>{String(candidate.source_name || candidate.candidate_id)}</strong><small>{text("仅学习格式 · 内容来自当前Matrix", "Format only · content from current Matrix")}</small></button>; })}</div> : null}</div>
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
          <section className="outline-editor-card"><div className="section-heading"><div><h2>{text("新手大纲编辑器", "Beginner outline editor")}</h2><p>{text("大标题和小标题必须对应当前检索主题；新手模式会自动生成系统需要的格式。", "Every heading and subheading must match the current discovery topic; beginner mode generates the required format automatically.")}</p></div><button className="button button-primary" type="button" disabled={!outlineReady || saveOutline.isPending} onClick={() => saveOutline.mutate()}>{saveOutline.isPending ? text("保存中…", "Saving…") : text("保存大纲", "Save outline")}</button></div><OutlineBuilder value={outlineDraft} papers={papers} onChange={setOutlineDraft} />{!outlineReady && outlineDraft.trim() ? <p className="message message-warning">{text("请补全每个章节的标题，并至少选择一篇Matrix论文。", "Complete every section title and select at least one Matrix paper per section.")}</p> : null}{saveOutline.error ? <p className="message message-error">{saveOutline.error.message}</p> : null}</section>
          <details className="outline-options"><summary>{text("查看系统生成的候选大纲", "View system-generated outline candidates")}</summary><pre>{payload.outline_options_md || text("暂无候选大纲。", "No candidate outlines yet.")}</pre></details>
        </section>
      )}
    </>
  );
}

function BlueprintWorkspace({ payload }: { payload: PlanningPayload }) {
  const { text } = useUiText();
  const sections = payload.section_blueprint?.sections || [];
  const [selectedId, setSelectedId] = useState(String(sections[0]?.section_id || ""));
  const [advancedDetail, setAdvancedDetail] = useState<"raw" | "plan" | "outline">("raw");
  const section = sections.find((item) => String(item.section_id) === selectedId) || sections[0];
  const issues = [...(payload.scope_diagnostics?.issues || []), ...(payload.taxonomy_diagnostics?.issues || [])];
  const adjustments = payload.section_blueprint?.auto_routing_adjustments || [];
  const resolvedOutline = payload.section_blueprint?.resolved_outline_md || payload.selected_outline_md;
  const adjustedPaperCount = new Set(adjustments.flatMap((item) => item.paper_ids || [])).size;
  const adjustedTargets = [...new Set(adjustments.map((item) => item.target_section).filter((target): target is string => Boolean(target)))];
  return (
    <div className="blueprint-grid-react">
      <section className="pane blueprint-section-list"><div className="pane-head"><div><span className="step-label">{text("Blueprint章节", "Blueprint sections")}</span><h2>{sections.length} {text("个章节", "sections")}</h2></div></div><div className="keyword-list">{sections.map((item) => { const papers = item.primary_papers || item.major_papers || item.assigned_papers || []; const claims = item.review_claims || item.paragraph_plan || []; const role = String(item.section_role || "body"); const synthesisOnly = role === "introduction" || role === "conclusion"; const missing = !claims.length || (!synthesisOnly && !papers.length); const readiness = item.evidence_readiness?.status; const readinessLabel = missing || readiness === "insufficient" ? text("需要检查", "Needs review") : readiness === "partial" ? text("部分证据", "Partial evidence") : readiness === "synthesis" || synthesisOnly ? text("综合章节", "Synthesis section") : text("就绪", "Ready"); return <button key={String(item.section_id)} type="button" className={item === section ? "active" : ""} onClick={() => setSelectedId(String(item.section_id))}><strong>{String(item.section_id || "")} · {String(item.title || text("无标题", "Untitled"))}</strong><small>{papers.length} {text("篇主要论文", "primary papers")} · {claims.length} {text("个论证计划", "argument plans")} · {readinessLabel}</small></button>; })}</div></section>
      <section className="pane blueprint-detail-react"><div className="pane-head blueprint-detail-head"><div><span className="step-label">{text("Blueprint摘要", "Blueprint summary")}</span><h2>{section?.title || "Blueprint"}</h2></div></div>{issues.length ? <div className="planning-diagnostics">{issues.map((issue) => <p className={issue.severity === "planning_blocker" ? "message message-error" : "message message-warning"} key={`${issue.rule_id}-${issue.message}`}>{issue.message}</p>)}</div> : <div className="blueprint-health-row"><span className="blueprint-health-status"><i />{text("自动检查通过", "Automatic checks passed")}</span>{adjustments.length ? <details className="blueprint-routing-details"><summary><strong>{text(`${adjustedPaperCount} 篇论文路由已调整`, `${adjustedPaperCount} paper routes adjusted`)}</strong><span>{text("查看记录", "View log")}</span></summary><div className="blueprint-routing-detail-body"><p>{text("系统依据可定位的科学事实完成调整，无需逐项确认。", "Routes were adjusted from source-addressable scientific facts; no separate confirmation is required.")}</p><div>{adjustedTargets.map((target) => <span key={target}>{target}</span>)}</div></div></details> : <span className="blueprint-routing-none">{text("无需调整论文路由", "No route adjustments needed")}</span>}</div>}
        {section ? <div className="blueprint-summary-grid"><section><h3>{text("核心论点", "Core argument")}</h3><p>{displayText(section.section_thesis || section.section_goal) || "—"}</p></section><section><h3>{text("主要论文", "Primary papers")}</h3><strong>{(section.primary_papers || section.major_papers || section.assigned_papers || []).length}</strong></section><section><h3>{text("论证计划", "Argument plan")}</h3><strong>{(section.review_claims || section.paragraph_plan || []).length}</strong></section><section><h3>{text("证据状态", "Evidence status")}</h3><strong>{section.evidence_readiness?.status === "partial" ? text("部分论文仅可作背景", "Some papers are context only") : section.evidence_readiness?.status === "insufficient" ? text("证据不足", "Insufficient evidence") : section.evidence_readiness?.status === "synthesis" ? text("综合正文证据", "Synthesizes body evidence") : text("可进入写作", "Ready for writing")}</strong></section></div> : <div className="empty-state">{text("请先生成Blueprint。", "Generate a blueprint first.")}</div>}
        <details className="advanced-panel blueprint-advanced-detail"><summary>{text("查看完整 Blueprint 与生成依据", "View full blueprint and generation inputs")}</summary><div className="advanced-panel-body"><nav className="advanced-tab-list"><button type="button" className={advancedDetail === "raw" ? "active" : ""} onClick={() => setAdvancedDetail("raw")}>{text("完整字段", "Full fields")}</button><button type="button" className={advancedDetail === "plan" ? "active" : ""} onClick={() => setAdvancedDetail("plan")}>{text("写作计划", "Writing plan")}</button><button type="button" className={advancedDetail === "outline" ? "active" : ""} onClick={() => setAdvancedDetail("outline")}>{text("选定大纲", "Selected outline")}</button></nav>{advancedDetail === "raw" ? <div className="blueprint-json-fields">{section ? Object.entries(section).filter(([, value]) => value !== null && value !== "" && (!Array.isArray(value) || value.length)).map(([key, value]) => <section key={key}><h3>{key.replaceAll("_", " ")}</h3><pre>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></section>) : null}</div> : <pre className="markdown-preview">{advancedDetail === "plan" ? payload.section_writing_plan_md : resolvedOutline}</pre>}</div></details>
      </section>
    </div>
  );
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
    refetchInterval: (query) => query.state.data?.matrix_enrichment?.jobs?.some((job) => jobIsActive(job.status)) ? 1500 : false,
  });
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["planning", project?.project_id || ""] });
    return planning.refetch();
  };
  const generateBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint`, { method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision }) }),
    onSuccess: async () => {
      await refresh();
      const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next);
    },
  });
  const confirmBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/blueprint/confirm`, { method: "POST", ...jsonBody({ revision: planning.data!.blueprint_revision }) }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      navigate(`/sections?project=${encodeURIComponent(project!.project_id)}`);
    },
  });
  const continueLimitedMode = useMutation({
    mutationFn: () => apiRequest(
      `/api/v1/projects/${encodeURIComponent(project!.project_id)}/planning/matrix/enrichment/limited-mode`,
      { method: "POST", ...jsonBody({ revision: planning.data!.matrix_revision }) },
    ),
    onSuccess: refresh,
  });
  const planningBlocked = planning.data?.scope_diagnostics?.can_confirm === false || planning.data?.taxonomy_diagnostics?.can_confirm === false;
  const matrixEnrichmentRunning = Boolean(
    planning.data?.matrix_enrichment?.jobs?.some((job) => jobIsActive(job.status))
  );
  const matrixEnrichmentBlocked = Boolean(
    planning.data?.matrix_enrichment?.planning_blocked
  );
  const matrixEnrichmentPublishFailed = Boolean(
    planning.data?.matrix_enrichment?.failed_publish_with_pending_rows
  );
  const allMatrixFactsFailed = Boolean(
    planning.data?.matrix_enrichment?.all_failed
    && !planning.data?.matrix_enrichment?.limited_mode_confirmed
  );
  const autoRepairableRouting = Boolean(
    planning.data?.blueprint_current
    && planning.data?.outline_selection?.manually_edited !== true
    && planning.data?.taxonomy_diagnostics?.issues?.some((issue) => ["taxonomy.catch_all_body_section", "taxonomy.dominant_boundary_section"].includes(String(issue.rule_id)))
  );

  return (
    <main className="workspace page-container workspace-page">
      <div className="workspace-heading"><div><p className="eyebrow">{text("阶段 3 · 分析与规划", "Stage 3 · Analysis and planning")}</p><h1>{text("Matrix与综述大纲", "Matrix and review outline")}</h1><p className="muted">{text("从确认文献形成Matrix，选择大纲逻辑，再生成可审核的章节Blueprint。", "Build the matrix from confirmed papers, choose an outline logic, then generate a reviewable section blueprint.")}</p></div><ProjectSelector /></div>
      <nav className="workspace-step-tabs"><button type="button" className={tab === "matrix" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.set("tab", "matrix"); setSearchParams(next); }}>1 {text("文献Matrix与大纲", "Literature matrix and outline")}</button><button type="button" className={tab === "blueprint" ? "active" : ""} onClick={() => { const next = new URLSearchParams(searchParams); next.set("tab", "blueprint"); setSearchParams(next); }}>2 {text("章节Blueprint", "Section blueprint")}</button></nav>
      {planning.isPending ? <div className="empty-state">{text("正在加载Planning产物…", "Loading planning artifacts…")}</div> : null}
      {planning.error ? <ErrorState error={planning.error} onRetry={() => planning.refetch()} /> : null}
      {planning.data && matrixEnrichmentPublishFailed ? <section className="message message-warning planning-limited-mode"><div><strong>{text("科学事实已经提取，但尚未写入 Matrix", "Scientific facts were extracted but not published")}</strong><p>{text("请回到“文献 Matrix”点击“恢复已有结果”。系统会复用已完成的检查点，不会重新调用模型。", "Return to the Literature Matrix and choose Recover existing results. The completed checkpoint will be reused without another model call.")}</p></div></section> : null}
      {planning.data && allMatrixFactsFailed ? <section className="message message-warning planning-limited-mode"><div><strong>{text("所有论文的科学事实自动提取均未成功", "Scientific fact extraction failed for every paper")}</strong><p>{text("系统没有把标题或摘要伪装成全文事实。你可以在 Matrix 中重试提取；若确认接受信息较弱的结果，可主动以有限模式继续。", "Titles and abstracts were not presented as full-text facts. Retry extraction in Matrix, or explicitly continue in limited mode if you accept the weaker evidence basis.")}</p></div><button className="button button-secondary" type="button" disabled={continueLimitedMode.isPending} onClick={() => continueLimitedMode.mutate()}>{continueLimitedMode.isPending ? text("正在保存…", "Saving…") : text("以有限模式继续", "Continue in limited mode")}</button></section> : null}
      {planning.data && project ? <>{!planning.data.outline_current && planning.data.selected_outline_md?.trim() ? <p className="message message-warning">{text("当前仍显示旧大纲供参考，但它不属于新的 Matrix。请在“大纲选择与上传”中重新选择或保存大纲后再生成 Blueprint。", "The previous outline remains visible for reference, but it does not belong to the new matrix. Choose or save an outline again before generating the blueprint.")}</p> : null}{tab === "blueprint" && !planning.data.blueprint_current && planning.data.section_blueprint?.sections?.length ? <p className="message message-warning">{text("当前仍显示旧 Blueprint 供核对，但它已经过期，不能确认进入章节阶段。请先使用当前 Matrix 和大纲重新生成。", "The previous blueprint remains visible for comparison, but it is stale and cannot be confirmed. Regenerate it from the current matrix and outline first.")}</p> : null}{tab === "matrix" ? <MatrixWorkspace payload={planning.data} projectId={project.project_id} refresh={refresh} /> : <BlueprintWorkspace payload={planning.data} />}<div className="stage-action-bar"><div><strong>{tab === "matrix" ? text("生成Blueprint", "Generate blueprint") : autoRepairableRouting ? text("自动调整大纲", "Auto-adjust outline") : text("确认Blueprint", "Confirm blueprint")}</strong><p>{tab === "matrix" ? matrixEnrichmentRunning ? text("正在自动提取可定位的科学事实，完成后即可生成Blueprint。", "Source-addressable scientific facts are being extracted automatically; generate the blueprint when this finishes.") : matrixEnrichmentBlocked ? text("科学事实结果尚未就绪，请先在文献 Matrix 中恢复或重试。", "Scientific facts are not ready. Recover or retry them in the Literature Matrix first.") : text("使用当前Matrix与已保存大纲生成章节任务；系统生成的未分类项会自动重新路由。", "Generate section tasks from the current matrix and saved outline; system-generated unclassified items are rerouted automatically.") : autoRepairableRouting ? text("系统将根据当前分类规则和论文内容重新分配未分类论文，然后更新 Blueprint。", "The system will reroute unclassified papers from the current taxonomy and paper evidence, then update the Blueprint.") : planningBlocked ? text("先在当前Planning页面修复Scope或分类阻断项；不需要增加新的确认步骤。", "Resolve Scope or taxonomy blockers on this Planning page; no additional confirmation step is required.") : text("确认章节论点、论文分配、综合需求和图表需求后进入章节写作。", "Review section claims, paper assignments, synthesis requirements, and figure needs before section writing.")}</p></div>{tab === "matrix" ? <button className="button button-primary" type="button" disabled={generateBlueprint.isPending || !planning.data.outline_current || matrixEnrichmentRunning || matrixEnrichmentBlocked} onClick={() => generateBlueprint.mutate()}>{generateBlueprint.isPending ? text("生成中…", "Generating…") : text("生成Blueprint", "Generate blueprint")}</button> : autoRepairableRouting ? <button className="button button-primary" type="button" disabled={generateBlueprint.isPending} onClick={() => generateBlueprint.mutate()}>{generateBlueprint.isPending ? text("自动调整中…", "Auto-adjusting…") : text("自动调整并重新生成Blueprint", "Auto-adjust and regenerate blueprint")}</button> : <button className="button button-primary" type="button" disabled={confirmBlueprint.isPending || !planning.data.blueprint_current || planningBlocked} onClick={() => confirmBlueprint.mutate()}>{confirmBlueprint.isPending ? text("确认中…", "Confirming…") : text("确认并进入章节", "Confirm and enter sections")}</button>}{(generateBlueprint.error || confirmBlueprint.error) ? <span className="message message-error">{(generateBlueprint.error || confirmBlueprint.error)?.message}</span> : null}</div></> : null}
    </main>
  );
}
