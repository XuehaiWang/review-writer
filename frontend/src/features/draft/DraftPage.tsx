import { LocalizedError } from "../../components/LocalizedError";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { meQuery } from "../../api/queries";
import { ErrorState } from "../../components/ErrorState";
import { ProjectSelector, useSelectedProject } from "../../components/ProjectSelector";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { approveCurrentDraft } from "./approveCurrentDraft";
import { DraftApprovalPanel, type ApprovalQuality } from "./DraftApprovalPanel";
import { DraftReader } from "./DraftReader";
import { CandidateBrowser } from "./CandidateBrowser";
import { type DialogueCandidate, type DialogueParagraph } from "./ParagraphDialogue";
import { SectionDialogue, type DialogueSection } from "./SectionDialogue";
import { hasDraftScratch } from "./useDraftScratch";
import { DraftCompositionPanel, DraftSynthesisStatus, type SynthesisJob } from "./DraftCompositionPanel";
import { DraftManuscriptFields } from "./DraftManuscriptFields";
import { EditableManuscript } from "./EditableManuscript";

type Batch = { id: string; status: string; progress_current: number; progress_total: number; error_message?: string; result?: { coherence_plan?: { status: string }; paragraph_results?: Record<string, { paragraph_id: string; status: string; reason?: string }> } };
type DraftPayload = { revision: number; draft_artifact_id: string; first_draft_md: string; manuscript_preview_md?: string; paragraphs: DialogueParagraph[];
  synthesis_job?: SynthesisJob;
  synthesis_stale?: boolean;
  manuscript_fields?: { title: string; keywords: string[] };
  legacy_manuscript_fields?: { title: string; keywords: string[] } | null;
  active_feedback_job_id?: string;
  sections: DialogueSection[]; section_task_states: Record<string, { id: string; status: string }>;
  paragraph_task_states: Record<string, { id: string; status: string; batch: boolean }>;
  quality: ApprovalQuality; quality_artifact_id: string; draft_approval_current: boolean;
  rewrite_candidates: (DialogueCandidate & { revision_mode?: string; base_text_sha256?: string })[]; dialogue_batch_job?: Batch;
  freshness: { upstream_stale: boolean; editing_blocked?: boolean }; versions: { artifact_id: string; current: boolean; operation: string; created_at: string }[] };

export function DraftPage() {
  const { text } = useUiText();
  const { selected: project } = useSelectedProject();
  const me = useQuery(meQuery);
  return <main className="workspace page-container workspace-page draft-page">
    <div className="workspace-heading">
      <div><p className="eyebrow">{text("阶段 6 · 初稿", "Stage 6 · Draft")}</p>
        <h1>{text("初稿修订", "Draft revision")}</h1>
        <p className="muted">{text("阅读正文，按需优化或讨论章节，采用修改后确认进入终稿。", "Read, revise through suggestions or chapter dialogue, then approve.")}</p>
      </div><ProjectSelector />
    </div>
    {project && me.data ? <DraftWorkspace key={me.data.user_id + project.project_id} projectId={project.project_id} userId={me.data.user_id} /> : null}
  </main>;
}

function DraftWorkspace({ projectId, userId }: { projectId: string; userId: string }) {
  const { text } = useUiText();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const requestedTab = params.get("tab");
  const tab = ["batch", "dialogue", "preview", "approval", "history"].includes(requestedTab || "")
    ? requestedTab! : params.get("paragraph") ? "dialogue" : "preview";
  const selected = params.get("paragraph") || "";
  const panelRef = useRef<HTMLElement>(null);
  const dirtyEdits = useRef(new Set<string>());
  const [dirtyParagraphKeys, setDirtyParagraphKeys] = useState<ReadonlySet<string>>(() => new Set());
  const recordEdit = useCallback((key: string, dirty: boolean) => {
    if (dirtyEdits.current.has(key) === dirty) return;
    if (dirty) dirtyEdits.current.add(key); else dirtyEdits.current.delete(key);
    setDirtyParagraphKeys(new Set(dirtyEdits.current));
  }, []);
  const recordFields = useCallback((dirty: boolean) => recordEdit("manuscript-fields", dirty), [recordEdit]);
  const requireSaved = () => {
    if (dirtyEdits.current.size) throw new Error(text("请先保存或撤销完整正文中的修改。", "Save or undo manuscript edits first."));
  };
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirtyEdits.current.size) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);
  const switchTab = (id: string, paragraphKey?: string) => {
    if (dirtyEdits.current.size && id !== tab && !window.confirm(text("有未保存修改，离开后仍会暂存在本机。确定切换？", "Unsaved edits will stay in this browser. Switch views?"))) return;
    const next = new URLSearchParams(params);
    next.set("tab", id);
    if (id !== "dialogue") setDialogueExpanded(false);
    if (paragraphKey !== undefined) next.set("paragraph", paragraphKey);
    setParams(next);
    panelRef.current?.scrollIntoView?.({ block: "start" });
  };
  const autoAssembled = useRef(false);
  const [captionOpen, setCaptionOpen] = useState(0);
  const [dialogueExpanded, setDialogueExpanded] = useState(false);
  const [checked, setChecked] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [versionLimit, setVersionLimit] = useState(20);
  const [viewed, setViewed] = useState<{ id: string; content: string } | null>(null);
  const base = "/api/v1/projects/" + encodeURIComponent(projectId) + "/draft";
  const draft = useQuery({ queryKey: ["draft", projectId], queryFn: () => apiRequest<DraftPayload>(base),
    refetchInterval: query => query.state.data?.active_feedback_job_id || jobIsActive(query.state.data?.synthesis_job?.status) || jobIsActive(query.state.data?.dialogue_batch_job?.status) ? 3000 : false, refetchIntervalInBackground: true });
  const data = draft.data;
  const refresh = async () => { await Promise.all([client.invalidateQueries({ queryKey: ["draft", projectId] }), client.invalidateQueries({ queryKey: ["draft-dialogue", projectId] })]); };
  const assemble = useMutation({ mutationFn: async () => { requireSaved(); return apiRequest<{ synthesis_warning?: string }>(base + "/assemble", { method: "POST" }); },
    onSuccess: async result => { if (result.synthesis_warning) setNotice(result.synthesis_warning); await refresh(); } });
  useEffect(() => {
    if (!data || data.draft_artifact_id || data.freshness.upstream_stale || autoAssembled.current) return;
    autoAssembled.current = true;
    assemble.mutate();
  }, [data]);
  const batch = useMutation({ mutationFn: () => apiRequest(base + "/dialogue-batch", { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() } }), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => apiRequest("/api/v1/jobs/" + data!.dialogue_batch_job!.id + "/cancel", { method: "POST" }), onSuccess: refresh });
  const retry = useMutation({ mutationFn: () => {
    const job = data!.dialogue_batch_job!;
    const path = job.status === "succeeded" ? base + "/dialogue-batch/" + job.id + "/resume" : "/api/v1/jobs/" + job.id + "/retry";
    return apiRequest(path, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() } });
  }, onSuccess: refresh });
  const approve = useMutation({ mutationFn: async () => { requireSaved(); return approveCurrentDraft(projectId, data!, text("已保存正文发生变化，请刷新后确认。", "Saved text changed. Refresh before approving.")); }, onSuccess: () => { void refresh(); navigate("/final?project=" + encodeURIComponent(projectId)); } });
  const restore = useMutation({ mutationFn: async (id: string) => { requireSaved(); return apiRequest(base + "/restore", { method: "POST", ...jsonBody({ revision: data!.revision, artifact_id: id }) }); }, onSuccess: async () => { setViewed(null); await refresh(); } });
  const inspect = useMutation({ mutationFn: async (id: string) => ({ id, content: await apiRequest<string>("/api/v1/artifacts/" + id + "/content") }), onSuccess: setViewed });
  const decision = useMutation({ mutationFn: async ({ ids, action }: { ids: string[]; action: "accept" | "reject" }) => {
    if (action === "accept") requireSaved();
    const failures: string[] = [];
    for (const id of ids) { try {
      const candidate = data?.rewrite_candidates.find(c => c.candidate_id === id);
      const affected = candidate?.revision_mode === "section_synthesis" ? data?.sections.find(s => s.section_id === candidate.paragraph_key)?.paragraphs || [] : candidate ? [candidate] : [];
      if (action === "accept" && affected.some(p => hasDraftScratch(userId + ":" + projectId + ":" + p.paragraph_key + ":manual"))) throw new Error(text("该部分有未保存的手动编辑，请先处理。", "Resolve unsaved manual edits in this section first."));
      await apiRequest(base + "/dialogue-candidates/" + id + "/" + action, { method: "POST",
        headers: candidate?.base_text_sha256 ? { "If-Match": candidate.base_text_sha256 } : undefined });
    } catch (error) { failures.push(id + ": " + (error as Error).message); } }
    return { failures, completed: ids.length - failures.length };
  }, onSuccess: async (result, { action }) => { setChecked([]); setNotice(
    (action === "accept" ? text("已保存修改：", "Changes saved: ") : text("已放弃候选：", "Candidates discarded: "))
    + result.completed + (result.failures.length ? " · " + result.failures.join("; ") : "")); await refresh(); } });
  const error = assemble.error || batch.error || cancel.error || retry.error || restore.error || inspect.error || decision.error;
  if (draft.error) return <ErrorState error={draft.error} onRetry={() => void draft.refetch()} />;
  if (!data) return <p role="status">{text("正在加载初稿…", "Loading draft…")}</p>;
  const section = data.sections?.find(s => s.section_id === selected || s.paragraphs.some(p => p.paragraph_key === selected || p.paragraph_id === selected)) || data.sections?.[0];
  const candidates = data.rewrite_candidates.filter(c => ["dialogue", "section_synthesis"].includes(c.revision_mode || ""));
  const pending = candidates.filter(c => c.status === "pending");
  const activeBatch = jobIsActive(data.dialogue_batch_job?.status);
  const choose = (key: string) => switchTab("dialogue", key);
  const tabs = [["batch", text("批量优化与建议", "Batch revision & suggestions")], ["dialogue", text("章节对话", "Chapter dialogue")], ["preview", text("完整正文", "Manuscript")], ["approval", text("确认事项", "Approval details")], ["history", text("版本历史", "History")]];
  const latestPending = [...new Map(pending.map(c => [c.paragraph_key, c])).values()];
  const selectedCandidates = pending.filter(c => checked.includes(c.candidate_id));
  const selectedTextByParagraph = new Map(selectedCandidates.map(c => [c.paragraph_key, c.candidate_text]));
  const combinedTexts = selectedCandidates.length && !selectedCandidates.some(c => c.revision_mode === "section_synthesis")
    ? data.paragraphs.map(p => (selectedTextByParagraph.get(p.paragraph_key) || p.text).trim()).filter(Boolean) : [];
  const duplicateText = combinedTexts.length > new Set(combinedTexts).size;
  const batchJob = data.dialogue_batch_job;
  const incompleteBatchResults = Object.values(batchJob?.result?.paragraph_results || {}).filter(result => result.status !== "completed");
  const batchStatus = batchJob ? batchJob.status === "succeeded" && incompleteBatchResults.length
    ? text("部分分析完成", "Partially complete")
    : ({ queued: text("排队中", "Queued"), running: text("正在分析", "Analyzing"), succeeded: text("分析完成", "Analysis complete"), failed: text("分析失败", "Analysis failed"), cancelled: text("已取消", "Cancelled"), interrupted: text("已中断", "Interrupted") } as Record<string, string>)[batchJob.status] || batchJob.status : "";
  const manuscript = (keywords?: ReactNode) => <EditableManuscript
    content={data.manuscript_fields ? (data.manuscript_preview_md ?? data.first_draft_md).replace(/^#\s+[^\n]+\n?/m, "") : data.manuscript_preview_md ?? data.first_draft_md}
    keywords={keywords} paragraphs={data.paragraphs} userId={userId} projectId={projectId} revision={data.revision}
    disabled={(data.freshness.editing_blocked ?? data.freshness.upstream_stale) || decision.isPending} refresh={refresh} onEditingChange={recordEdit} />;
  return <>
    <DraftSynthesisStatus job={data.synthesis_job} assembling={assemble.isPending} />

    {tab === "preview" && data.synthesis_stale ? <p role="status">{text("正文或结论已有更新，可按需更新摘要/结论，不影响确认和导出。", "Source text changed. Update synthesis if needed; approval and export remain available.")}</p> : null}
    {notice ? <p role="status" className="message">{notice}</p> : null}
    {error ? <p role="alert" className="message message-error"><LocalizedError error={error} /></p> : null}
    {data.freshness.upstream_stale ? <section className="message message-warning"><p>{data.freshness.editing_blocked === false
      ? text("配图已更新，正文仍可手动修改和保存。配图同步前暂不能确认终稿；重新组装前请备份手动编辑，组装会重建正文。", "Figures changed. You can still edit and save prose. Final approval requires synchronization; back up manual edits before reassembly, which rebuilds the manuscript.")
      : text("正文证据已变化，暂不能保存段落。现有输入仍保留，请先备份修改，再同步上游内容。", "Manuscript evidence changed; paragraph saving is temporarily unavailable. Existing input is retained. Back up edits before synchronizing upstream content.")}</p><button className="button button-secondary" disabled={assemble.isPending} onClick={() => assemble.mutate()}>{text("根据上游重新组装", "Reassemble from upstream")}</button></section> : null}
    {!data.draft_artifact_id ? <button className="button button-primary" disabled={assemble.isPending} onClick={() => assemble.mutate()}>{text("组装初稿", "Assemble draft")}</button> : <>
      <div className="draft-stage-navigation">
        <nav className="workspace-step-tabs" aria-label={text("初稿工作区", "Draft workspace")}>
          <button type="button" className={["preview", "dialogue"].includes(tab) ? "active" : ""} aria-current={["preview", "dialogue"].includes(tab) ? "page" : undefined} onClick={() => switchTab("preview")}>{text("完整正文", "Manuscript")}</button>
          <button type="button" className={tab === "batch" ? "active" : ""} aria-current={tab === "batch" ? "page" : undefined} onClick={() => switchTab("batch")}>{text("批量优化", "Batch revision")}{pending.length ? " · " + pending.length : ""}</button>
        </nav>
        <button type="button" className="button button-quiet draft-history-link" onClick={() => switchTab("history")}>{text("版本历史", "History")}</button>
      </div>
      {pending.length > 0 && tab !== "batch" ? <p role="status"><button className="button button-quiet" onClick={() => switchTab("batch")}>{text("待查看修改建议：", "Suggestions to review: ")}{pending.length}{text(" 处，尚未改变正文", " — not yet applied")}</button></p> : null}
      <div className={tab === "dialogue" ? `draft-reading-with-dialogue${dialogueExpanded ? " draft-dialogue-expanded" : ""}` : ""}>
      <section hidden={!["preview", "dialogue"].includes(tab) || (tab === "dialogue" && dialogueExpanded)} className="pane draft-manuscript-home">


        <DraftReader discussOnSelect={tab === "dialogue"} content={data.manuscript_preview_md ?? data.first_draft_md} dirtyParagraphKeys={dirtyParagraphKeys}
          toolbarActions={<DraftCompositionPanel key={projectId} captionOpen={captionOpen} projectId={projectId} markdown={data.first_draft_md} job={data.synthesis_job} refresh={refresh} candidates={data.rewrite_candidates} requireSaved={requireSaved} />}
          onOverview={() => setCaptionOpen(value => value + 1)} onChapter={title => {
          const normalize = (value: string) => value.replace(/^\d+(?:\.\d+)*[.)]?\s*/, "").trim().toLowerCase();
          const target = data.sections?.find(s => normalize(s.title) === normalize(title));
          choose(target?.section_id || section?.section_id || "");
        }}>
          {data.manuscript_fields ? <DraftManuscriptFields userId={userId} projectId={projectId} revision={data.revision} fields={data.manuscript_fields} legacy={data.legacy_manuscript_fields} refresh={refresh} onEditingChange={recordFields}
            renderFields={(title, keywords) => <>{title}{manuscript(keywords)}</>} /> : manuscript()}
        </DraftReader>
      </section>
      <div hidden={tab === "preview"} className={tab === "dialogue" ? "draft-dialogue-layout" : "draft-single-panel"}>
      <section className="pane draft-workspace-panel" id="draft-workspace-panel" ref={panelRef} aria-label={tabs.find(([id]) => id === tab)?.[1]}><div className="pane-head"><button className="button button-quiet" onClick={() => switchTab("preview")}>{text("返回正文", "Back to manuscript")}</button>{tab === "dialogue" ? <button type="button" className="button button-secondary" aria-expanded={dialogueExpanded} onClick={() => setDialogueExpanded(value => !value)}>{dialogueExpanded ? text("恢复双栏", "Restore split view") : text("展开对话", "Expand dialogue")}</button> : null}<h2>{tabs.find(([id]) => id === tab)?.[1]}</h2></div><div className="pane-body">
        {tab === "dialogue" && section ? <SectionDialogue key={userId + projectId + section.section_id} userId={userId} projectId={projectId} section={section} revision={data.revision} blocked={data.freshness.upstream_stale} refresh={refresh} activeTask={data.section_task_states?.[section.section_id]} candidates={candidates.filter(c => section.paragraphs.some(p => p.paragraph_key === c.paragraph_key))} decisionPending={decision.isPending} decide={(ids, action) => decision.mutate({ ids, action })} /> : null}
        {tab === "dialogue" && !section ? <p role="status">{text("当前初稿没有可编辑章节，请检查完整正文。", "No editable chapters are available; check the manuscript.")}</p> : null}

        {tab === "approval" ? <DraftApprovalPanel quality={data.quality?.current ? data.quality : {}} approved={data.draft_approval_current} onReview={choose} /> : null}
        {tab === "batch" ? <div className="draft-batch-workspace">
          <section className="draft-batch-intro">
            <div><span className="step-label">{text("全文检查", "Manuscript review")}</span><h3>{text("批量分析与优化", "Batch analysis and revision")}</h3><p>{text("系统提出修改建议，原文不会自动替换；审阅后再保存。", "The system proposes changes without replacing saved text. Review before saving.")}</p></div>
            <button className="button button-primary" disabled={activeBatch || batch.isPending || data.freshness.upstream_stale} onClick={() => batch.mutate()}>{batch.isPending ? text("正在提交…", "Submitting…") : text("开始批量分析优化", "Start batch revision")}</button>
          </section>
          {batchJob ? <section className="draft-batch-status" role="status">
            <div className="draft-batch-status-head"><div><strong>{batchStatus}</strong><span>{batchJob.progress_current}/{batchJob.progress_total || data.paragraphs.length} {text("段已处理", "paragraphs processed")}</span></div><div className="draft-batch-status-actions">
              {activeBatch ? <button className="button button-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消任务", "Cancel task")}</button> : null}
              {(["failed", "cancelled", "interrupted"].includes(batchJob.status) || (batchJob.status === "succeeded" && incompleteBatchResults.length > 0)) ? <button className="button button-secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>{text("继续未完成段落", "Resume unfinished paragraphs")}</button> : null}
            </div></div>
            {activeBatch ? <progress max={batchJob.progress_total || data.paragraphs.length} value={batchJob.progress_current} /> : null}
            {!activeBatch && ["unavailable", "partial"].includes(batchJob.result?.coherence_plan?.status || "") ? <p>{text("本轮只完成了部分范围，已保存正文不受影响。", "This run covered only part of the manuscript; saved text is unchanged.")}</p> : null}
            {batchJob.error_message ? <p className="message message-warning">{batchJob.error_message}</p> : null}
            {incompleteBatchResults.length ? <details><summary>{text(`查看 ${incompleteBatchResults.length} 处未完成内容`, `View ${incompleteBatchResults.length} unfinished items`)}</summary><div className="draft-batch-incomplete">{incompleteBatchResults.map(result => <p key={result.paragraph_id}><button className="button button-quiet" onClick={() => choose(result.paragraph_id)}>{result.paragraph_id}</button><span>{result.reason?.includes("unknown source passage")
              ? text("原文引用未核实，此段未保存。可仅重试未完成段落。", "The source citation could not be verified; this paragraph was not saved. Retry unfinished paragraphs only.")
              : result.reason || result.status}</span></p>)}</div></details> : null}
          </section> : null}
          <section className="draft-batch-review" aria-label={text("修改建议", "Revision suggestions")}>
            <div className="draft-batch-review-head"><div><span className="step-label">{text("审阅候选", "Review suggestions")}</span><h3>{text("修改建议", "Revision suggestions")}</h3><p>{pending.length ? text(`${pending.length} 处待确认，逐条比较原文与候选。`, `${pending.length} pending; compare each suggestion with the original.`) : text("没有待确认的修改。", "No suggestions awaiting review.")}</p></div>
              {pending.length ? <div className="draft-batch-bulk"><button className="button button-secondary" onClick={() => setChecked(latestPending.map(c => c.candidate_id))}>{text("选择全部待确认", "Select all pending")}</button><button className="button button-primary" disabled={decision.isPending || !selectedCandidates.length || data.freshness.upstream_stale} onClick={() => decision.mutate({ ids: selectedCandidates.map(c => c.candidate_id), action: "accept" })}>{text(`保存选中（${selectedCandidates.length}）`, `Save selected (${selectedCandidates.length})`)}</button></div> : null}
            </div>
            {selectedCandidates.length > 0 && duplicateText ? <p role="status" className="message message-warning">{text("选中候选组合中有完全重复的段落，请逐条核对；系统不会自动改写。", "Selected suggestions would create identical paragraphs. Review them; the system will not rewrite them automatically.")}</p> : null}
            {candidates.length ? <CandidateBrowser focusId={params.get("candidate") || ""} stateKey={`${userId}:${projectId}:batch-browser`} candidates={candidates} disabled={decision.isPending || data.freshness.upstream_stale} checked={checked}
              toggle={(c, selected) => setChecked(selected ? [...checked.filter(id => !pending.some(other => other.candidate_id === id && other.paragraph_key === c.paragraph_key)), c.candidate_id] : checked.filter(id => id !== c.candidate_id))}
              decide={(id, action) => decision.mutate({ ids: [id], action })} />
              : <p role="status" className="draft-batch-empty">{batchJob?.status === "succeeded" ? text("本轮没有生成需要确认的修改建议。", "This run produced no changes requiring review.") : text("运行批量分析后，修改建议会显示在这里。", "Run batch analysis to see suggestions here.")}</p>}
          </section>
        </div> : null}
        {tab === "history" ? <><p>{text("恢复版本将改变当前正文并使终稿过期；历史文件保持不变。", "Restoring changes the current draft and invalidates the final. Historical files remain unchanged.")}</p>{!data.versions.length ? <p role="status">{text("暂无历史版本。", "No historical versions yet.")}</p> : null}{inspect.isPending ? <p role="status">{text("正在加载版本…", "Loading version…")}</p> : null}{data.versions.slice(0, versionLimit).map(v => <p key={v.artifact_id}>{v.created_at} · {v.operation} <button className="button button-secondary" disabled={inspect.isPending} onClick={() => inspect.mutate(v.artifact_id)}>{text("预览版本", "Preview")}</button></p>)}{data.versions.length > versionLimit ? <button className="button button-secondary" onClick={() => setVersionLimit(n => n + 20)}>{text("加载更早版本", "Load older versions")}</button> : null}{viewed ? <section className="draft-version-preview"><DraftReader content={viewed.content} /><button className="button button-secondary" disabled={restore.isPending || viewed.id === data.draft_artifact_id} onClick={() => restore.mutate(viewed.id)}>{text("确认恢复此版本", "Confirm restore")}</button></section> : null}</> : null}
      </div></section></div></div>
      <div className="stage-action-bar draft-stage-action-bar">
        <div>
          <strong>{data.draft_approval_current ? text("初稿已确认", "Draft approved") : text("正文确认", "Manuscript approval")}</strong>
          <p>{data.freshness.upstream_stale
            ? text("上游内容已变化，请先同步正文，再进入终稿。", "Upstream content changed. Synchronize the manuscript before entering Final.")
            : pending.length
              ? text(`还有 ${pending.length} 处未采用的修改建议；它们不会自动进入终稿。`, `${pending.length} unaccepted suggestions will not enter Final automatically.`)
              : text("确认当前已保存的正文后进入终稿；未保存编辑不会被带入。", "Confirm the saved manuscript to continue to Final; unsaved edits are not included.")}</p>
          {approve.error ? <p className="message message-error" role="alert"><LocalizedError error={approve.error} /></p> : null}
        </div>
        <div className="stage-action-buttons">
          <button className="button button-quiet" type="button" onClick={() => switchTab("approval")}>{text("查看确认事项", "Review approval details")}</button>
          <button className="button button-primary" type="button" disabled={approve.isPending || data.freshness.upstream_stale}
            onClick={() => {
              if (!data.draft_approval_current) { approve.mutate(); return; }
              try { requireSaved(); navigate("/final?project=" + encodeURIComponent(projectId)); }
              catch (error) { setNotice((error as Error).message); }
            }}>
            {approve.isPending ? text("正在确认…", "Approving…") : data.draft_approval_current ? text("进入终稿", "Enter Final") : text("确认进入终稿", "Approve and enter Final")}
          </button>
        </div>
      </div>
    </>}
  </>;
}
