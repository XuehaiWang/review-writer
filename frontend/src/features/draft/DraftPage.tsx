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

type Batch = { id: string; status: string; progress_current: number; progress_total: number; error_message?: string; result?: { paragraph_results?: Record<string, { paragraph_id: string; status: string; reason?: string }> } };
type DraftPayload = { revision: number; draft_artifact_id: string; first_draft_md: string; paragraphs: DialogueParagraph[];
  active_feedback_job_id?: string;
  sections: DialogueSection[]; section_task_states: Record<string, { id: string; status: string }>;
  paragraph_task_states: Record<string, { id: string; status: string; batch: boolean }>;
  quality: ApprovalQuality; quality_artifact_id: string; draft_approval_current: boolean;
  rewrite_candidates: (DialogueCandidate & { revision_mode?: string })[]; dialogue_batch_job?: Batch;
  freshness: { upstream_stale: boolean }; versions: { artifact_id: string; current: boolean; operation: string; created_at: string }[] };

export function DraftPage() {
  const { text } = useUiText();
  const { selected: project } = useSelectedProject();
  const me = useQuery(meQuery);
  return <main className="workspace page-container workspace-page draft-page">
    <div className="workspace-heading">
      <div><p className="eyebrow">{text("阶段 6 · 初稿", "Stage 6 · Draft")}</p>
        <h1>{text("初稿修订", "Draft revision")}</h1>
        <p className="muted">{text("先由 AI 批量分析优化并生成候选，再通过章节对话和手动编辑完善正文，最后预览与确认。", "Start with AI batch analysis and candidates, refine through chapter dialogue or manual edits, then preview and approve.")}</p>
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
    ? requestedTab! : params.get("paragraph") ? "dialogue" : "batch";
  const selected = params.get("paragraph") || "";
  const panelRef = useRef<HTMLElement>(null);
  const switchTab = (id: string, paragraphKey?: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", id);
    if (paragraphKey !== undefined) next.set("paragraph", paragraphKey);
    setParams(next);
    panelRef.current?.scrollIntoView?.({ block: "start" });
  };
  const autoAssembled = useRef(false);
  const [checked, setChecked] = useState<string[]>([]);
  const [notice, setNotice] = useState("");
  const [versionLimit, setVersionLimit] = useState(20);
  const [viewed, setViewed] = useState<{ id: string; content: string } | null>(null);
  const base = "/api/v1/projects/" + encodeURIComponent(projectId) + "/draft";
  const draft = useQuery({ queryKey: ["draft", projectId], queryFn: () => apiRequest<DraftPayload>(base),
    refetchInterval: query => query.state.data?.active_feedback_job_id || jobIsActive(query.state.data?.dialogue_batch_job?.status) ? 3000 : false, refetchIntervalInBackground: true });
  const data = draft.data;
  const refresh = async () => { await Promise.all([client.invalidateQueries({ queryKey: ["draft", projectId] }), client.invalidateQueries({ queryKey: ["draft-dialogue", projectId] })]); };
  const assemble = useMutation({ mutationFn: () => apiRequest(base + "/assemble", { method: "POST" }), onSuccess: refresh });
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
      if (action === "accept" && candidate && hasDraftScratch(userId + ":" + projectId + ":" + candidate.paragraph_key + ":manual")) throw new Error(text("该段有未保存的手动编辑，请先处理。", "Resolve this paragraph's unsaved manual edit first."));
      await apiRequest(base + "/dialogue-candidates/" + id + "/" + action, { method: "POST" });
    } catch (error) { failures.push(id + ": " + (error as Error).message); } }
    return { failures, completed: ids.length - failures.length };
  }, onSuccess: async (result, { action }) => { setChecked([]); setNotice(
    (action === "accept" ? text("已保存修改：", "Changes saved: ") : text("已放弃候选：", "Candidates discarded: "))
    + result.completed + (result.failures.length ? " · " + result.failures.join("; ") : "")); await refresh(); } });
  const error = assemble.error || batch.error || cancel.error || retry.error || approve.error || restore.error || inspect.error;
  if (draft.error) return <ErrorState error={draft.error} onRetry={() => void draft.refetch()} />;
  if (!data) return <p role="status">{text("正在加载初稿…", "Loading draft…")}</p>;
  const section = data.sections?.find(s => s.section_id === selected || s.paragraphs.some(p => p.paragraph_key === selected || p.paragraph_id === selected)) || data.sections?.[0];
  const candidates = data.rewrite_candidates.filter(c => c.revision_mode === "dialogue");
  const pending = candidates.filter(c => c.status === "pending");
  const activeBatch = jobIsActive(data.dialogue_batch_job?.status);
  const choose = (key: string) => switchTab("dialogue", key);
  const tabs = [["batch", text("01 批量分析与候选", "01 Batch analysis & candidates")], ["dialogue", text("02 章节对话", "02 Chapter dialogue")], ["preview", text("03 完整正文", "03 Manuscript")], ["approval", text("04 人工确认", "04 Approval")], ["history", text("版本历史", "History")]];
  const latestPending = [...new Map(pending.map(c => [c.paragraph_key, c])).values()];
  const selectedCandidates = pending.filter(c => checked.includes(c.candidate_id));
  const combined = data.paragraphs.map(p => ({ ...p, text: selectedCandidates.find(c => c.paragraph_key === p.paragraph_key)?.candidate_text || p.text }));
  const duplicateText = combined.some((p, i) => combined.slice(0, i).some(other => other.text.trim() === p.text.trim()));
  return <>
    {notice ? <p role="status" className="message">{notice}</p> : null}
    {error ? <p role="alert" className="message message-error">{error.message}</p> : null}
    {data.freshness.upstream_stale ? <section className="message message-warning"><p>{text("上游内容已变化，当前正文仍保留。重新组装前请先保留需要的编辑。", "Upstream content changed. Existing text is retained; preserve edits before reassembling.")}</p><button className="button button-secondary" disabled={assemble.isPending} onClick={() => assemble.mutate()}>{text("根据上游重新组装", "Reassemble from upstream")}</button></section> : null}
    {!data.draft_artifact_id ? <button className="button button-primary" disabled={assemble.isPending} onClick={() => assemble.mutate()}>{text("组装初稿", "Assemble draft")}</button> : <>
      <nav className="workspace-mode-tabs" aria-label={text("初稿工作区", "Draft workspace")}>{tabs.map(([id, label]) => <button type="button" className={tab === id ? "active" : ""} aria-pressed={tab === id} aria-controls="draft-workspace-panel" key={id} onClick={() => switchTab(id)}>{label}</button>)}</nav>
      <div className={tab === "dialogue" ? "draft-dialogue-layout" : "draft-single-panel"}>{tab === "dialogue" ? <aside className="pane"><div className="pane-head"><div><span className="step-label">{text("选择修订目标", "Revision target")}</span><h2>{text("章节", "Chapters")}</h2><p>{data.sections?.length || 0} {text("章", "chapters")}</p></div></div><div className="dialogue-paragraph-list">{data.sections?.map(s => <button className={section?.section_id === s.section_id ? "selected" : ""} aria-pressed={section?.section_id === s.section_id} key={s.section_id} onClick={() => choose(s.section_id)}><strong>{s.title}</strong><span>{s.paragraphs.length} {text("段正文", "paragraphs")}</span>{data.section_task_states?.[s.section_id] ? <em>{text("正在分析优化", "Revision in progress")}</em> : pending.some(c => s.paragraphs.some(p => p.paragraph_key === c.paragraph_key)) ? <em>{text("待确认候选", "Candidates pending")}</em> : null}</button>)}</div></aside> : null}
      <section className="pane draft-workspace-panel" id="draft-workspace-panel" ref={panelRef} aria-label={tabs.find(([id]) => id === tab)?.[1]}><div className="pane-head"><h2>{tabs.find(([id]) => id === tab)?.[1]}</h2></div><div className="pane-body">
        {tab === "dialogue" && section ? <SectionDialogue key={userId + projectId + section.section_id} userId={userId} projectId={projectId} section={section} revision={data.revision} blocked={data.freshness.upstream_stale} refresh={refresh} activeTask={data.section_task_states?.[section.section_id]} candidates={candidates.filter(c => section.paragraphs.some(p => p.paragraph_key === c.paragraph_key))} decisionPending={decision.isPending} decide={(ids, action) => decision.mutate({ ids, action })} /> : null}
        {tab === "dialogue" && !section ? <p role="status">{text("当前初稿没有可编辑章节，请检查完整正文。", "No editable chapters are available; check the manuscript.")}</p> : null}
        {tab === "preview" ? <><p className="muted">{text("这里显示最新已保存正文，不包含尚未采用的候选。", "This is the latest saved manuscript, excluding unaccepted candidates.")}</p>{data.first_draft_md ? <DraftReader content={data.first_draft_md} /> : <p role="status">{text("暂无已保存正文。", "No saved manuscript yet.")}</p>}<button type="button" className="button button-primary" onClick={() => switchTab("approval")}>{text("下一步：人工确认", "Next: approval")}</button></> : null}
        {tab === "approval" ? <DraftApprovalPanel quality={data.quality?.current ? data.quality : {}} approved={data.draft_approval_current} busy={approve.isPending || data.freshness.upstream_stale} onApprove={() => approve.mutate()} onNext={() => navigate("/final?project=" + projectId)} onReview={choose} /> : null}
        {tab === "batch" ? <>
          <h2>{text("分析并优化全部段落", "Analyze and revise all paragraphs")}</h2><p>{text("每段只分析优化一次。失败段落单独记录，其余继续；所有候选由你确认保存。", "One revision task per paragraph. Failures do not stop the rest. No candidate is saved without your confirmation.")}</p>
          <button className="button button-primary" disabled={activeBatch || batch.isPending || data.freshness.upstream_stale} onClick={() => batch.mutate()}>{text("开始批量分析优化", "Start batch revision")}</button>
          {data.dialogue_batch_job ? <section className="draft-batch-status" role="status"><p>{data.dialogue_batch_job.status} · {data.dialogue_batch_job.progress_current}/{data.dialogue_batch_job.progress_total || data.paragraphs.length}</p><progress max={data.dialogue_batch_job.progress_total || data.paragraphs.length} value={data.dialogue_batch_job.progress_current} />
            {activeBatch ? <button className="button button-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消批量任务", "Cancel batch")}</button> : null}
            {["failed", "cancelled", "interrupted"].includes(data.dialogue_batch_job.status) ? <button className="button button-secondary" disabled={retry.isPending} onClick={() => retry.mutate()}>{text("继续未完成段落", "Resume unfinished paragraphs")}</button> : null}
            {data.dialogue_batch_job.error_message ? <p>{data.dialogue_batch_job.error_message}</p> : null}
            {Object.values(data.dialogue_batch_job.result?.paragraph_results || {}).filter(r => r.status !== "completed").map(r => <p key={r.paragraph_id}><button className="button button-quiet" onClick={() => choose(r.paragraph_id)}>{r.paragraph_id}</button> · {r.reason || r.status}</p>)}
            <p>{text("失败或跳过的段落可在左侧选择后单独发送，不必重做已完成的段落。", "Select failed or skipped paragraphs on the left to retry individually without replaying completed work.")}</p>
          </section> : null}

          <div className="button-row"><button className="button button-secondary" disabled={!pending.length} onClick={() => setChecked(latestPending.map(c => c.candidate_id))}>{text("全选每段最新候选", "Select latest per paragraph")}</button><button className="button button-secondary" disabled={decision.isPending || !selectedCandidates.length || data.freshness.upstream_stale} onClick={() => decision.mutate({ ids: selectedCandidates.map(c => c.candidate_id), action: "accept" })}>{text("保存选中候选", "Save selected")}</button></div>
          {selectedCandidates.length ? <details><summary>{text("查看采用后的段落组合预览（尚未保存）", "Preview combined paragraphs (not saved)")}</summary>{duplicateText ? <p role="status">{text("组合中存在完全重复的段落，请检查；不会因此自动改写。", "Identical paragraphs exist in this combination. Review them; no automatic rewriting is performed.")}</p> : null}<div className="draft-long-document">{combined.map(p => <article key={p.paragraph_key}><h4>{p.paragraph_id}</h4><MarkdownView content={p.text} /></article>)}</div></details> : null}
          {!candidates.length ? <p role="status" className="muted">{text("尚无候选。点击开始批量分析优化后，这里会显示处理进度和修改对比。", "No candidates yet. Start batch revision to see progress and comparisons here.")}</p> : null}
          <CandidateBrowser stateKey={`${userId}:${projectId}:batch-browser`} candidates={candidates} disabled={decision.isPending || data.freshness.upstream_stale} checked={checked}
            toggle={(c, selected) => setChecked(selected ? [...checked.filter(id => !pending.some(other => other.candidate_id === id && other.paragraph_key === c.paragraph_key)), c.candidate_id] : checked.filter(id => id !== c.candidate_id))}
            decide={(id, action) => decision.mutate({ ids: [id], action })} />
          <div className="button-row"><button type="button" className="button button-primary" onClick={() => switchTab("dialogue")}>{text("下一步：章节讨论与人工修改", "Next: chapter discussion and manual edits")}</button></div>
        </> : null}
        {tab === "history" ? <><p>{text("恢复版本将改变当前正文并使终稿过期；历史文件保持不变。", "Restoring changes the current draft and invalidates the final. Historical files remain unchanged.")}</p>{!data.versions.length ? <p role="status">{text("暂无历史版本。", "No historical versions yet.")}</p> : null}{inspect.isPending ? <p role="status">{text("正在加载版本…", "Loading version…")}</p> : null}{data.versions.slice(0, versionLimit).map(v => <p key={v.artifact_id}>{v.created_at} · {v.operation} <button className="button button-secondary" disabled={inspect.isPending} onClick={() => inspect.mutate(v.artifact_id)}>{text("预览版本", "Preview")}</button></p>)}{data.versions.length > versionLimit ? <button className="button button-secondary" onClick={() => setVersionLimit(n => n + 20)}>{text("加载更早版本", "Load older versions")}</button> : null}{viewed ? <section className="draft-version-preview"><DraftReader content={viewed.content} /><button className="button button-secondary" disabled={restore.isPending || viewed.id === data.draft_artifact_id} onClick={() => restore.mutate(viewed.id)}>{text("确认恢复此版本", "Confirm restore")}</button></section> : null}</> : null}
      </div></section></div>
    </>}
  </>;
}
