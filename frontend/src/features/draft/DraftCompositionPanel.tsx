import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { MarkdownView } from "../../components/MarkdownView";

export type SynthesisJob = { id: string; status: string; error_message?: string; result?: { candidate_ids?: string[]; warnings?: unknown[]; candidate_pending?: boolean; overview_artifact_id?: string } };
type Candidate = { candidate_id: string; paragraph_key: string; original_text: string; candidate_text: string; status: string; base_text_sha256?: string; created_at?: string };
type ImageVersion = { id: string; url: string; title: string; instructions: string; created_at: string; selected: boolean; source_changed: boolean };
type Overview = { revision: number; overview_figure_exists?: boolean; overview_text?: { title?: string }; history?: ImageVersion[]; job?: SynthesisJob };
type Kind = "abstract" | "conclusion" | "overview";

export function DraftCompositionPanel({ projectId, markdown, job, refresh, candidates = [], captionOpen = 0 }: {
  projectId: string; markdown: string; job?: SynthesisJob | null; refresh: () => Promise<unknown>;
  candidates?: Candidate[]; captionOpen?: number;
}) {
  const { text } = useUiText();
  const client = useQueryClient();
  const base = "/api/v1/projects/" + encodeURIComponent(projectId) + "/draft";
  const [kind, setKind] = useState<Kind | null>(null);
  const [imageId, setImageId] = useState("");
  const [instructions, setInstructions] = useState("");
  const [caption, setCaption] = useState("");
  const [edited, setEdited] = useState("");
  const [notice, setNotice] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const overview = useQuery({ queryKey: ["draft-overview", projectId], queryFn: () => apiRequest<Overview>(base + "/overview"),
    refetchInterval: q => jobIsActive(q.state.data?.job?.status) ? 3000 : false });
  useEffect(() => { if (captionOpen) setKind("overview"); }, [captionOpen]);
  useEffect(() => { if (kind) dialog.current?.showModal(); else dialog.current?.close(); }, [kind]);
  useEffect(() => {
    if (overview.data?.overview_figure_exists) void client.invalidateQueries({ queryKey: ["draft", projectId] });
  }, [client, projectId, overview.data?.revision, overview.data?.overview_figure_exists]);
  const history = overview.data?.history || [];
  const selected = history.find(v => v.id === imageId);
  const candidate = candidates.filter(c => c.paragraph_key === "synthesis:" + kind && c.status === "pending")
    .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || "")).at(-1);
  useEffect(() => { setEdited(candidate?.candidate_text || ""); }, [candidate?.candidate_id]);
  const chooseImage = (v: ImageVersion) => { setImageId(v.id); setCaption(v.selected ? overview.data?.overview_text?.title || v.title : v.title); setInstructions(v.instructions || ""); setNotice(""); };
  useEffect(() => {
    const newest = overview.data?.job?.result?.overview_artifact_id;
    const found = history.find(v => v.id === newest) || history.find(v => v.selected) || history[0];
    if (found) chooseImage(found);
  // Switch when a new generation completes, not on each polling response.
  }, [overview.data?.job?.result?.overview_artifact_id, Boolean(overview.data), projectId]);
  const run = useMutation({ mutationFn: (target: Kind) => apiRequest(base + (target === "overview" ? "/overview-jobs" : "/synthesis/" + target),
    { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...(target === "overview" ? jsonBody({ instructions }) : {}) }),
    onSuccess: async () => { setNotice(text("已提交，结果会保留；保存后才更新正文。", "Submitted. Results are retained; save to update the manuscript.")); await refresh(); await overview.refetch(); } });
  const save = useMutation({ mutationFn: async () => {
    if (kind === "overview") return apiRequest(base + "/overview/adopt", { method: "POST", ...jsonBody({ image_id: imageId, title: caption, revision: overview.data!.revision }) });
    if (!candidate) throw new Error(text("请先生成候选", "Generate a candidate first"));
    return apiRequest(base + "/synthesis-candidates/" + candidate.candidate_id + "/accept", { method: "POST", ...jsonBody({ text: edited, base_text_sha256: candidate.base_text_sha256 }) });
  }, onSuccess: async () => { setNotice(text("已保存到正文", "Saved to manuscript")); await refresh(); await overview.refetch(); } });
  const active = kind === "overview" ? overview.data?.job : job;
  const busy = run.isPending || jobIsActive(active?.status);
  const cancel = useMutation({ mutationFn: () => apiRequest("/api/v1/jobs/" + active!.id + "/cancel", { method: "POST" }), onSuccess: async () => { await refresh(); await overview.refetch(); } });
  const label = kind === "overview" ? text("总览图", "Overview") : kind === "abstract" ? text("摘要", "Abstract") : text("结论", "Conclusion");
  const open = (target: Kind) => { setNotice(""); run.reset(); save.reset(); cancel.reset(); setKind(target); if (target === "overview" && !selected && history[0]) chooseImage(history.find(v => v.selected) || history[0]); };
  const close = () => {
    if (candidate && edited !== candidate.candidate_text && !window.confirm(text("尚有未保存的文字修改，确定关闭？", "Discard unsaved text edits and close?"))) return;
    setKind(null);
  };
  return <section className="draft-composition-panel">
    <div className="button-row" aria-label={text("完善正文", "Complete manuscript")}>
      <button className="button button-secondary" disabled={!markdown} onClick={() => open("abstract")}>{text("摘要", "Abstract")}</button>
      <button className="button button-secondary" disabled={!markdown} onClick={() => open("conclusion")}>{text("结论", "Conclusion")}</button>
      <button className="button button-secondary" disabled={!markdown} onClick={() => open("overview")}>{text("总览图", "Overview")}</button>
    </div>
    <dialog ref={dialog} className="draft-composition-dialog" aria-label={label} onCancel={e => { e.preventDefault(); close(); }}>
      {kind && <><header><h2>{label}</h2><button className="button button-quiet" onClick={close}>{text("关闭", "Close")}</button></header>
        <p className="muted">{text("生成结果不会自动覆盖正文。关闭窗口不取消后台任务。", "Generation does not replace saved text. Closing this window does not cancel the task.")}</p>
        {kind === "overview" ? <div className="draft-overview-workspace">
          <div className="draft-overview-preview">{selected ? <img src={selected.url} alt={text("总览图预览", "Overview preview")} /> : <p>{text("尚无总览图，在右侧填写要求后生成。", "No overview yet. Enter preferences and generate.")}</p>}</div>
          <aside><label>{text("本次生成要求（可选）", "Generation preferences (optional)")}<textarea rows={5} maxLength={4000} value={instructions} onChange={e => setInstructions(e.target.value)} /></label>
            <label>{text("图注（不修改图内文字）", "Caption (does not change image text)")}<textarea rows={3} value={caption} onChange={e => setCaption(e.target.value)} /></label>
            {selected?.source_changed && <p role="status">{text("这张图基于较早正文生成，请确认仍适用后保存。", "This image uses an earlier manuscript. Confirm it still applies before saving.")}</p>}
            <h3>{text("生成历史", "Generation history")}</h3><div className="draft-overview-history">{history.map(v => <button key={v.id} className={v.id === imageId ? "selected" : ""} aria-pressed={v.id === imageId} onClick={() => chooseImage(v)}><img src={v.url} alt="" /><span>{v.created_at ? new Date(v.created_at).toLocaleString() : text("历史版本", "Version")}{v.selected ? text(" · 正文使用中", " · In manuscript") : ""}</span></button>)}</div>
          </aside></div> : <div className="draft-synthesis-comparison"><section><h3>{text("当前正文", "Current text")}</h3><MarkdownView content={currentSection(markdown, kind)} /></section><label>{text("新结果（可直接编辑）", "New result (editable)")}<textarea rows={16} value={edited} onChange={e => setEdited(e.target.value)} disabled={!candidate} placeholder={text("点击生成，结果将在这里显示", "Generate to preview a new version here")} /></label></div>}
        {busy && <p role="status">{active?.status === "queued" ? text("排队中…", "Queued…") : text("正在生成，完成后将在此展示…", "Generating; the result will appear here…")}</p>}
        {notice && <p role="status">{notice}</p>}
        {active?.status === "failed" && <p role="alert">{active.error_message}</p>}
        {(run.error || save.error || overview.error || cancel.error) && <p role="alert">{(run.error || save.error || overview.error || cancel.error)?.message}</p>}
        <footer><button className="button button-secondary" disabled={busy || save.isPending} onClick={() => run.mutate(kind)}>{text("生成新版本", "Generate new version")}</button>
          {jobIsActive(active?.status) && <button className="button button-quiet" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{text("取消生成", "Cancel generation")}</button>}
          <button className="button button-primary" disabled={save.isPending || (kind === "overview" ? !selected || !caption.trim() : !candidate || !edited.trim())} onClick={() => save.mutate()}>{save.isPending ? text("保存中…", "Saving…") : text("保存到正文", "Save to manuscript")}</button></footer>
      </>}
    </dialog>
  </section>;
}

function currentSection(markdown: string, kind: string) {
  const title = kind === "abstract" ? "Abstract|摘要" : "Conclusion|Conclusions|结论|总结";
  return markdown.match(new RegExp("^##\\s+(?:" + title + ")\\s*\\n([\\s\\S]*?)(?=^##\\s|$(?![\\s\\S]))", "im"))?.[1] || "—";
}
