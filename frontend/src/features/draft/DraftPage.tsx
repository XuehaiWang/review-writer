import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { meQuery } from "../../api/queries";
import { MarkdownView } from "../../components/MarkdownView";
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
import { DraftCompositionPanel, type SynthesisJob } from "./DraftCompositionPanel";
import { DraftManuscriptFields } from "./DraftManuscriptFields";

type Batch = { id: string; status: string; progress_current: number; progress_total: number; error_message?: string; result?: { paragraph_results?: Record<string, { paragraph_id: string; status: string; reason?: string }> } };
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
  freshness: { upstream_stale: boolean }; versions: { artifact_id: string; current: boolean; operation: string; created_at: string }[] };

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
  const switchTab = (id: string, paragraphKey?: string) => {
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
  const assemble = useMutation({ mutationFn: () => apiRequest<{ synthesis_warning?: string }>(base + "/assemble", { method: "POST" }),
    onSuccess: async result => { if (result.synthesis_warning) setNotice(result.synthesis_warning); await refresh(); } });
  useEffect(() => {
    if (!data || data.draft_artifact_id || data.freshness.upstream_stale || autoAssembled.current) return;
    autoAssembled.current = true;
    assemble.mutate();
  }, [data]);
  const batch = useMutation({ mutationFn: () => apiRequest(base + "/dialogue-batch", { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() } }), onSuccess: refresh });
  const cancel = useMutation({ mutationFn: () => apiRequest("/api/v1/jobs/" + data!.dialogue_batch_job!.id + "/cancel", { method: "POST" }), onSuccess: refresh });
  const retry = useMutation({ mutationFn: () => apiRequest("/api/v1/jobs/" + data!.dialogue_batch_job!.id + "/retry", { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() } }), onSuccess: refresh });
  const approve = useMutation({ mutationFn: () => approveCurrentDraft(projectId, data!, text("已保存正文发生变化，请刷新后确认。", "Saved text changed. Refresh before approving.")), onSuccess: refresh });
  const restore = useMutation({ mutationFn: (id: string) => apiRequest(base + "/restore", { method: "POST", ...jsonBody({ revision: data!.revision, artifact_id: id }) }), onSuccess: async () => { setViewed(null); await refresh(); } });
  const inspect = useMutation({ mutationFn: async (id: string) => ({ id, content: await apiRequest<string>("/api/v1/artifacts/" + id + "/content") }), onSuccess: setViewed });
  const decision = useMutation({ mutationFn: async ({ ids, action }: { ids: string[]; action: "accept" | "reject" }) => {
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
  const error = assemble.error || batch.error || cancel.error || retry.error || approve.error || restore.error || inspect.error;
  if (draft.error) return <ErrorState error={draft.error} onRetry={() => void draft.refetch()} />;
  if (!data) return <p role="status">{text("正在加载初稿…", "Loading draft…")}</p>;
  const section = data.sections?.find(s => s.section_id === selected || s.paragraphs.some(p => p.paragraph_key === selected || p.paragraph_id === selected)) || data.sections?.[0];
  const candidates = data.rewrite_candidates.filter(c => ["dialogue", "section_synthesis"].includes(c.revision_mode || ""));
  const pending = candidates.filter(c => c.status === "pending");
  const activeBatch = jobIsActive(data.dialogue_batch_job?.status);
  const choose = (key: string) => switchTab("dialogue", key);
  const tabs = [["batch", text("批量优化与建议", "Batch revision & suggestions")], ["dialogue", text("章节对话", "Chapter dialogue")], ["preview", text("完整正文", "Manuscript")], ["approval", text("确认进入终稿", "Approve for Final")], ["history", text("版本历史", "History")]];
  const latestPending = [...new Map(pending.map(c => [c.paragraph_key, c])).values()];
  const selectedCandidates = pending.filter(c => checked.includes(c.candidate_id));
  const combined = data.paragraphs.map(p => ({ ...p, text: selectedCandidates.find(c => c.paragraph_key === p.paragraph_key)?.candidate_text || p.text }));
  const duplicateText = combined.some((p, i) => combined.slice(0, i).some(other => other.text.trim() === p.text.trim()));
  return <>
    {jobIsActive(data.synthesis_job?.status) ? <p role="status">{text("正在补齐摘要或结论；你可以查看正文，当前批量任务不会中途增加段落。", "Synthesis is running; existing batch tasks keep their original paragraph snapshot.")}</p> : null}
    {data.synthesis_job?.status === "failed" ? <p role="status">{text("摘要/结论生成未完成，已有正文保留。点击“摘要”或“结论”可重新生成。", "Synthesis did not finish. Existing text is safe; open Abstract or Conclusion to retry.")}</p> : null}

    {tab === "preview" && data.synthesis_stale ? <p role="status">{text("正文或结论已有更新，可按需更新摘要/结论，不影响确认和导出。", "Source text changed. Update synthesis if needed; approval and export remain available.")}</p> : null}
    {notice ? <p role="status" className="message">{notice}</p> : null}
    {error ? <p role="alert" className="message message-error">{error.message}</p> : null}
    {data.freshness.upstream_stale ? <section className="message message-warning"><p>{text("上游内容已变化，当前正文仍保留。重新组装前请先保留需要的编辑。", "Upstream content changed. Existing text is retained; preserve edits before reassembling.")}</p><button className="button button-secondary" disabled={assemble.isPending} onClick={() => assemble.mutate()}>{text("根据上游重新组装", "Reassemble from upstream")}</button></section> : null}
    {!data.draft_artifact_id ? <button className="button button-primary" disabled={assemble.isPending} onClick={() => assemble.mutate()}>{text("组装初稿", "Assemble draft")}</button> : <>
      <nav className="draft-main-toolbar button-row" aria-label={text("初稿工作区", "Draft workspace")}>
        <button className="button button-secondary" onClick={() => switchTab("preview")}>{text("完整正文", "Manuscript")}</button>
        <button className="button button-secondary" onClick={() => switchTab("batch")}>{text("批量优化", "Batch revision")}{pending.length ? " · " + pending.length : ""}</button>
        <button className="button button-primary" onClick={() => switchTab("approval")}>{text("确认进入终稿", "Approve for Final")}</button>
        <details><summary>{text("更多", "More")}</summary><button className="button button-quiet" onClick={() => switchTab("history")}>{text("版本历史", "History")}</button></details>
      </nav>
      {pending.length > 0 && tab !== "batch" ? <p role="status"><button className="button button-quiet" onClick={() => switchTab("batch")}>{text("待查看修改建议：", "Suggestions to review: ")}{pending.length}{text(" 处，尚未改变正文", " — not yet applied")}</button></p> : null}
      <div hidden={!["preview", "dialogue"].includes(tab)} className="draft-document-tools">
        {data.manuscript_fields ? <DraftManuscriptFields userId={userId} projectId={projectId} revision={data.revision} fields={data.manuscript_fields} legacy={data.legacy_manuscript_fields} refresh={refresh} /> : null}
        <DraftCompositionPanel key={projectId} captionOpen={captionOpen} projectId={projectId} markdown={data.first_draft_md} job={data.synthesis_job} refresh={refresh} candidates={data.rewrite_candidates} />
      </div>
      <div className={tab === "dialogue" ? `draft-reading-with-dialogue${dialogueExpanded ? " draft-dialogue-expanded" : ""}` : ""}>
      <section hidden={!["preview", "dialogue"].includes(tab) || (tab === "dialogue" && dialogueExpanded)} className="pane draft-manuscript-home">


        <DraftReader discussOnSelect={tab === "dialogue"} content={data.manuscript_preview_md ?? data.first_draft_md} onOverview={() => setCaptionOpen(value => value + 1)} onChapter={title => {
          const normalize = (value: string) => value.replace(/^\d+(?:\.\d+)*[.)]?\s*/, "").trim().toLowerCase();
          const target = data.sections?.find(s => normalize(s.title) === normalize(title));
          choose(target?.section_id || section?.section_id || "");
        }} />
      </section>
      <div hidden={tab === "preview"} className={tab === "dialogue" ? "draft-dialogue-layout" : "draft-single-panel"}>
      <section className="pane draft-workspace-panel" id="draft-workspace-panel" ref={panelRef} aria-label={tabs.find(([id]) => id === tab)?.[1]}><div className="pane-head"><button className="button button-quiet" onClick={() => switchTab("preview")}>{text("返回正文", "Back to manuscript")}</button>{tab === "dialogue" ? <button type="button" className="button button-secondary" aria-expanded={dialogueExpanded} onClick={() => setDialogueExpanded(value => !value)}>{dialogueExpanded ? text("恢复双栏", "Restore split view") : text("展开对话", "Expand dialogue")}</button> : null}<h2>{tabs.find(([id]) => id === tab)?.[1]}</h2></div><div className="pane-body">
        {tab === "dialogue" && section ? <SectionDialogue key={userId + projectId + section.section_id} userId={userId} projectId={projectId} section={section} revision={data.revision} blocked={data.freshness.upstream_stale} refresh={refresh} activeTask={data.section_task_states?.[section.section_id]} candidates={candidates.filter(c => section.paragraphs.some(p => p.paragraph_key === c.paragraph_key))} decisionPending={decision.isPending} decide={(ids, action) => decision.mutate({ ids, action })} /> : null}
        {tab === "dialogue" && !section ? <p role="status">{text("当前初稿没有可编辑章节，请检查完整正文。", "No editable chapters are available; check the manuscript.")}</p> : null}

        {tab === "approval" ? <DraftApprovalPanel quality={data.quality?.current ? data.quality : {}} approved={data.draft_approval_current} busy={approve.isPending || data.freshness.upstream_stale} onApprove={() => approve.mutate()} onNext={() => navigate("/final?project=" + projectId)} onReview={choose} /> : null}
        {tab === "batch" ? <>
          <h2>{text("分析并优化全部段落", "Analyze and revise all paragraphs")}</h2><p>{text("每段只分析优化一次。失败段落单独记录，其余继续；所有候选由你确认保存。", "One revision task per paragraph. Failures do not stop the rest. No candidate is saved without your confirmation.")}</p>
          <button className="button button-primary" disabled={activeBatch || batch.isPending || data.freshness.upstream_stale} onClick={() => batch.mutate()}>{text("开始批量分析优化", "Start batch revision")}</button>
          {data.dialogue_batch_job ? <section className="draft-batch-status" role="status"><p>{data.dialogue_batch_job.status} · {data.dialogue_batch_job.progress_current}/{data.dialogue_batch_job.progress_total || data.paragraphs.length}</p><progress max={data.dialogue_batch_job.progress_total || data.paragraphs.length} value={data.dialogue_batch_job.progress_current} />
            {activeBatch ? <button className="button button-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消批量任务", "Cancel batch")}</button> : null}
            {["failed", "cancelled", "interrupted"].includes(data.dialogue_batch_job.status) ? <button className="button button-secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>{text("继续未完成段落", "Resume unfinished paragraphs")}</button> : null}
            {data.dialogue_batch_job.error_message ? <p>{data.dialogue_batch_job.error_message}</p> : null}
            {Object.values(data.dialogue_batch_job.result?.paragraph_results || {}).filter(r => r.status !== "completed").map(r => <p key={r.paragraph_id}><button className="button button-quiet" onClick={() => choose(r.paragraph_id)}>{r.paragraph_id}</button> · {r.reason || r.status}</p>)}
            <p>{text("点击失败段落可进入章节对话单独处理，不必重做已完成的段落。", "Open a failed paragraph in chapter dialogue without replaying completed work.")}</p>
          </section> : null}

          <div className="button-row"><button className="button button-secondary" disabled={!pending.length} onClick={() => setChecked(latestPending.map(c => c.candidate_id))}>{text("全选每段最新候选", "Select latest per paragraph")}</button><button className="button button-secondary" disabled={decision.isPending || !selectedCandidates.length || data.freshness.upstream_stale} onClick={() => decision.mutate({ ids: selectedCandidates.map(c => c.candidate_id), action: "accept" })}>{text("保存选中候选", "Save selected")}</button></div>
          {selectedCandidates.length && !selectedCandidates.some(c => c.revision_mode === "section_synthesis") ? <details><summary>{text("查看采用后的段落组合预览（尚未保存）", "Preview combined paragraphs (not saved)")}</summary>{duplicateText ? <p role="status">{text("组合中存在完全重复的段落，请检查；不会因此自动改写。", "Identical paragraphs exist in this combination. Review them; no automatic rewriting is performed.")}</p> : null}<div className="draft-long-document">{combined.map(p => <article key={p.paragraph_key}><h4>{p.paragraph_id}</h4><MarkdownView content={p.text} /></article>)}</div></details> : null}
          {!candidates.length ? <p role="status" className="muted">{text("尚无候选。点击开始批量分析优化后，这里会显示处理进度和修改对比。", "No candidates yet. Start batch revision to see progress and comparisons here.")}</p> : null}
          <CandidateBrowser focusId={params.get("candidate") || ""} stateKey={`${userId}:${projectId}:batch-browser`} candidates={candidates} disabled={decision.isPending || data.freshness.upstream_stale} checked={checked}
            toggle={(c, selected) => setChecked(selected ? [...checked.filter(id => !pending.some(other => other.candidate_id === id && other.paragraph_key === c.paragraph_key)), c.candidate_id] : checked.filter(id => id !== c.candidate_id))}
            decide={(id, action) => decision.mutate({ ids: [id], action })} />
          <div className="button-row"><button type="button" className="button button-primary" onClick={() => switchTab("preview")}>{text("返回正文查看效果", "Return to manuscript")}</button></div>
        </> : null}
        {tab === "history" ? <><p>{text("恢复版本将改变当前正文并使终稿过期；历史文件保持不变。", "Restoring changes the current draft and invalidates the final. Historical files remain unchanged.")}</p>{!data.versions.length ? <p role="status">{text("暂无历史版本。", "No historical versions yet.")}</p> : null}{inspect.isPending ? <p role="status">{text("正在加载版本…", "Loading version…")}</p> : null}{data.versions.slice(0, versionLimit).map(v => <p key={v.artifact_id}>{v.created_at} · {v.operation} <button className="button button-secondary" disabled={inspect.isPending} onClick={() => inspect.mutate(v.artifact_id)}>{text("预览版本", "Preview")}</button></p>)}{data.versions.length > versionLimit ? <button className="button button-secondary" onClick={() => setVersionLimit(n => n + 20)}>{text("加载更早版本", "Load older versions")}</button> : null}{viewed ? <section className="draft-version-preview"><DraftReader content={viewed.content} /><button className="button button-secondary" disabled={restore.isPending || viewed.id === data.draft_artifact_id} onClick={() => restore.mutate(viewed.id)}>{text("确认恢复此版本", "Confirm restore")}</button></section> : null}</> : null}
      </div></section></div></div>
    </>}
  </>;
}
