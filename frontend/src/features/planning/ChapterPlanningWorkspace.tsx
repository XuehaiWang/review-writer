import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { queryKeys } from "../../api/queries";
import { LocalizedError } from "../../components/LocalizedError";
import { RegenerateConfirmDialog } from "../../components/RegenerateConfirmDialog";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels } from "./OutlineBuilder";
import { displayText } from "./planningText";
import type { BlueprintSection, PlanningPayload } from "./PlanningPage";

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

function ChapterPlanningActions({ payload, generating, confirming, error, onGenerate, onConfirm, refresh }: {
  payload: PlanningPayload;
  generating: boolean;
  confirming: boolean;
  error?: React.ReactNode;
  onGenerate: () => void;
  onConfirm: () => void;
  refresh: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const [regenerationOpen, setRegenerationOpen] = useState(false);
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
  const outlineReady = Boolean(payload.outline_ready_for_chapter_planning ?? (
    payload.outline_current && payload.selected_outline_md?.trim() && payload.outline_selection?.outline_complete !== false
  ));
  const complete = Boolean(outlineReady && payload.blueprint_current && academic?.status === "completed");
  const incomplete = academic?.status === "incomplete";
  const busy = generating || Boolean(activeJob);
  const canGenerate = !busy && !confirming && outlineReady;
  const canResume = !complete && outlineReady && (incomplete || latestJob?.available_actions.includes("retry"));
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
    : !outlineReady ? text("请先在“选择大纲”中选择或保存一份完整大纲。", "Choose or save a complete outline first.")
    : sectionCount && !payload.blueprint_current ? text("当前显示的是旧章节规划。请根据当前文献和大纲重新生成后再确认。", "The displayed chapter plan is outdated. Regenerate it from the current papers and outline before confirming.")
    : incomplete ? text(`未完成章节：${academic?.incomplete_sections?.join("、") || "待生成"}。继续生成会复用已完成步骤。`, `Unfinished sections: ${academic?.incomplete_sections?.join(", ") || "pending"}. Continue to resume pending work.`)
    : complete && payload.blueprint_approved ? text("当前规划已确认。正文生成仍需单独启动；如需调整，可在这里生成新候选。", "This plan is confirmed. Drafting starts separately; generate a new candidate here if revisions are needed.")
    : complete ? text("确认后应用暂定论点与论文安排；证据缺口留待正文阶段处理。", "Confirmation applies provisional arguments and paper assignments; evidence gaps are addressed during drafting.")
    : text("复用已提取且有效的事实，只对缺失或失效的证据补充分析，再安排章节写作目标与检索方向。", "Reuse valid extracted facts and analyze missing or outdated evidence before planning chapter objectives and retrieval.");
  const startGeneration = () => {
    if (sectionCount && !canResume) setRegenerationOpen(true);
    else onGenerate();
  };
  return <><section className="stage-action-bar" aria-live="polite">
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
      {error || cancel.error ? <p className="message message-error" role="alert">{error || <LocalizedError error={cancel.error} />}</p> : null}
    </div>
    {activeJob ? <button className="button button-secondary" type="button" disabled={cancel.isPending || activeJob.cancellation_requested} onClick={() => cancel.mutate()}>{activeJob.cancellation_requested ? text("正在停止…", "Stopping…") : text("停止生成", "Stop generation")}</button>
      : <button className={`button ${sectionCount ? "button-secondary" : "button-primary"}`} type="button" disabled={!canGenerate} onClick={startGeneration}>
        {generating ? text("正在生成…", "Generating…") : canResume ? text("继续生成", "Continue generation") : sectionCount ? text("重新生成", "Regenerate") : text("生成章节规划", "Generate chapter plan")}
      </button>}
    {sectionCount && !payload.blueprint_approved ? <button className="button button-primary" type="button" disabled={confirming || busy || !complete} onClick={onConfirm}>{confirming ? text("确认中…", "Confirming…") : text("确认章节规划", "Confirm chapter plan")}</button> : null}
  </section>
    <RegenerateConfirmDialog open={regenerationOpen} title={text("重新规划章节？", "Regenerate the chapter plan?")}
      description={text("系统将按当前大纲和文献生成新的章节规划候选；现有规划不会立即删除。", "A new chapter-plan candidate will be generated from the current outline and papers. The existing plan is not deleted immediately.")}
      consequence={text("若之后确认新规划，现有章节正文及后续图像、初稿、终稿可能需要更新。已有人工修改可能不再出现在当前版本；本次生成会产生模型费用。", "If you later confirm the new plan, section prose and downstream figures, Draft, and Final may need updating. Manual edits may no longer appear in the current version. Model usage may incur charges.")}
      confirmLabel={text("确认重新规划", "Regenerate chapter plan")}
      onCancel={() => setRegenerationOpen(false)} onConfirm={() => { setRegenerationOpen(false); onGenerate(); }} />
  </>;
}

export function ChapterPlanningWorkspace({ payload, projectId, refresh }: {
  payload: PlanningPayload;
  projectId: string;
  refresh: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const generateBlueprint = useMutation({
    mutationFn: () => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/blueprint/jobs`, {
      method: "POST", ...jsonBody({ revision: payload.blueprint_revision }),
      headers: { "Idempotency-Key": newIdempotencyKey() },
    }),
    onSuccess: refresh,
  });
  const confirmBlueprint = useMutation({
    mutationFn: async () => {
      try {
        return await apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/blueprint/confirm`, {
          method: "POST", ...jsonBody({ revision: payload.blueprint_revision, artifact_id: payload.blueprint_artifact_id }),
        });
      } catch (error) {
        // A lost response may follow a successful confirmation. Read state; never resubmit the POST.
        const latest = await apiRequest<PlanningPayload>(`/api/v1/projects/${encodeURIComponent(projectId)}/planning`).catch(() => null);
        if (latest?.blueprint_approved) return { status: "approved" };
        throw error;
      }
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      queryClient.removeQueries({ queryKey: ["sections", projectId] });
      await refresh();
    },
  });
  const restoreBlueprint = useMutation({
    mutationFn: (artifactId: string) => apiRequest(`/api/v1/projects/${encodeURIComponent(projectId)}/planning/blueprint/restore`, {
      method: "POST", ...jsonBody({ revision: payload.blueprint_revision, artifact_id: artifactId }),
    }),
    onSuccess: refresh,
  });
  const outlineReady = Boolean(payload.outline_ready_for_chapter_planning ?? (
    payload.outline_current && payload.selected_outline_md?.trim() && payload.outline_selection?.outline_complete !== false
  ));
  return <>
    {!outlineReady ? <p className="message message-info">{text("请先到阶段 03 选择并保存完整大纲，再生成章节规划。", "Choose and save a complete outline in stage 03 before generating a chapter plan.")} <Link to={`/planning?view=outline&project=${encodeURIComponent(projectId)}`}>{text("前往选择大纲", "Choose outline")}</Link></p> : null}
    <ChapterPlanningActions payload={payload} generating={generateBlueprint.isPending} confirming={confirmBlueprint.isPending}
      error={(generateBlueprint.error || confirmBlueprint.error || restoreBlueprint.error) ? <LocalizedError error={generateBlueprint.error || confirmBlueprint.error || restoreBlueprint.error} /> : undefined}
      onGenerate={() => generateBlueprint.mutate()} onConfirm={() => confirmBlueprint.mutate()} refresh={refresh} />
    {payload.section_blueprint?.sections?.length ? <BlueprintWorkspace payload={payload} restoring={restoreBlueprint.isPending} onRestorePrevious={(artifactId) => restoreBlueprint.mutate(artifactId)} /> : null}
  </>;
}
