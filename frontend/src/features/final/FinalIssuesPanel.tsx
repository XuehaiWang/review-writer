import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { apiRequest, jsonBody } from "../../api/client";
import { isMetadataObject, metadataFieldValue, metadataTextForEditing, type MetadataRecord } from "../library/metadata/metadataEditorModel";
import { useUiText } from "../../i18n/useUiText";
import { FinalFigureReview } from "./FinalFigureReview";

export type FinalIssue = { target_type: string; target_id: string; issues: string[]; title?: string; reference_number?: number };
const fields = [
  ["authors", "作者（每行一位）", "Authors (one per line)"], ["journal", "期刊", "Journal"],
  ["volume", "卷号", "Volume"], ["issue", "期号", "Issue"], ["pages", "页码", "Pages"],
  ["article_number", "文章编号", "Article number"], ["publisher", "出版社", "Publisher"],
] as const;
const descriptions: Record<string, [string, string]> = {
  figure_caption_pending: ["图注尚未确认。打开核对窗口，对照原图注确认或修改。", "The caption is pending. Compare it with the source and confirm or edit."],
  paper_level_interpretation_missing: ["图片解释的来源尚未记录，不代表图片错误。请对照原文核对图注及相关正文。", "The interpretation source has not been recorded; this does not prove the figure is wrong. Compare the caption and prose with the source."],
  source_figure_identity_unresolved: ["原论文图号或图片来源未确认，请到图像阶段核对来源。", "Source figure identity is unresolved; check it in Images."],
  source_paper_identity_missing: ["缺少来源论文记录，请到图像阶段补全来源。", "The source paper is missing; check the source in Images."],
  visible_callout_or_interpretation_missing: ["正文中没有找到对应图号，请在初稿中补充对该图的引用和解释。", "No matching figure callout was found. Add its reference and interpretation in Draft."],
  published_label_missing: ["图片缺少正文编号，请同步终稿；仍有问题时联系管理员。", "The figure number is missing. Sync Final; contact an administrator if it persists."],
  authors: ["作者信息缺失、格式异常或存在冲突，请对照原文核对。", "Author details are missing, malformed or conflicting. Check the source."],
  journal: ["期刊信息需要补全或核对。", "Check or complete the journal details."],
  volume: ["请对照原文补全或更正卷号。", "Check or complete the volume against the source."],
  pages: ["请核对页码；采用文章编号的期刊，请填写文章编号。", "Check page numbers, or supply the article number if the journal uses one."],
  title: ["论文标题需要核对；涉及论文身份，请到文献库处理。", "Check the paper title in the library; this affects source identity."],
  doi: ["DOI 需要核对，先确认引用的是同一篇论文。", "Check the DOI and confirm the source identity in the library."],
  year: ["发表年份需要核对，可能影响综述的时间范围。", "Check the publication year and review scope in the library."],
  metadata_changed_after_blueprint: ["论文身份、年份或来源信息在大纲确认后发生变化，请核对选文依据。", "Source identity, dates or evidence changed after planning; review source selection."],
  bibliography_identity_unresolved: ["书目信息尚未核实，请对照论文首页补全，身份有疑问时前往文献库核对。", "Verify bibliography against the original paper; resolve identity questions in the library."],
  overview_labels_missing: ["旧版总览图缺少分类记录，无需重新生成图片。", "The legacy overview lacks label metadata; image regeneration is unnecessary."],
  overview_title_missing: ["总览图缺少图注，请在初稿的总览图窗口补充并保存。", "Add and save an overview caption in Draft."],
  overview_labels_not_traceable_to_current_axis: ["总览图分类可能与正文不一致，请在初稿检查当前采用的图片。", "Check whether the selected overview matches the manuscript categories in Draft."],
  html_residue: ["稿件存在未正确处理的格式标记，请同步终稿后重新检查；仍出现时联系管理员。", "Unprocessed formatting remains. Sync Final; contact an administrator if it persists."],
  unverified_manual_claims_exported: ["有手动修改的内容尚未完成来源核验，请在初稿核对相关论述与引用。", "Check sources and citations for manually edited prose in Draft."],
  caption_missing: ["图片缺少图注，请在初稿补全。", "Complete the figure caption in Draft."],
  image_missing: ["引用的图片未出现在稿件中，请检查图片选择并同步终稿。", "A referenced image is missing; check figure selection and sync Final."],
};

export function FinalIssuesPanel({ projectId, issues, busy, onSync }: {
  projectId: string; issues: FinalIssue[]; busy: boolean; onSync: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const [editing, setEditing] = useState<FinalIssue | null>(null);
  const labels: Record<string, string> = { reference: text("参考文献", "Reference"), figure: text("图片", "Figure"),
    claim: text("论述与引用", "Claims and citations"), overview: text("总览图", "Overview"),
    outline: text("大纲与范围", "Outline and scope"), paragraph: text("正文段落", "Paragraph"), manuscript: text("稿件检查", "Manuscript checks") };
  return <section className="final-issues-panel"><h2>{text("检查与处理建议", "Checks and suggested actions")}</h2>
    <p className="muted">{text("这里列出需要核对的内容，不代表必须重新生成全文。书目更正会更新共用文献库；当前稿件保存后重新组装，其他项目下次同步时采用新信息。", "These findings do not require regenerating the manuscript. Corrections update the shared library; this Final is reassembled, and other projects use corrections on their next sync.")}</p>
    {!issues.length && <p>{text("当前没有待处理的检查项。", "No outstanding findings.")}</p>}
    {issues.map((row, index) => <article key={`${row.target_type}:${row.target_id}:${index}`}>
      <h3>{labels[row.target_type] || text("稿件检查", "Manuscript checks")}{row.reference_number ? ` [${row.reference_number}]` : ""}{row.title ? ` · ${row.title}` : ""}</h3>
      <ul>{row.issues.map(code => <li key={code}>{descriptions[code] ? text(...descriptions[code]) : row.target_type === "reference"
        ? text("部分书目字段尚未通过核对，请查看原文并补全；身份或来源问题请到文献库处理。", "Some bibliography fields need verification. Complete them from the source; handle identity questions in the library.")
        : text("这一项尚需核对。请检查相关原文、引用或图表；无法定位时，将技术详情提供给管理员。", "Review the related text, citations or figures. If you cannot locate the issue, share the technical details with an administrator.")}</li>)}</ul>
      <div className="button-row">{row.target_type === "reference" && <button type="button" className="button button-primary" disabled={busy} onClick={() => setEditing(row)}>{text("补全书目信息", "Correct bibliography")}</button>}
      {row.target_type === "figure" && row.target_id && <button type="button" className="button button-primary" disabled={busy} onClick={() => setEditing(row)}>{text("核对图片与图注", "Review figure and caption")}</button>}
      <Link className="button button-secondary" to={`/${row.target_type === "reference" ? "library" : row.target_type === "outline" ? "planning" : "draft"}?project=${encodeURIComponent(projectId)}`}>{row.target_type === "reference" ? text("文献库核对身份", "Check identity in library") : row.target_type === "outline" ? text("前往大纲核对", "Review outline") : text("前往初稿核对", "Review in Draft")}</Link></div>
      <details><summary>{text("技术详情（供排查）", "Technical details")}</summary><code>{row.target_id}: {row.issues.join(", ")}</code></details>
    </article>)}
    {editing && (editing.target_type === "figure" ? <FinalFigureReview key={editing.target_id} figureId={editing.target_id} projectId={projectId} busy={busy} onSync={onSync} close={() => setEditing(null)} /> : <BibliographyCorrection key={editing.target_id} issue={editing} projectId={projectId} busy={busy} onSync={onSync} close={() => setEditing(null)} />)}
  </section>;
}

function BibliographyCorrection({ issue, projectId, busy, onSync, close }: {
  issue: FinalIssue; projectId: string; busy: boolean; onSync: () => Promise<unknown>; close: () => void;
}) {
  const { text } = useUiText();
  const client = useQueryClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState(false);
  const [location, setLocation] = useState("");
  const [saved, setSaved] = useState(false);
  const url = `/api/v1/projects/${encodeURIComponent(projectId)}/final/references/${encodeURIComponent(issue.target_id)}/bibliography`;
  const query = useQuery({ queryKey: ["final-reference", projectId, issue.target_id], queryFn: () => apiRequest<{ metadata: MetadataRecord; metadata_artifact_id: string }>(url), refetchOnWindowFocus: false });
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => { if (query.data) setValues(Object.fromEntries(fields.map(([key]) => {
    const value = metadataFieldValue(query.data.metadata, key)
      ?? metadataFieldValue(query.data.metadata, key === "pages" ? "page" : key === "issue" ? "number" : key);
    return [key, Array.isArray(value) ? value.map(item => isMetadataObject(item)
      ? metadataTextForEditing(item.name || [item.given, item.family].filter(Boolean).join(" "))
      : metadataTextForEditing(item)).join("\n") : metadataTextForEditing(value)];
  }))); }, [query.data]);
  const sync = useMutation({ mutationFn: onSync });
  const save = useMutation({ mutationFn: async () => {
    await apiRequest(url, { method: "PUT", ...jsonBody({ metadata_artifact_id: query.data!.metadata_artifact_id,
      fields: Object.fromEntries(fields.map(([key]) => [key, key === "authors" ? (values[key] || "").split(/\r?\n/).map(v => v.trim()).filter(Boolean) : (values[key] || "").trim()])), source_location: location }) });
    setSaved(true);
    await Promise.all([client.invalidateQueries({ queryKey: ["final", projectId] }), client.invalidateQueries({ queryKey: ["library"] })]);
  }, onSuccess: () => sync.mutate() });
  const pending = save.isPending || sync.isPending;
  return <dialog ref={dialog} className="final-bibliography-dialog" aria-label={text("补全书目信息", "Correct bibliography")} onCancel={event => { event.preventDefault(); if (!pending) close(); }}>
    <header><h2>{text("补全书目信息", "Correct bibliography")}</h2><button className="button button-quiet" disabled={pending} onClick={close}>{text("关闭", "Close")}</button></header>
    {query.isPending && <p role="status">{text("读取中…", "Loading…")}</p>}
    {query.data && <><h3>{metadataTextForEditing(metadataFieldValue(query.data.metadata, "title")) || issue.title}</h3>
      <p className="muted">DOI: {metadataTextForEditing(metadataFieldValue(query.data.metadata, "doi")) || "—"} · {text("年份", "Year")}: {metadataTextForEditing(metadataFieldValue(query.data.metadata, "year")) || "—"}</p>
      <p className="muted">{text("只更正同一篇论文的书目信息，不重写正文。标题、DOI 和年份需要修改时，请前往文献库核对身份与范围。", "Correct bibliography for the same paper without rewriting prose. Change title, DOI or year in the library after checking identity and scope.")}</p>
      <a href={`/api/v1/library/papers/${encodeURIComponent(issue.target_id)}/pdf`} target="_blank" rel="noreferrer">{text("打开原始 PDF 对照", "Open source PDF")}</a>
      <p className="muted">{text("只修改有问题的字段，无需全部填满。选填表示不影响保存；作者、期刊等必要信息若仍缺失，检查提示会保留。", "Edit only the fields that need correction. Optional fields do not block saving; missing required bibliography details will still appear in checks.")}</p>
      <fieldset disabled={saved || pending || busy} className="final-bibliography-fields">{fields.map(([key, zh, en]) => <label key={key}>{text(zh, en)}{text("（选填）", " (optional)")}{key === "authors" ? <textarea rows={4} maxLength={20000} value={values[key] || ""} onChange={e => setValues(v => ({ ...v, [key]: e.target.value }))} /> : <input maxLength={1000} value={values[key] || ""} onChange={e => setValues(v => ({ ...v, [key]: e.target.value }))} />}</label>)}
      <label>{text("核对依据（选填，如 PDF 第 1 页）", "Source location (optional, e.g. PDF page 1)")}<input maxLength={500} value={location} onChange={e => setLocation(e.target.value)} /></label>
      <label className="final-bibliography-confirm"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />{text("已对照原文，确认是同一篇论文的书目更正。", "I checked the source and this is the same paper.")}</label></fieldset></>}
    {saved && <p role="status">{text("书目已保存。若正文中点名提到作者，请核对这些句子；系统不会自动改写。旧导出文件不会覆盖，请完成同步后重新下载。", "Bibliography saved. Check author names explicitly mentioned in prose; they are not rewritten. Sync and download new exports; existing files remain unchanged.")}</p>}
    {(query.error || save.error || sync.error) && <div role="alert">{saved ? text("书目已保存，但终稿同步未能提交。请重试同步。", "Saved, but Final sync could not start. Retry sync.") : text("未能读取或保存书目，请检查填写内容；若版本已变化，请关闭后重新打开。", "Could not load or save. Check fields; if the version changed, close and reopen.")}<details><summary>{text("技术详情", "Technical details")}</summary>{(query.error || save.error || sync.error)?.message}</details></div>}
    {sync.isSuccess && <p role="status">{text("终稿同步已提交，完成后会重新显示检查结果。", "Final sync submitted; findings refresh when it completes.")}</p>}
    {!saved && query.data && !pending && <p className="muted">{busy ? text("终稿正在处理，请等待任务完成后保存。", "Final is busy. Wait for the current task before saving.") : !confirmed ? text("保存前请勾选上方确认项；核对依据可以留空。", "Check the confirmation above to save; source location can be left blank.") : null}</p>}
    <footer>{!saved ? <button className="button button-primary" disabled={!query.data || !confirmed || pending || busy} onClick={() => save.mutate()}>{pending ? text("保存中…", "Saving…") : text("保存并更新参考文献", "Save and update references")}</button> : sync.isError ? <button className="button button-primary" disabled={pending || busy} onClick={() => sync.mutate()}>{text("重试同步终稿", "Retry Final sync")}</button> : null}</footer>
  </dialog>;
}
