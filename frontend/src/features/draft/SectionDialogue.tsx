import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { MarkdownView } from "../../components/MarkdownView";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { CandidateComparison, CandidateEvidenceReview, CandidateReply, CandidateStatus, ParagraphDialogue, type DialogueCandidate, type DialogueParagraph } from "./ParagraphDialogue";
import { ParagraphManualEditor } from "./ParagraphManualEditor";
import { hasDraftScratch, useDraftScratch } from "./useDraftScratch";
import { EvidenceLinks } from "./EvidenceLinks";


export type DialogueSection = { section_id: string; title: string; paragraphs: DialogueParagraph[] };
type SectionTurn = { id: string; status: string; message: string; action?: string; streaming_reply?: string; phase?: string; progress_current: number; progress_total: number;
  error_message?: string; result?: { paragraph_results?: Record<string, { paragraph_id: string; status: string; reason?: string }> } };
type Candidate = DialogueCandidate & { batch_job_id?: string };

export function SectionDialogue({ section, projectId, userId, revision, blocked, candidates, activeTask, initialTarget,
  decisionPending, decide, refresh }: { section: DialogueSection; projectId: string; userId: string; revision: number;
  blocked: boolean; candidates: Candidate[]; activeTask?: { id: string; status: string }; initialTarget?: string;
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
  const [target, setTarget] = useState("");
  useEffect(() => { setTarget(initialTarget || ""); }, [initialTarget]);
  const [useSaved, setUseSaved] = useState(false);
  const [showText, setShowText] = useState(false);
  const [showOptions, setShowOptions] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const olderScroll = useRef<{ height: number; top: number } | null>(null);
  const [newReply, setNewReply] = useState(false);
  const [legacy, setLegacy] = useState("");
  const [historyLimit, setHistoryLimit] = useState(5);
  const [localError, setLocalError] = useState("");
  const messageRef = useRef(message); messageRef.current = message;
  const endpoint = `/api/v1/projects/${encodeURIComponent(projectId)}/draft/section-dialogues/${encodeURIComponent(section.section_id)}`;
  const history = useQuery({ queryKey: ["draft-dialogue", projectId, "section", section.section_id],
    queryFn: () => apiRequest<{ turns: SectionTurn[] }>(endpoint),
    refetchInterval: q => !streamConnected && (activeTask || q.state.data?.turns.some(t => jobIsActive(t.status))) ? 5000 : false,
    refetchIntervalInBackground: true });
  const turns = history.data?.turns || [];
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
  const active = turns.find(t => jobIsActive(t.status)) || activeTask;
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
        void refreshRef.current();
      }
    });
    source.addEventListener("done", () => { source.close(); setStreamConnected(false); });
    return () => source.close();
  }, [activeId, endpoint, projectId, section.section_id, queryClient]);
  const sync = async () => { await Promise.all([history.refetch(), refresh()]); };
  const send = useMutation({ mutationFn: ({ submitted, action }: { submitted: string; originalInput: string; action: "discuss" | "revise" }) => apiRequest(endpoint, { method: "POST",
    headers: { "Idempotency-Key": `${requestKey}:${action}` }, ...jsonBody({ message: submitted, base_hashes: message.trim() ? baseHashes : hashes,
      paragraph_keys: action === "revise" ? [] : target ? [target] : [], use_saved: useSaved, action }) }),
    onSuccess: async (_data, { originalInput }) => { if (messageRef.current === originalInput) setMessage(""); setRequestKey(newIdempotencyKey()); await sync(); } });
  const cancel = useMutation({ mutationFn: () => apiRequest(`/api/v1/jobs/${active!.id}/cancel`, { method: "POST" }), onSuccess: sync });
  const changed = JSON.stringify(baseHashes) !== JSON.stringify(hashes);
  const error = history.error || send.error || cancel.error;
  const revise = (action: "discuss" | "revise" = "discuss") => {
    if (section.paragraphs.some(p => hasDraftScratch(`${userId}:${projectId}:${p.paragraph_key}:manual`))) {
      setLocalError(text("本章还有未保存的手动编辑，请先保存或取消。", "Save or cancel this chapter's manual edits first.")); return;
    }
    setLocalError(""); send.mutate({ action, originalInput: message, submitted: message.trim() || text("根据本章此前讨论生成完整章节修改候选。修改讨论涉及的内容，其余保留。", "Generate a complete chapter candidate from our discussion. Revise relevant content and retain the rest.") });
  };

  const legacyCandidates = candidates.filter(c => !turns.some(t => t.id === c.batch_job_id));
  const jumpToLatest = () => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
    atBottom.current = true; setNewReply(false);
  };
  return <section className="section-dialogue chapter-chat">
    <header className="chapter-chat-header">
      <div><span className="step-label">{section.section_id}</span><h2>{section.title}</h2></div>
      <button type="button" className="button button-secondary" aria-expanded={showText} onClick={() => setShowText(!showText)}>
        {showText ? text("返回对话", "Back to conversation") : text("正文与手动编辑", "Text and manual editing")}
      </button>
    </header>
    <div hidden={!showText} className="chapter-text-panel">
      {section.paragraphs.map(p => <article className="dialogue-saved-paragraph" key={p.paragraph_key}>
        <span className="step-label">{p.paragraph_id}</span><MarkdownView content={p.text} />
        <div className="button-row"><ParagraphManualEditor scratchKey={`${userId}:${projectId}:${p.paragraph_key}:manual`}
          projectId={projectId} paragraph={p} revision={revision} disabled={blocked || decisionPending} refresh={sync} />
          <button className="button button-quiet" onClick={() => { setTarget(p.paragraph_key); setShowText(false); setRequestKey(newIdempotencyKey()); }}>{text("针对这段讨论", "Discuss this paragraph")}</button>
        </div>
      </article>)}
    </div>
    <div hidden={showText} className="chapter-chat-body">
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
            <div className="chat-message chat-message-assistant"><span>AI</span>
              {jobIsActive(turn.status) ? <div role="status"><p>{turn.phase === "scope" ? text("正在确定讨论范围", "Identifying discussion scope") : turn.phase === "answer" ? text("正在生成回答", "Generating answer") : turn.phase === "checking" ? text("正在整理与检查结果", "Checking the result") : text("正在排队或查阅证据", "Waiting or reading evidence")} · {turn.progress_current}/{turn.progress_total}</p><progress max={turn.progress_total || 1} value={turn.progress_current} /></div> : null}
              {jobIsActive(turn.status) && turn.streaming_reply ? <div className="chat-reply-text"><small>{text("正在生成，尚未校验或保存", "Generating; not validated or saved")}</small><p style={{ whiteSpace: "pre-wrap" }}>{turn.streaming_reply}</p></div> : null}
              {turn.error_message ? <p className="message message-error">{turn.error_message}</p> : null}
              {Object.values(turn.result?.paragraph_results || {}).filter(r => r.status !== "completed").map(r => <p key={r.paragraph_id} className="message message-warning">{r.paragraph_id} · {r.reason || r.status}</p>)}
              {!items.length && !jobIsActive(turn.status) && !turn.error_message ? <p>{text("本轮已结束，暂无可用修改。可继续说明你的要求。", "This turn ended without available changes. You can continue the discussion.")}</p> : null}
              {items.length ? <ChapterReply items={items} section={turn.action === "revise" ? section : undefined} disabled={blocked || decisionPending || jobIsActive(turn.status)} decide={decide} /> : null}
            </div>
          </article>;
        })}
        {active && !turns.some(t => t.id === active.id) ? <p role="status">{text("任务已提交，正在等待回复…", "Request submitted. Waiting for a reply…")}</p> : null}
      </div>
      {newReply ? <button className="button button-secondary chat-new-reply" onClick={jumpToLatest}>{text("查看最新回复 ↓", "Latest reply ↓")}</button> : null}
    </div>
    <div className="chapter-chat-composer">
      {target ? <p className="chat-target">{text("本次讨论：", "Discussing: ")}{section.paragraphs.find(p => p.paragraph_key === target)?.paragraph_id}
        <button className="button button-quiet" onClick={() => { setTarget(""); setRequestKey(newIdempotencyKey()); }}>{text("改为整章", "Whole chapter")}</button></p> : null}
      {useSaved ? <p className="muted">{text("本次从已保存正文重新开始", "Restarting from saved text")}</p> : null}
      <label className="sr-only" htmlFor={`chat-input-${section.section_id}`}>{text("告诉 AI 本章希望怎样改进", "Tell the AI how to improve this chapter")}</label>
      <textarea id={`chat-input-${section.section_id}`} rows={3} maxLength={12000} value={message}
        placeholder={text("输入问题或修改想法…", "Ask a question or describe your changes…")}
        onChange={e => { setMessage(e.target.value); setBaseHashes(hashes); setRequestKey(newIdempotencyKey()); }}
        onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !e.nativeEvent.isComposing && !blocked && !active && !send.isPending && message.trim() && !changed) { e.preventDefault(); revise(); } }} />
      <div className="chapter-chat-actions">
        <button type="button" className="button button-quiet" aria-expanded={showOptions} onClick={() => setShowOptions(!showOptions)}>{text("更多选项", "More options")}</button>
        <span className="muted">{text("修改不会自动保存", "Changes are not saved automatically")}</span>
        <button type="button" className="button button-secondary" disabled={blocked || !!active || send.isPending || (!!message && changed)} onClick={() => revise("revise")}>{text("根据讨论生成修改", "Generate revision from discussion")}</button>
        {active ? <button className="button button-secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消本章任务", "Cancel chapter task")}</button> :
          <button className="button button-primary" disabled={blocked || send.isPending || !message.trim() || changed} onClick={() => revise()}>{text("发送", "Send")}</button>}
      </div>
      <p className="muted">{text("生成修改面向当前整章，讨论未涉及的内容保留；保存后才更新正文。", "Generate revision covers this entire chapter, retaining unrelated content. Save to update the draft.")}</p>
      {showOptions ? <div className="chapter-chat-options">
        <label>{text("修改范围", "Revision scope")}<select value={target} onChange={e => { setTarget(e.target.value); setRequestKey(newIdempotencyKey()); }}>
          <option value="">{text("当前章节（按需修改）", "Current chapter (revise as needed)")}</option>
          {section.paragraphs.map(p => <option key={p.paragraph_key} value={p.paragraph_key}>{p.paragraph_id}</option>)}
        </select></label>
        <label className="check-label"><input type="checkbox" checked={useSaved} onChange={e => { setUseSaved(e.target.checked); setRequestKey(newIdempotencyKey()); }} />{text("从已保存正文重新讨论（默认接续待确认候选）", "Restart from saved text (otherwise continue pending candidates)")}</label>
        {legacyCandidates.length ? <details><summary>{text("其他已保留候选", "Other retained candidates")}</summary><ChapterReply items={legacyCandidates} disabled={blocked || decisionPending} decide={decide} /></details> : null}
        <details><summary>{text("查看旧段落对话", "Previous paragraph conversations")}</summary>
          <select aria-label={text("旧对话段落", "Previous conversation paragraph")} value={legacy} onChange={e => setLegacy(e.target.value)}><option value="">{text("选择段落", "Select a paragraph")}</option>{section.paragraphs.map(p => <option key={p.paragraph_key} value={p.paragraph_key}>{p.paragraph_id}</option>)}</select>
          {section.paragraphs.filter(p => p.paragraph_key === legacy).map(p => <ParagraphDialogue key={p.paragraph_key} projectId={projectId} paragraph={p} />)}
        </details>
      </div> : null}
      {message && changed ? <p className="message message-warning">{text("本章正文发生变化，请核对最新内容。", "This chapter changed. Review the latest text.")} <button className="button button-secondary" onClick={() => { setBaseHashes(hashes); setRequestKey(newIdempotencyKey()); }}>{text("已核对", "Reviewed")}</button></p> : null}
      {blocked ? <p className="message message-warning">{text("上游内容已变化，请先核对初稿。", "Upstream content changed. Review the draft first.")}</p> : null}
      {error || localError ? <p role="alert" className="message message-error">{error?.message || localError}</p> : null}
      {storageFailed ? <p role="alert">{text("浏览器暂存不可用，请保留输入。", "Browser storage unavailable; preserve your input.")}</p> : null}
    </div>
  </section>;
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
