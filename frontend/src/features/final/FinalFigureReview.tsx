import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiRequest, jsonBody } from "../../api/client";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";
import { diagnosticText } from "../../i18n/diagnostics";

type Review = { published_label: string; caption: string; fingerprint: string; output_artifact_id: string;
  source_artifact_id: string; paper_id: string; paper_title: string; source_label: string; source_caption: string;
  context_md: string; blockers: string[] };

export function FinalFigureReview({ projectId, figureId, busy, close, onSync }: {
  projectId: string; figureId: string; busy: boolean; close: () => void; onSync: () => Promise<unknown>;
}) {
  const { text, language } = useUiText();
  const dialog = useRef<HTMLDialogElement>(null);
  const client = useQueryClient();
  const [caption, setCaption] = useState("");
  const [location, setLocation] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [saved, setSaved] = useState(false);
  const url = `/api/v1/projects/${encodeURIComponent(projectId)}/final/figures/${encodeURIComponent(figureId)}/review`;
  const query = useQuery({ queryKey: ["final-figure-review", projectId, figureId], queryFn: () => apiRequest<Review>(url), refetchOnWindowFocus: false });
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => { if (query.data) setCaption(query.data.caption); }, [query.data]);
  const sync = useMutation({ mutationFn: onSync });
  const save = useMutation({ mutationFn: async () => {
    await apiRequest(url, { method: "PUT", ...jsonBody({ fingerprint: query.data!.fingerprint, caption,
      source_location: location, confirmed }) });
    setSaved(true);
    await client.invalidateQueries({ queryKey: ["final", projectId] });
  }, onSuccess: () => sync.mutate() });
  const pending = save.isPending || sync.isPending;
  const row = query.data;
  const reason = !row ? text("正在读取图片记录。", "Loading figure record.")
    : row.blockers.length ? text("请先处理下方来源问题，不能直接确认。", "Resolve source issues below before confirming.")
    : busy ? text("终稿正在处理，请稍后保存。", "Final is busy; wait before saving.")
    : !caption.trim() ? text("请填写图注。", "Enter a caption.")
    : !confirmed ? text("核对后请勾选确认项。", "Check the confirmation after reviewing.") : "";
  return <dialog ref={dialog} className="final-bibliography-dialog final-figure-review" aria-label={text("核对图片与图注", "Review figure and caption")}
    onCancel={e => { e.preventDefault(); if (!pending) close(); }}>
    <header><h2>{row?.published_label} · {text("核对图片与图注", "Review figure and caption")}</h2><button className="button button-quiet" disabled={pending} onClick={close}>{text("关闭", "Close")}</button></header>
    <p className="muted">{text("这不是图片错误的判定。请核对图片来源、图注和正文解释。保存只更新终稿图注与人工核对记录，不修改初稿或重绘图片；初稿或图像版本变化后需重新核对。", "This finding does not prove the image is wrong. Check its source, caption and prose. Saving updates only Final captions and human review records, without changing Draft or redrawing. Review again after Draft or image versions change.")}</p>
    {row && <>
      <div className="final-figure-comparison">{[[row.output_artifact_id, text("当前图片", "Current figure")], [row.source_artifact_id, text("原始图片", "Source figure")]].map(([id, label]) => <section key={label}><h3>{label}</h3>{id ? <a href={`/api/v1/artifacts/${encodeURIComponent(id)}/content`} target="_blank" rel="noreferrer"><img src={`/api/v1/artifacts/${encodeURIComponent(id)}/content`} alt={label} /></a> : <p>{text("来源图片不可用", "Source image unavailable")}</p>}</section>)}</div>
      <h3>{row.paper_title || text("来源论文未确认", "Source paper not confirmed")} · {row.source_label}</h3>
      {row.paper_id && <a href={`/api/v1/library/papers/${encodeURIComponent(row.paper_id)}/pdf`} target="_blank" rel="noreferrer">{text("打开原始 PDF 核对", "Inspect source PDF")}</a>}
      <h4>{text("原图注 / 来源描述", "Source caption / description")}</h4><p>{row.source_caption || text("未提取到原图注，请对照 PDF。", "No extracted caption; inspect the PDF.")}</p>
      <h4>{text("相关正文（只读）", "Related prose (read-only)")}</h4><MarkdownView content={row.context_md} empty={text("未定位到邻近段落，请前往初稿核对。", "No adjacent paragraph found; inspect Draft.")} />
      <fieldset disabled={saved || pending || busy || !!row.blockers.length} className="final-bibliography-fields">
        <label>{text("终稿图注（必填，不含 Figure 编号）", "Final caption (required, without Figure number)")}<textarea rows={4} maxLength={3000} value={caption} onChange={e => setCaption(e.target.value)} /></label>
        <label>{text("原文页码或图号（选填）", "Source page or figure number (optional)")}<input maxLength={500} value={location} onChange={e => setLocation(e.target.value)} /><span className="muted">{text("仅用于记录核对位置，留空不影响保存。", "For recording where you checked; leaving it blank does not prevent saving.")}</span></label>
        <label className="final-bibliography-confirm"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />{text("已对照原文，确认图片、图注和相关解释对应同一项工作。", "I checked the source: the figure, caption and related interpretation refer to the same work.")}</label>
      </fieldset>
      {row.blockers.length > 0 && <ul role="alert">{row.blockers.map(message => <li key={message}>{diagnosticText(message, language)}</li>)}</ul>}
      <div className="button-row"><Link className="button button-secondary" to={`/images?project=${encodeURIComponent(projectId)}`}>{text("更换图片 / 核对来源", "Change image / check source")}</Link><Link className="button button-secondary" to={`/draft?project=${encodeURIComponent(projectId)}`}>{text("修改正文解释", "Edit interpretation in Draft")}</Link></div>
    </>}
    {(query.error || save.error || sync.error) && <div role="alert">{saved ? text("记录已保存，同步提交失败，可重试。", "Saved, but sync submission failed. Retry sync.") : text("未能读取或保存，请检查填写内容；版本变化时关闭后重新打开。", "Could not load or save; check fields or reopen after a version change.")}<details><summary>{text("详细原因", "Details")}</summary>{(query.error || save.error || sync.error)?.message}</details></div>}
    {saved ? <p role="status">{text("核对记录已保存。同步完成后查看终稿并重新下载导出文件。", "Review saved. Check Final and download new exports after sync completes.")}</p> : <p className="muted">{reason}</p>}
    <footer>{!saved ? <button className="button button-primary" disabled={!!reason || pending} onClick={() => save.mutate()}>{pending ? text("保存中…", "Saving…") : text("确认并更新终稿", "Confirm and update Final")}</button> : sync.isError ? <button className="button button-primary" disabled={busy || pending} onClick={() => sync.mutate()}>{text("重试同步终稿", "Retry Final sync")}</button> : null}</footer>
  </dialog>;
}
