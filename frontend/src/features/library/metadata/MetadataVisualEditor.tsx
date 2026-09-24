import { LocalizedError } from "../../../components/LocalizedError";
import { useUiText } from "../../../i18n/useUiText";
import {
  isMetadataObject,
  metadataFieldConfidence,
  metadataFieldIsHumanChecked,
  metadataFieldValue,
  metadataTextForEditing,
  metadataValidationError,
  setStructuredTagsVerified,
  STRUCTURED_TAG_KEYS,
  structuredTagValue,
  updateBibliographicField,
  updateStructuredTag,
  type BibliographicField,
  type MetadataRecord,
  type StructuredTagKey,
} from "./metadataEditorModel";


function MetadataFieldStatus({ metadata, field }: { metadata: MetadataRecord; field: BibliographicField }) {
  const { text } = useUiText();
  const confidence = metadataFieldConfidence(metadata, field);
  if (metadataFieldIsHumanChecked(metadata, field)) {
    return <span className="metadata-field-status checked">{text("已人工核对", "Manually checked")}</span>;
  }
  if (confidence !== null) {
    return <span className={`metadata-field-status ${confidence < 0.7 ? "low" : ""}`}>{text(`系统识别 ${Math.round(confidence * 100)}%`, `Detected ${Math.round(confidence * 100)}%`)}</span>;
  }
  return <span className="metadata-field-status low">{text("待核对", "Needs review")}</span>;
}


export function MetadataVisualEditor({
  draft,
  dirty,
  saving,
  reviewing,
  reextracting,
  reextractResult,
  error,
  onChange,
  onSave,
  onReview,
  onReextract,
}: {
  draft: MetadataRecord;
  dirty: boolean;
  saving: boolean;
  reviewing: boolean;
  reextracting: boolean;
  reextractResult: string;
  error: Error | null;
  onChange: (next: MetadataRecord) => void;
  onSave: () => void;
  onReview: () => void;
  onReextract: () => void;
}) {
  const { language, text } = useUiText();
  const validationError = metadataValidationError(draft);
  const humanReview = isMetadataObject(draft.human_review) ? draft.human_review : {};
  const quality = isMetadataObject(draft.quality) ? draft.quality : {};
  const sourceFile = isMetadataObject(draft.source_file) ? draft.source_file : {};
  const extraction = isMetadataObject(draft.extraction) ? draft.extraction : {};
  const authors = metadataFieldValue(draft, "authors");
  const warnings = Array.isArray(quality.warnings) ? quality.warnings.map(String) : [];
  const overallConfidence = Number(quality.overall_confidence);
  const reviewed = humanReview.status === "reviewed";
  const tagsVerified = metadataFieldIsHumanChecked(draft, "structured_tags");
  const tagLabels: Record<StructuredTagKey, string> = {
    product: text("主要产物", "Main product"),
    substrate: text("原料 / 底物", "Starting material / substrate"),
    catalyst_or_method: text("催化剂或核心方法", "Catalyst or core method"),
    organometallic_partner: text("有机金属试剂", "Organometallic partner"),
    ligand_or_chiral_source: text("配体或手性来源", "Ligand or chiral source"),
    leaving_group: text("离去基团", "Leaving group"),
    reaction_type: text("反应类型", "Reaction type"),
    document_scope: text("论文内容范围", "Document scope"),
  };
  const warningLabels: Record<string, string> = {
    missing_title: text("缺少标题", "Missing title"),
    missing_authors: text("缺少作者", "Missing authors"),
    missing_year: text("缺少发表年份", "Missing publication year"),
    missing_journal: text("缺少期刊名称", "Missing journal"),
    missing_doi: text("缺少 DOI", "Missing DOI"),
    low_confidence_title: text("标题识别置信度较低", "Low-confidence title"),
    low_confidence_authors: text("作者识别置信度较低", "Low-confidence authors"),
    low_confidence_year: text("年份识别置信度较低", "Low-confidence year"),
    low_confidence_abstract: text("摘要识别置信度较低", "Low-confidence abstract"),
  };
  const reviewedAt = String(humanReview.reviewed_at || "").trim();
  const formattedReviewedAt = reviewedAt && !Number.isNaN(Date.parse(reviewedAt))
    ? new Date(reviewedAt).toLocaleString(language === "en" ? "en-US" : "zh-CN")
    : "";
  const setField = (field: BibliographicField, value: unknown) => onChange(updateBibliographicField(draft, field, value));

  return (
    <form className="editor-panel metadata-review-panel" onSubmit={(event) => { event.preventDefault(); if (dirty && !validationError && !saving && !reviewing) onSave(); }}>
      <div className={`metadata-review-primary ${dirty ? "dirty" : ""}`}>
        <div>
          <span className={`metadata-review-state ${reviewed ? "reviewed" : "pending"}`}>
            {reviewed ? text("已审核", "Reviewed") : text("待人工核对", "Review needed")}
          </span>
          <strong>{dirty ? text("有尚未保存的修改", "You have unsaved changes") : text("核对系统识别的书目信息", "Review the detected bibliography")}</strong>
          <p>{text("直接修改下方字段即可，不需要理解 JSON。保存后，修改过的字段会标记为“已人工核对”。", "Edit the fields below directly—no JSON knowledge is needed. Saved edits are marked as manually checked.")}</p>
          {formattedReviewedAt ? <small>{text(`上次审核：${formattedReviewedAt}`, `Last reviewed: ${formattedReviewedAt}`)}</small> : null}
        </div>
        <div className="metadata-review-actions">
          <button className="button button-secondary" type="submit" disabled={!dirty || Boolean(validationError) || saving || reviewing}>
            {saving ? text("保存中…", "Saving…") : text("保存修改", "Save changes")}
          </button>
          <button className="button button-primary" type="button" disabled={Boolean(validationError) || saving || reviewing} onClick={onReview}>
            {reviewing ? text("确认中…", "Confirming…") : reviewed && !dirty ? text("重新确认审核", "Confirm review again") : text("保存并标记为已审核", "Save and mark reviewed")}
          </button>
        </div>
      </div>

      <section className="metadata-form-section" aria-labelledby="metadata-bibliography-title">
        <header>
          <div><span>01</span><h3 id="metadata-bibliography-title">{text("基本书目信息", "Bibliographic information")}</h3></div>
          <p>{text("标题为必填项；作者建议每行填写一位。", "Title is required. Enter one author per line.")}</p>
        </header>
        <div className="metadata-form-grid">
          <label className="metadata-form-field metadata-field-wide">
            <span><strong>{text("论文标题", "Paper title")}</strong><MetadataFieldStatus metadata={draft} field="title" /></span>
            <textarea rows={3} value={String(metadataFieldValue(draft, "title") || "")} onChange={(event) => setField("title", event.target.value)} aria-invalid={validationError === "title"} />
            {validationError === "title" ? <small className="field-error">{text("请填写论文标题。", "Enter a paper title.")}</small> : null}
          </label>
          <label className="metadata-form-field metadata-field-wide">
            <span><strong>{text("作者", "Authors")}</strong><MetadataFieldStatus metadata={draft} field="authors" /></span>
            <textarea rows={4} value={Array.isArray(authors) ? authors.map(String).join("\n") : String(authors || "")} onChange={(event) => setField("authors", event.target.value)} placeholder={text("每行一位作者，例如：\nAda Lovelace\nGrace Hopper", "One author per line, for example:\nAda Lovelace\nGrace Hopper")} />
          </label>
          <label className="metadata-form-field">
            <span><strong>{text("发表年份", "Publication year")}</strong><MetadataFieldStatus metadata={draft} field="year" /></span>
            <input type="number" min={1000} max={new Date().getFullYear() + 1} value={String(metadataFieldValue(draft, "year") ?? "")} onChange={(event) => setField("year", event.target.value ? Number(event.target.value) : null)} aria-invalid={validationError === "year"} />
            {validationError === "year" ? <small className="field-error">{text("请输入有效的四位年份。", "Enter a valid four-digit year.")}</small> : null}
          </label>
          <label className="metadata-form-field">
            <span><strong>{text("期刊 / 出版物", "Journal / publication")}</strong><MetadataFieldStatus metadata={draft} field="journal" /></span>
            <input value={String(metadataFieldValue(draft, "journal") || "")} onChange={(event) => setField("journal", event.target.value)} placeholder={text("例如：Nature Chemistry", "For example: Nature Chemistry")} />
          </label>
          <label className="metadata-form-field metadata-field-wide">
            <span><strong>DOI</strong><MetadataFieldStatus metadata={draft} field="doi" /></span>
            <input value={String(metadataFieldValue(draft, "doi") || "")} onChange={(event) => setField("doi", event.target.value.trim())} placeholder="10.xxxx/xxxxx" />
            <small>{text("只填写 DOI 本身，不需要粘贴 https://doi.org/。", "Enter the DOI only; you do not need the https://doi.org/ prefix.")}</small>
          </label>
        </div>
      </section>

      <section className="metadata-form-section" aria-labelledby="metadata-abstract-title">
        <header>
          <div><span>02</span><h3 id="metadata-abstract-title">{text("摘要", "Abstract")}</h3></div>
          <p>{text("用于后续检索、筛选和写作规划。", "Used for later retrieval, screening, and writing plans.")}</p>
        </header>
        {!metadataFieldIsHumanChecked(draft, "abstract") ? (
          <div>
            <button className="button button-secondary" type="button" disabled={dirty || saving || reviewing || reextracting} onClick={onReextract}>
              {reextracting ? text("正在重新识别…", "Rechecking abstract…") : text("从已有解析重新识别", "Recheck parsed abstract")}
            </button>
            {dirty ? <small>{text("请先保存当前修改", "Save current edits first")}</small> : null}
          </div>
        ) : null}
        {reextractResult ? <p className="message" role="status">{{
          updated: text("已重新识别摘要，请核对内容。", "Abstract rechecked. Please review it."),
          not_found: text("已有解析中仍未找到明确的原文摘要，可手动补充。", "No explicit abstract was found in the existing parse. You can add it manually."),
          unchanged: text("摘要内容没有变化。", "The abstract is unchanged."),
          human_checked: text("人工已核对的摘要未被覆盖。", "The manually checked abstract was preserved."),
        }[reextractResult] || reextractResult}</p> : null}
        <label className="metadata-form-field metadata-field-wide">
          <span><strong>{text("论文摘要", "Paper abstract")}</strong><MetadataFieldStatus metadata={draft} field="abstract" /></span>
          <textarea rows={10} value={metadataTextForEditing(metadataFieldValue(draft, "abstract"))} onChange={(event) => setField("abstract", event.target.value)} placeholder={text("未识别到摘要时可在此补充", "Add the abstract here if it was not detected")} />
        </label>
      </section>

      <details className="metadata-form-section metadata-optional-section">
        <summary>
          <div><span>03</span><strong>{text("内容标签（可选）", "Content tags (optional)")}</strong></div>
          <small>{text("经核对后，仅作为章节归类的辅助线索", "After verification, used only as a supporting hint for chapter organization")}</small>
        </summary>
        <div className="metadata-optional-body">
          <p>{text(
            "仅填写论文中明确出现的内容；不确定的项目请留空，系统会按“未指定”保存。这些标签不决定检索相关性，也不会作为正文科学证据。",
            "Enter only information explicitly stated in the paper. Leave uncertain fields blank; the system stores them as not specified. These tags do not determine retrieval relevance or serve as scientific evidence.",
          )}</p>
          <div className="metadata-tag-grid">
            {STRUCTURED_TAG_KEYS.map((key) => (
              <label className="metadata-form-field" key={key}>
                <span><strong>{tagLabels[key]}</strong></span>
                <input value={structuredTagValue(draft, key)} onChange={(event) => onChange(updateStructuredTag(draft, key, event.target.value))} placeholder={text("未填写", "Not provided")} />
              </label>
            ))}
          </div>
          <label className="metadata-tag-verification">
            <input type="checkbox" checked={tagsVerified} onChange={(event) => onChange(setStructuredTagsVerified(draft, event.target.checked))} />
            <span><strong>{text("我已根据论文内容核对以上标签", "I checked these tags against the paper")}</strong><small>{text("勾选后，已填写的标签才会辅助后续章节归类；再次修改会自动取消核对状态。", "After confirmation, completed tags may assist chapter organization. Editing a tag automatically clears this confirmation.")}</small></span>
          </label>
        </div>
      </details>

      <details className="metadata-system-details">
        <summary>{text("查看系统识别与文件信息（只读）", "View detection and file details (read-only)")}</summary>
        <div className="metadata-system-body">
          <dl>
            <div><dt>{text("论文编号", "Paper ID")}</dt><dd>{String(draft.paper_id || "—")}</dd></div>
            <div><dt>{text("原始文件", "Original file")}</dt><dd>{String(sourceFile.original_upload_name || sourceFile.pdf_name || "—")}</dd></div>
            <div><dt>{text("解析方式", "Extraction mode")}</dt><dd>{String(extraction.mode || "—")}</dd></div>
            <div><dt>{text("整体识别置信度", "Overall confidence")}</dt><dd>{Number.isFinite(overallConfidence) ? `${Math.round(overallConfidence * 100)}%` : "—"}</dd></div>
          </dl>
          <div className="metadata-quality-summary">
            <strong>{warnings.length ? text(`系统建议核对 ${warnings.length} 项`, `${warnings.length} items need review`) : text("未发现额外问题", "No additional issues found")}</strong>
            {warnings.length ? <ul>{warnings.map((warning) => <li key={warning}>{warningLabels[warning] || warning.replaceAll("_", " ")}</li>)}</ul> : null}
          </div>
        </div>
      </details>

      {error ? <p className="message message-error" role="alert"><LocalizedError error={error} /></p> : null}
    </form>
  );
}
