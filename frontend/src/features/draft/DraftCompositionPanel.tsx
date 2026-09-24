import { uiLocale } from "../../i18n/locale";
import { useLocalizedMessage } from "../../i18n/useLocalizedMessage";
import { LocalizedError } from "../../components/LocalizedError";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import { jobIsActive } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";
import { MarkdownView } from "../../components/MarkdownView";

export type SynthesisJob = { id: string; status: string; error_message?: string; result?: { candidate_ids?: string[]; warnings?: unknown[]; candidate_pending?: boolean; overview_artifact_id?: string } };
type Candidate = { candidate_id: string; paragraph_key: string; original_text: string; candidate_text: string; status: string; base_text_sha256?: string; created_at?: string };
type StructureReference = { kind: "molecule" | "reaction"; label: string; name: string; smiles: string; role: string; conditions: string };
type ImageVersion = { id: string; url: string; title: string; instructions: string; created_at: string; selected: boolean; source_changed: boolean; structure_references?: StructureReference[] };
type Overview = { revision: number; overview_figure_exists?: boolean; overview_text?: { title?: string }; history?: ImageVersion[]; job?: SynthesisJob };
type Kind = "abstract" | "conclusion" | "overview";

export function DraftSynthesisStatus({ job, assembling = false }: { job?: SynthesisJob | null; assembling?: boolean }) {
  const { text } = useUiText();
  const active = jobIsActive(job?.status);
  if (!assembling && !active && !["failed", "cancelled", "interrupted"].includes(job?.status || "")) return null;
  return <section className="draft-batch-status" role="status" aria-live="polite" aria-busy={assembling || active}>
    <strong>{assembling ? text("正在组装初稿", "Assembling draft")
      : active ? (job?.status === "queued" ? text("摘要 / 结论 · 等待生成", "Abstract / Conclusion · Queued")
        : job?.status === "cancel_requested" ? text("摘要 / 结论 · 正在取消", "Abstract / Conclusion · Cancelling")
        : text("摘要 / 结论 · 正在生成", "Abstract / Conclusion · Generating"))
      : text("摘要 / 结论 · 尚未完成", "Abstract / Conclusion · Incomplete")}</strong>
    {assembling || active ? <><progress aria-label={text("初稿完善进度", "Draft preparation progress")} />
      <p>{text("首次组装会自动补齐缺失的摘要和结论，请勿重复生成。你可以继续阅读正文，完成后页面会自动更新。", "First assembly automatically fills missing Abstract and Conclusion. No need to generate again. You can read the manuscript while this page updates automatically.")}</p></>
      : <p>{text("已有正文保留。点击“摘要”或“结论”查看结果或重新生成。", "Existing text is retained. Open Abstract or Conclusion to inspect results or retry.")}</p>}
  </section>;
}

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
  const [references, setReferences] = useState<StructureReference[]>([]);
  const updateReference = (index: number, changes: Partial<StructureReference>) => setReferences(rows => rows.map((row, i) => i === index ? { ...row, ...changes } : row));
  const [caption, setCaption] = useState("");
  const [edited, setEdited] = useState("");
  const [notice, setNotice] = useLocalizedMessage();
  useEffect(() => { setReferences([]); setImageId(""); setInstructions(""); setCaption(""); }, [projectId]);
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
  const chooseImage = (v: ImageVersion) => { setImageId(v.id); setCaption(v.selected ? overview.data?.overview_text?.title || v.title : v.title); setInstructions(v.instructions || ""); setReferences(v.structure_references || []); setNotice(""); };
  useEffect(() => {
    const newest = overview.data?.job?.result?.overview_artifact_id;
    const found = history.find(v => v.id === newest) || history.find(v => v.selected) || history[0];
    if (found) chooseImage(found);
  // Switch when a new generation completes, not on each polling response.
  }, [overview.data?.job?.result?.overview_artifact_id, Boolean(overview.data), projectId]);
  const run = useMutation({ mutationFn: (target: Kind) => apiRequest(base + (target === "overview" ? "/overview-jobs" : "/synthesis/" + target),
    { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() }, ...(target === "overview" ? jsonBody({ instructions, structure_references: references }) : {}) }),
    onSuccess: async () => { setNotice(["已提交，结果会保留；保存后才更新正文。", "Submitted. Results are retained; save to update the manuscript."]); await refresh(); await overview.refetch(); } });
  const save = useMutation({ mutationFn: async () => {
    if (kind === "overview") return apiRequest(base + "/overview/adopt", { method: "POST", ...jsonBody({ image_id: imageId, title: caption, revision: overview.data!.revision }) });
    if (!candidate) throw new Error(text("请先生成候选", "Generate a candidate first"));
    return apiRequest(base + "/synthesis-candidates/" + candidate.candidate_id + "/accept", { method: "POST", ...jsonBody({ text: edited, base_text_sha256: candidate.base_text_sha256 }) });
  }, onSuccess: async () => { setNotice(["已保存到正文", "Saved to manuscript"]); await refresh(); await overview.refetch(); } });
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
            <details className="overview-structure-references"><summary>{text("结构参考（可选）", "Structure references (optional)")}{references.length ? ` · ${references.length}` : ""}</summary>
              <p className="muted">{text("不填写时按文献自动生成。参考只用于新版本，不改变当前图片，也不自动作为文献证据。图像模型可能重画结构，请在保存前检查。", "Leave empty to use project evidence. References apply to new versions, not the current image, and are not literature evidence. Check structures before saving; image models may redraw them.")}</p>
              {references.map((ref, index) => <fieldset key={index} disabled={busy}>
                <legend>{text("参考", "Reference")} {index + 1}</legend>
                <label>{text("类型", "Type")}<select value={ref.kind} onChange={e => updateReference(index, { kind: e.target.value as StructureReference["kind"], smiles: "", role: "reference" })}><option value="molecule">{text("单个分子", "Molecule")}</option><option value="reaction">{text("反应式", "Reaction")}</option></select></label>
                <label>{text("分类／显示名称", "Category / display label")}<input maxLength={100} value={ref.label} onChange={e => updateReference(index, { label: e.target.value })} /></label>
                <label>{text("物质或反应名称", "Molecule or reaction name")}<input maxLength={200} value={ref.name} onChange={e => updateReference(index, { name: e.target.value })} /></label>
                {ref.kind === "molecule" && <label>{text("结构角色", "Structure role")}<select value={ref.role} onChange={e => updateReference(index, { role: e.target.value })}>{[["reference", text("参考结构", "Reference")], ["substrate", text("底物", "Substrate")], ["product", text("产物", "Product")], ["catalyst", text("催化剂", "Catalyst")], ["ligand", text("配体", "Ligand")]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}
                <label>{ref.kind === "reaction" ? text("Reaction SMILES（反应物>>产物）", "Reaction SMILES (reactants>>products)") : "SMILES"}<textarea rows={2} maxLength={3000} value={ref.smiles} onChange={e => updateReference(index, { smiles: e.target.value })} /></label>
                <small className="muted">{ref.kind === "reaction" ? text("必须提供反应物和产物；系统不会从底物推测产物。", "Both reactants and products are required; products are never inferred.") : text("名称与 SMILES 至少填一项；无法确认的名称仅作为文字参考。", "Provide a name or SMILES; unconfirmed names remain text-only references.")}</small>
                {ref.kind === "reaction" && <label>{text("反应条件（可选）", "Conditions (optional)")}<input maxLength={500} value={ref.conditions} onChange={e => updateReference(index, { conditions: e.target.value })} /></label>}
                <button type="button" className="button button-quiet" onClick={() => setReferences(rows => rows.filter((_, i) => i !== index))}>{text("移除参考", "Remove reference")}</button>
              </fieldset>)}
              <button type="button" className="button button-secondary" disabled={busy || references.length >= 4} onClick={() => setReferences(rows => [...rows, { kind: "molecule", label: "", name: "", smiles: "", role: "reference", conditions: "" }])}>{text("添加结构参考", "Add structure reference")}</button>
              <small className="muted">{text("最多 4 条；留空的条目请移除。", "Up to 4 references; remove unused empty entries.")}</small>
            </details>
            <label>{text("图注（不修改图内文字）", "Caption (does not change image text)")}<textarea rows={3} value={caption} onChange={e => setCaption(e.target.value)} /></label>
            {selected?.source_changed && <p role="status">{text("这张图基于较早正文生成，请确认仍适用后保存。", "This image uses an earlier manuscript. Confirm it still applies before saving.")}</p>}
            <h3>{text("生成历史", "Generation history")}</h3><div className="draft-overview-history">{history.map(v => <button key={v.id} className={v.id === imageId ? "selected" : ""} aria-pressed={v.id === imageId} onClick={() => chooseImage(v)}><img src={v.url} alt="" /><span>{v.created_at ? new Date(v.created_at).toLocaleString(uiLocale()) : text("历史版本", "Version")}{v.selected ? text(" · 正文使用中", " · In manuscript") : ""}</span></button>)}</div>
          </aside></div> : <div className="draft-synthesis-comparison"><section><h3>{text("当前正文", "Current text")}</h3><MarkdownView content={currentSection(markdown, kind)} /></section><label>{text("新结果（可直接编辑）", "New result (editable)")}<textarea rows={16} value={edited} onChange={e => setEdited(e.target.value)} disabled={!candidate} placeholder={text("点击生成，结果将在这里显示", "Generate to preview a new version here")} /></label></div>}
        {busy && <p role="status">{active?.status === "queued" ? text("排队中…", "Queued…") : text("正在生成，完成后将在此展示…", "Generating; the result will appear here…")}</p>}
        {notice && <p role="status">{notice}</p>}
        {active?.status === "failed" && <p role="alert">{active.error_message}</p>}
        {(run.error || save.error || overview.error || cancel.error) && <p role="alert"><LocalizedError error={(run.error || save.error || overview.error || cancel.error)} /></p>}
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
