import { useEffect, useRef, useState } from "react";
import { useLocalizedMessage } from "../../i18n/useLocalizedMessage";
import { LocalizedError } from "../../components/LocalizedError";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { MarkdownView } from "../../components/MarkdownView";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { CandidateComparison, CandidateEvidenceReview, CandidateReply, CandidateStatus, type DialogueCandidate, type DialogueParagraph } from "./ParagraphDialogue";
import { ChapterVersions } from "./ChapterVersions";
import { hasDraftScratch, useDraftScratch } from "./useDraftScratch";
import { EvidenceLinks } from "./EvidenceLinks";


export type DialogueSection = { section_id: string; title: string; paragraphs: DialogueParagraph[] };
type SectionTurn = { id: string; status: string; message: string; action?: string; streaming_reply?: string; phase?: string; progress_current: number; progress_total: number;
  branch_id?: string; initial_artifact_id?: string; error_message?: string; result?: { paragraph_results?: Record<string, { paragraph_id: string; status: string; reason?: string }> } };
type Candidate = DialogueCandidate & { batch_job_id?: string };

export function SectionDialogue({ section, projectId, userId, revision, blocked, candidates, activeTask,
  decisionPending, decide, refresh }: { section: DialogueSection; projectId: string; userId: string; revision: number;
  blocked: boolean; candidates: Candidate[]; activeTask?: { id: string; status: string };
  decisionPending: boolean; decide: (ids: string[], action: "accept" | "reject") => void; refresh: () => Promise<unknown> }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [streamConnected, setStreamConnected] = useState(false);
  const completedStreams = useRef(new Set<string>());
  const refreshRef = useRef(refresh); refreshRef.current = refresh;
  const scratch = `${userId}:${projectId}:section:${section.section_id}`;
  const [message, setMessage, storageFailed] = useDraftScratch(`${scratch}:message`, "");
  const hashes = Object.fromEntries(section.paragraphs.map(p => [p.paragraph_key, p.text_sha256]));
  const [baseHashes, setBaseHashes] = useDraftScratch(`${scratch}:base`, hashes);
  const [requestKey, setRequestKey] = useDraftScratch(`${scratch}:request`, newIdempotencyKey());
  const [showVersions, setShowVersions] = useState(false);
  const [branch, setBranch] = useDraftScratch(`${scratch}:branch`, { id: "", initialArtifactId: "" });
  const logRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const olderScroll = useRef<{ height: number; top: number } | null>(null);
  const [newReply, setNewReply] = useState(false);
  const [historyLimit, setHistoryLimit] = useState(5);
  const [localError, setLocalError] = useLocalizedMessage();
  const messageRef = useRef(message); messageRef.current = message;
  const endpoint = `/api/v1/projects/${encodeURIComponent(projectId)}/draft/section-dialogues/${encodeURIComponent(section.section_id)}`;
  const history = useQuery({ queryKey: ["draft-dialogue", projectId, "section", section.section_id],
    queryFn: () => apiRequest<{ turns: SectionTurn[] }>(endpoint),
    refetchInterval: q => !streamConnected && (activeTask || q.state.data?.turns.some(t => jobIsActive(t.status))) ? 5000 : false,
    refetchIntervalInBackground: true });
  const allTurns = history.data?.turns || [];
  useEffect(() => {
    const latest = allTurns[allTurns.length - 1];
    if (!branch.id && latest?.branch_id && latest.initial_artifact_id) {
      setBranch({ id: latest.branch_id, initialArtifactId: latest.initial_artifact_id });
    }
  }, [history.data, branch.id, setBranch]);
  const turns = allTurns.filter(t => (t.branch_id || "") === branch.id);
  const completedVersion = turns.filter(t => !jobIsActive(t.status)).map(t => `${t.id}:${t.status}`).join("|");
  const updateToken = turns.map(t => t.id + t.status + t.progress_current + t.streaming_reply).join("|") + candidates.map(c => c.candidate_id + c.status).join("|");
  useEffect(() => {
    const log = logRef.current;
    if (!log) return;
    if (olderScroll.current) {
      log.scrollTop = olderScroll.current.top + log.scrollHeight - olderScroll.current.height;
      olderScroll.current = null;
    } else if (atBottom.current) { log.scrollTop = log.scrollHeight; setNewReply(false); }
    else setNewReply(true);
  }, [updateToken, historyLimit]);
  const active = turns.find(t => jobIsActive(t.status)) || (activeTask && jobIsActive(activeTask.status) && !turns.some(t => t.id === activeTask.id && !jobIsActive(t.status)) ? activeTask : undefined);
  const activeId = active?.id;
  useEffect(() => {
    setStreamConnected(false);
    if (!activeId || completedStreams.current.has(activeId) || typeof EventSource === "undefined") return;
    const source = new EventSource(`${endpoint}/stream/${encodeURIComponent(activeId)}`);
    source.onopen = () => setStreamConnected(true);
    source.onerror = () => setStreamConnected(false); // EventSource reconnects; polling is only a fallback.
    source.addEventListener("snapshot", event => {
      const snapshot = JSON.parse((event as MessageEvent).data) as Partial<SectionTurn>;
      queryClient.setQueryData<{ turns: SectionTurn[] }>(["draft-dialogue", projectId, "section", section.section_id], old =>
        old ? { ...old, turns: old.turns.map(t => t.id === activeId ? { ...t, ...snapshot } : t) } : old);
      if (snapshot.status && !jobIsActive(snapshot.status)) {
        completedStreams.current.add(activeId);
        source.close(); setStreamConnected(false);
        void queryClient.invalidateQueries({ queryKey: ["draft-dialogue", projectId, "section", section.section_id] });
        void refreshRef.current().catch(() => undefined);
      }
    });
    source.addEventListener("done", () => { source.close(); setStreamConnected(false); });
    return () => source.close();
  }, [activeId, endpoint, projectId, section.section_id, queryClient]);
  const sync = async () => { await Promise.all([history.refetch(), refresh()]); };
  const send = useMutation({ mutationFn: ({ submitted, action }: { submitted: string; originalInput: string; action: "discuss" | "revise" }) => apiRequest(endpoint, { method: "POST",
    headers: { "Idempotency-Key": `${requestKey}:${action}` }, ...jsonBody({ message: submitted, base_hashes: message.trim() ? baseHashes : hashes,
      branch_id: branch.id, initial_artifact_id: branch.initialArtifactId, action }) }),
    onSuccess: async (_data, { originalInput }) => { if (messageRef.current === originalInput) setMessage(""); setRequestKey(newIdempotencyKey()); await sync(); } });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${active!.id}/cancel`, { method: "POST" }), onSuccess: sync });
  const changed = JSON.stringify(baseHashes) !== JSON.stringify(hashes);
  const error = history.error || send.error || cancel.error;
  const revise = (action: "discuss" | "revise" = "discuss") => {
    if (section.paragraphs.some(p => hasDraftScratch(`${userId}:${projectId}:${p.paragraph_key}:manual`))) {
      setLocalError(["本章还有未保存的手动编辑，请先保存或取消。", "Save or cancel this chapter's manual edits first."]); return;
    }
    setLocalError(""); send.mutate({ action, originalInput: message, submitted: message.trim() || text("根据本章此前讨论生成完整章节修改候选。修改讨论涉及的内容，其余保留。", "Generate a complete chapter candidate from our discussion. Revise relevant content and retain the rest.") });
  };

  const jumpToLatest = () => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
    atBottom.current = true; setNewReply(false);
  };
  return <section className="section-dialogue chapter-chat">
    <header className="chapter-chat-header">
      <div><span className="step-label">{section.section_id}</span><h2>{section.title}</h2></div>
      <button type="button" className="button button-secondary" onClick={() => setShowVersions(true)}>{text("查看正文", "View text")}</button>
    </header>
    {showVersions ? <ChapterVersions section={section} projectId={projectId} userId={userId} revision={revision}
      disabled={blocked || decisionPending || !!active || send.isPending} candidates={candidates} turns={allTurns} refresh={sync}
      close={() => setShowVersions(false)} restart={artifactId => {
        if (section.paragraphs.some(p => hasDraftScratch(`${userId}:${projectId}:${p.paragraph_key}:manual`))) {
          setLocalError(["本章还有未保存的手动编辑，请先保存或取消。", "Save or cancel this chapter's manual edits first."]); return;
        }
        setBranch({ id: newIdempotencyKey(), initialArtifactId: artifactId }); setBaseHashes(hashes);
        setRequestKey(newIdempotencyKey()); setMessage(""); setShowVersions(false); setLocalError("");
      }} /> : null}
    <div className="chapter-chat-body">
      <div className="chapter-chat-messages" ref={logRef} role="region" aria-label={text("章节聊天记录", "Chapter conversation")}
        onScroll={() => { const log = logRef.current; if (log) { atBottom.current = log.scrollHeight - log.scrollTop - log.clientHeight < 60; if (atBottom.current) setNewReply(false); } }}>
        {history.isPending ? <p role="status">{text("正在加载对话…", "Loading conversation…")}</p> : null}
        {!turns.length && !history.isPending ? <div className="chapter-chat-empty"><h3>{text("一起完善这一章", "Let's refine this chapter")}</h3><p>{text("直接告诉 AI 你的想法。它会参考本章正文与论文证据，回复建议或生成修改，保存由你决定。", "Share your ideas. The AI uses this chapter and source evidence to discuss or propose changes. You choose what to save.")}</p></div> : null}
        {turns.length > historyLimit ? <button className="button button-quiet" onClick={() => {
          const log = logRef.current; if (log) olderScroll.current = { height: log.scrollHeight, top: log.scrollTop };
          setHistoryLimit(n => n + 5);
        }}>{text("加载更早对话", "Load earlier conversations")}</button> : null}
        {turns.slice(-historyLimit).map(turn => {
          const items = candidates.filter(c => c.batch_job_id === turn.id);
          return <article className="chapter-chat-turn" key={turn.id}>
            <div className="chat-message chat-message-user"><span>{text("你", "You")}</span><p>{turn.message}</p></div>
            <TurnReply turn={turn} items={items} projectId={projectId} section={section} completedVersion={completedVersion}
              disabled={blocked || decisionPending} decide={decide} />
          </article>;
        })}
        {active && !turns.some(t => t.id === active.id) ? <p role="status">{text("任务已提交，正在等待回复…", "Request submitted. Waiting for a reply…")}</p> : null}
      </div>
      {newReply ? <button className="button button-secondary chat-new-reply" onClick={jumpToLatest}>{text("查看最新回复 ↓", "Latest reply ↓")}</button> : null}
    </div>
    <div className="chapter-chat-composer">
      {branch.id ? <p className="muted">{text("本轮讨论从初始版本开始。", "This discussion started from the initial version.")}</p> : null}
      <label className="sr-only" htmlFor={`chat-input-${section.section_id}`}>{text("告诉 AI 本章希望怎样改进", "Tell the AI how to improve this chapter")}</label>
      <textarea id={`chat-input-${section.section_id}`} rows={5} maxLength={12000} value={message}
        placeholder={text("输入问题或修改想法…", "Ask a question or describe your changes…")}
        onChange={e => { setMessage(e.target.value); setBaseHashes(hashes); setRequestKey(newIdempotencyKey()); }}
        onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !e.nativeEvent.isComposing && !blocked && !active && !send.isPending && message.trim() && !changed) { e.preventDefault(); revise(); } }} />
      <div className="chapter-chat-actions">
        <span className="muted">{text("修改不会自动保存", "Changes are not saved automatically")}</span>
        <button type="button" className="button button-secondary" disabled={blocked || !!active || send.isPending || (!!message && changed)} onClick={() => revise("revise")}>{text("根据讨论生成修改", "Generate revision from discussion")}</button>
        {active ? <button className="button button-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消本章任务", "Cancel chapter task")}</button> :
          <button className="button button-primary" disabled={blocked || send.isPending || !message.trim() || changed} onClick={() => revise()}>{text("发送", "Send")}</button>}
      </div>
      <p className="muted">{text("生成修改面向当前整章，讨论未涉及的内容保留；保存后才更新正文。", "Generate revision covers this entire chapter, retaining unrelated content. Save to update the draft.")}</p>
      {message && changed ? <p className="message message-warning">{text("本章正文发生变化，请核对最新内容。", "This chapter changed. Review the latest text.")} <button className="button button-secondary" onClick={() => { setBaseHashes(hashes); setRequestKey(newIdempotencyKey()); }}>{text("已核对", "Reviewed")}</button></p> : null}
      {blocked ? <p className="message message-warning">{text("上游内容已变化，请先核对初稿。", "Upstream content changed. Review the draft first.")}</p> : null}
      {error || localError ? <p role="alert" className="message message-error">{error ? <LocalizedError error={error} /> : localError}</p> : null}
      {storageFailed ? <p role="alert">{text("浏览器暂存不可用，请保留输入。", "Browser storage unavailable; preserve your input.")}</p> : null}
    </div>
  </section>;
}

function TurnReply({ turn, items, projectId, section, completedVersion, disabled, decide }: {
  turn: SectionTurn; items: Candidate[]; projectId: string; section: DialogueSection; completedVersion: string; disabled: boolean;
  decide: (ids: string[], action: "accept" | "reject") => void;
}) {
  const { text } = useUiText();
  const active = jobIsActive(turn.status);
  const retainedReply = useRef("");
  if (turn.streaming_reply) retainedReply.current = turn.streaming_reply;
  // A terminal SSE/history status does not mean the separate draft cache has caught up.
  // Read the published candidates after observing completion, including after a reload
  // or polling fallback. Retrying this query never submits another generation task.
  const result = useQuery({
    queryKey: ["draft-dialogue", projectId, "section-result", section.section_id, completedVersion],
    queryFn: () => apiRequest<{ rewrite_candidates: Candidate[] }>(`/api/v1/projects/${encodeURIComponent(projectId)}/draft`),
    enabled: !active,
    retry: false,
  });
  const published = result.data?.rewrite_candidates?.filter(c => c.batch_job_id === turn.id) || [];
  const resolved = [...new Map([...items, ...published].map(c => [c.candidate_id, c])).values()];
  const waiting = active || result.isPending || result.isFetching;
  return <div className="chat-message chat-message-assistant"><span>AI</span>
    {waiting ? <p role="status">{text("正在思考…", "Thinking…")}</p> : null}
    {!resolved.length && retainedReply.current && (waiting || result.isError) ? <div className="chat-reply-text">
      <small>{text("正在生成，尚未校验或保存", "Generating; not validated or saved")}</small>
      <p style={{ whiteSpace: "pre-wrap" }}>{retainedReply.current}</p>
    </div> : null}
    {turn.error_message ? <p className="message message-error">{turn.error_message}</p> : null}
    {Object.values(turn.result?.paragraph_results || {}).filter(r => r.status !== "completed").map(r => <p key={r.paragraph_id} className="message message-warning">{r.paragraph_id} · {r.reason || r.status}</p>)}
    {!active && result.isError && !result.isFetching ? <div role="alert">
      <p>{text("回复加载失败，请重试。", "Could not load the reply. Please retry.")}</p>
      <button type="button" className="button button-secondary" onClick={() => void result.refetch()}>{text("重新加载回复", "Reload reply")}</button>
    </div> : null}
    {!resolved.length && !waiting && result.isSuccess && !turn.error_message ? <p>{text("本轮未生成回复，可继续说明你的要求。", "No reply was generated for this turn. You can continue the discussion.")}</p> : null}
    {resolved.length ? <ChapterReply items={resolved} section={turn.action === "revise" ? section : undefined}
      disabled={disabled || waiting || result.isError} decide={decide} /> : null}
  </div>;
}

function ChapterReply({ items, section, disabled, decide }: { items: Candidate[]; section?: DialogueSection; disabled: boolean;
  decide: (ids: string[], action: "accept" | "reject") => void }) {
  const { text } = useUiText();
  const changes = items.filter(c => c.candidate_text);
  const pending = changes.filter(c => c.status === "pending");
  const ordered = section?.paragraphs.map(p => items.find(c => c.paragraph_key === p.paragraph_key));
  return <>
    {items.map(c => <div key={c.candidate_id} className="chat-reply-text">
      {items.length > 1 ? <small>{c.paragraph_id}</small> : null}<CandidateReply candidate={c} />
      <CandidateEvidenceReview candidate={c} />
      {c.validation_errors?.length && c.rejected_candidate_text ? <details><summary>{text("查看未通过技术校验的文本（不可保存）", "Inspect blocked text (read-only)")}</summary><MarkdownView content={c.rejected_candidate_text} /></details> : null}
      <EvidenceLinks sources={c.sources} legacyRefs={c.source_refs} />
    </div>)}
    {ordered ? <details open><summary>{text("完整章节候选预览", "Complete chapter candidate preview")}</summary>
      {ordered.some(c => !c) ? <p role="status">{text("部分段落尚未完成，以下不是完整候选。", "Some paragraphs are not complete; this preview is incomplete.")}</p> : null}
      {ordered.some(c => c?.validation_errors?.length) ? <p role="alert">{text("部分修改未通过技术校验，对应位置显示原文，并非已完成的修改。", "Some edits failed technical validation; their positions show original text, not completed revisions.")}</p> : null}
      <MarkdownView content={`## ${section!.title}\n\n` + ordered.filter((c): c is Candidate => !!c).map(c => c.candidate_text || c.original_text).join("\n\n")} />
    </details> : null}
    {changes.length ? <div className="chat-change-card">
      <strong>{text(`本轮提出 ${changes.length} 段修改`, `${changes.length} paragraph changes proposed`)}</strong>
      <span className="muted">{pending.length ? text(` · ${pending.length} 段待确认`, ` · ${pending.length} pending`) : text(" · 已处理或仅供查看", " · Handled or read-only")}</span>
      {!section ? changes.map(c => <section key={c.candidate_id}>
        <h4>{c.paragraph_id} · <CandidateStatus status={c.status} /></h4>
        <MarkdownView content={c.candidate_text} />
      </section>) : null}
      {pending.length ? <p className="muted">{text("以上是修改候选，尚未写入正文。点击“保存修改”后生效。", "These are proposals, not saved text. Click Save changes to update the draft.")}</p> : null}
      <details><summary>{text("查看对比", "View comparison")}</summary>
        {changes.map(c => <div key={c.candidate_id}><h4>{c.paragraph_id}</h4><CandidateComparison candidate={c} disabled={disabled} decide={(id, action) => decide([id], action)} /></div>)}
      </details>
      {pending.length ? <div className="button-row">
        <button className="button button-primary" disabled={disabled} onClick={() => decide(pending.map(c => c.candidate_id), "accept")}>{text("保存修改", "Save changes")}</button>
        <button className="button button-secondary" disabled={disabled} onClick={() => decide(pending.map(c => c.candidate_id), "reject")}>{text("放弃", "Discard")}</button>
      </div> : null}
    </div> : null}
  </>;
}
