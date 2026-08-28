import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody } from "../../api/client";
import { queryKeys } from "../../api/queries";
import type {
  BibliographyAuditResponse,
  BibliographyCandidate,
  BibliographyResolutionResponse,
  Job,
} from "../../api/types";
import { jobIsActive, useJob } from "../../hooks/useJob";
import { useUiText } from "../../i18n/useUiText";

type PaperSummary = {
  paper_id: string;
  title?: unknown;
  authors?: string[];
  year?: string | number;
  journal?: string;
  doi?: string;
};

type ManualFields = {
  title: string;
  authors: string;
  journal: string;
  year: string;
  publication_date: string;
  doi: string;
  book_title: string;
  publisher: string;
  school: string;
  degree_type: string;
  patent_number: string;
  responsible_entity: string;
  source_type: string;
  locator: string;
  parent_paper_id: string;
  evidence_location: string;
  evidence_note: string;
  reason: string;
};

const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

function displayValue(raw: unknown): string {
  if (raw === null || raw === undefined) return "";
  if (Array.isArray(raw)) return raw.map(displayValue).filter(Boolean).join("; ");
  if (typeof raw === "object" && "value" in raw) {
    return displayValue((raw as { value?: unknown }).value);
  }
  return String(raw);
}

function candidateSummary(candidate: BibliographyCandidate): string {
  const fields = candidate.fields || {};
  return [
    displayValue(fields.title),
    displayValue(fields.journal),
    displayValue(fields.year || fields.bibliographic_year),
    displayValue((fields.identifiers as Record<string, unknown> | undefined)?.doi || fields.doi),
  ].filter(Boolean).join(" · ");
}

export function BibliographyResolutionPanel({
  paper,
  onChanged,
}: {
  paper: PaperSummary;
  onChanged: () => Promise<unknown>;
}) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [submittedJobId, setSubmittedJobId] = useState("");
  const handledJobId = useRef("");
  const [documentType, setDocumentType] = useState("journal_article");
  const [manual, setManual] = useState<ManualFields>({
    title: displayValue(paper.title),
    authors: (paper.authors || []).join("; "),
    journal: paper.journal || "",
    year: displayValue(paper.year),
    publication_date: "",
    doi: paper.doi || "",
    book_title: "",
    publisher: "",
    school: "",
    degree_type: "",
    patent_number: "",
    responsible_entity: "",
    source_type: "",
    locator: "",
    parent_paper_id: "",
    evidence_location: "PDF page 1",
    evidence_note: "",
    reason: "",
  });

  useEffect(() => {
    setSubmittedJobId("");
    handledJobId.current = "";
    setManual({
      title: displayValue(paper.title),
      authors: (paper.authors || []).join("; "),
      journal: paper.journal || "",
      year: displayValue(paper.year),
      publication_date: "",
      doi: paper.doi || "",
      book_title: "",
      publisher: "",
      school: "",
      degree_type: "",
      patent_number: "",
      responsible_entity: "",
      source_type: "",
      locator: "",
      parent_paper_id: "",
      evidence_location: "PDF page 1",
      evidence_note: "",
      reason: "",
    });
  }, [paper.paper_id, paper.title, paper.authors, paper.year, paper.journal, paper.doi]);

  const audit = useQuery<BibliographyAuditResponse>({
    queryKey: queryKeys.libraryBibliographyAudit(paper.paper_id),
    queryFn: () => apiRequest(`/api/v1/library/papers/${encodeURIComponent(paper.paper_id)}/bibliography-audit`),
    enabled: Boolean(paper.paper_id),
  });
  const currentJobId = submittedJobId || audit.data?.job?.id || "";
  const liveJob = useJob(currentJobId);
  const job = liveJob.data || audit.data?.job || null;

  useEffect(() => {
    if (!job || !terminalStatuses.has(job.status) || handledJobId.current === job.id) return;
    handledJobId.current = job.id;
    void queryClient.invalidateQueries({ queryKey: queryKeys.libraryBibliographyAudit(paper.paper_id) });
    void onChanged();
  }, [job, onChanged, paper.paper_id, queryClient]);

  const startAudit = useMutation({
    mutationFn: () => apiRequest<Job>(`/api/v1/library/papers/${encodeURIComponent(paper.paper_id)}/bibliography-audit-jobs`, { method: "POST" }),
    onSuccess: (submitted) => setSubmittedJobId(submitted.id),
  });

  const resolve = useMutation({
    mutationFn: (body: Record<string, unknown>) => apiRequest<BibliographyResolutionResponse>(
      `/api/v1/library/papers/${encodeURIComponent(paper.paper_id)}/bibliography-resolution`,
      { method: "POST", ...jsonBody(body) },
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.libraryBibliographyAudit(paper.paper_id) });
      await queryClient.invalidateQueries({ queryKey: ["library"] });
      await onChanged();
    },
  });

  const candidates = audit.data?.candidates || [];
  const missingFields = audit.data?.audit?.automatic_resolution_missing_fields || [];
  const resolutionStatus = audit.data?.audit?.manual_review_status || "not_reviewed";
  const active = Boolean(job && jobIsActive(job.status));
  const fields = useMemo(() => ({
    title: manual.title,
    authors: manual.authors,
    journal: manual.journal,
    year: manual.year,
    publication_date: manual.publication_date,
    doi: manual.doi,
    book_title: manual.book_title,
    publisher: manual.publisher,
    school: manual.school,
    degree_type: manual.degree_type,
    patent_number: manual.patent_number,
    responsible_entity: manual.responsible_entity,
    source_type: manual.source_type,
    locator: manual.locator,
  }), [manual]);
  const evidence = {
    evidence_type: manual.evidence_location.toLowerCase().startsWith("http") ? "formal_url" : "pdf_page",
    location: manual.evidence_location,
    note: manual.evidence_note,
  };
  const setField = (key: keyof ManualFields, next: string) => setManual((current) => ({ ...current, [key]: next }));

  return (
    <section className="bibliography-resolution-panel">
      <div className="bibliography-resolution-actions">
        <button className="button button-secondary" type="button" disabled={active || startAudit.isPending} onClick={() => startAudit.mutate()}>
          {active || startAudit.isPending ? text("正在核验…", "Verifying…") : text("自动核验书目", "Verify bibliography")}
        </button>
        <span className={`status-pill ${resolutionStatus === "resolved" ? "ok" : "warning"}`}>
          {resolutionStatus === "resolved" ? text("已解决", "Resolved") : resolutionStatus}
        </span>
      </div>
      {job ? <p className={`message ${job.status === "failed" ? "message-error" : "message-info"}`}>{job.status === "failed" ? job.error_message : text(`核验任务：${job.status}${job.progress_total ? ` ${job.progress_current}/${job.progress_total}` : ""}`, `Verification job: ${job.status}${job.progress_total ? ` ${job.progress_current}/${job.progress_total}` : ""}`)}</p> : null}
      {missingFields.length ? <p className="message message-warning">{text(`仍缺少：${missingFields.join("、")}`, `Still missing: ${missingFields.join(", ")}`)}</p> : null}
      {candidates.length ? <details open className="bibliography-candidates"><summary>{text(`候选记录 ${candidates.length}`, `${candidates.length} candidate record(s)`)}</summary><div>{candidates.map((candidate) => <article key={candidate.candidate_id}><div><strong>{candidate.source}</strong><p>{candidateSummary(candidate) || text("候选字段不完整", "Candidate fields are incomplete")}</p><small>{text(`题名相似度 ${Math.round(Number(candidate.match?.title_similarity || 0) * 100)}%`, `Title similarity ${Math.round(Number(candidate.match?.title_similarity || 0) * 100)}%`)}</small></div><button className="button button-secondary" type="button" disabled={resolve.isPending} onClick={() => resolve.mutate({ action: "accept_candidate", candidate_id: candidate.candidate_id, document_type: documentType, fields: {} })}>{text("确认此记录", "Accept")}</button></article>)}</div></details> : null}
      <details className="bibliography-manual"><summary>{text("人工补充或设为辅助来源", "Manual resolution or supporting source")}</summary><div className="bibliography-manual-grid">
        <label>{text("文献类型", "Document type")}<select value={documentType} onChange={(event) => setDocumentType(event.target.value)}><option value="journal_article">{text("期刊论文", "Journal article")}</option><option value="book_chapter">{text("书籍章节", "Book chapter")}</option><option value="thesis">{text("学位论文", "Thesis")}</option><option value="patent">{text("专利", "Patent")}</option><option value="supporting_information">Supporting Information</option><option value="other">{text("其他正式来源", "Other formal source")}</option></select></label>
        <label>{text("标题", "Title")}<input value={manual.title} onChange={(event) => setField("title", event.target.value)} /></label>
        <label>{text("作者（分号分隔）", "Authors (semicolon-separated)")}<input value={manual.authors} onChange={(event) => setField("authors", event.target.value)} /></label>
        <label>{text("期刊或正式来源", "Journal or formal source")}<input value={manual.journal} onChange={(event) => setField("journal", event.target.value)} /></label>
        <label>{text("年份", "Year")}<input inputMode="numeric" value={manual.year} onChange={(event) => setField("year", event.target.value)} /></label>
        <label>{text("出版年月", "Publication date")}<input placeholder="YYYY-MM" value={manual.publication_date} onChange={(event) => setField("publication_date", event.target.value)} /></label>
        <label>DOI<input value={manual.doi} onChange={(event) => setField("doi", event.target.value)} /></label>
        {documentType === "book_chapter" ? <><label>{text("书名", "Book title")}<input value={manual.book_title} onChange={(event) => setField("book_title", event.target.value)} /></label><label>{text("出版方", "Publisher")}<input value={manual.publisher} onChange={(event) => setField("publisher", event.target.value)} /></label></> : null}
        {documentType === "thesis" ? <><label>{text("学校", "School")}<input value={manual.school} onChange={(event) => setField("school", event.target.value)} /></label><label>{text("学位类型", "Degree type")}<input value={manual.degree_type} onChange={(event) => setField("degree_type", event.target.value)} /></label></> : null}
        {documentType === "patent" ? <label>{text("专利号", "Patent number")}<input value={manual.patent_number} onChange={(event) => setField("patent_number", event.target.value)} /></label> : null}
        {documentType === "other" ? <><label>{text("责任主体", "Responsible entity")}<input value={manual.responsible_entity} onChange={(event) => setField("responsible_entity", event.target.value)} /></label><label>{text("来源类型", "Source type")}<input value={manual.source_type} onChange={(event) => setField("source_type", event.target.value)} /></label><label>{text("可定位标识", "Locator")}<input value={manual.locator} onChange={(event) => setField("locator", event.target.value)} /></label></> : null}
        <label>{text("母论文 ID（补充材料时必填）", "Parent paper ID (required for SI)")}<input value={manual.parent_paper_id} onChange={(event) => setField("parent_paper_id", event.target.value)} /></label>
        <label>{text("核验依据位置", "Evidence location")}<input value={manual.evidence_location} onChange={(event) => setField("evidence_location", event.target.value)} /></label>
        <label className="wide">{text("依据说明", "Evidence note")}<textarea value={manual.evidence_note} onChange={(event) => setField("evidence_note", event.target.value)} /></label>
        <label className="wide">{text("拒绝原因（仅在记录确实错误时填写）", "Rejection reason (only when the record is wrong)")}<textarea value={manual.reason} onChange={(event) => setField("reason", event.target.value)} /></label>
      </div><div className="bibliography-manual-actions"><button className="button button-primary" type="button" disabled={resolve.isPending || !manual.evidence_location.trim()} onClick={() => resolve.mutate({ action: "save_manual", document_type: documentType, parent_paper_id: manual.parent_paper_id || null, fields, manual_evidence: evidence })}>{text("保存并确认书目", "Save and resolve")}</button><button className="button button-secondary" type="button" disabled={resolve.isPending || !manual.evidence_location.trim()} onClick={() => resolve.mutate({ action: "supporting_only", document_type: documentType, parent_paper_id: manual.parent_paper_id || null, fields, manual_evidence: evidence, reason: manual.reason })}>{text("作为辅助来源保留", "Keep as supporting source")}</button><button className="button button-quiet danger" type="button" disabled={resolve.isPending || !manual.reason.trim()} onClick={() => resolve.mutate({ action: "reject", document_type: documentType, fields: {}, reason: manual.reason })}>{text("标记记录有误（不移除论文）", "Reject record (keep paper)")}</button></div></details>
      {resolve.data ? <p className="message message-success">{text(`书目状态已更新；影响范围：${resolve.data.impact.affected.join("、") || "仅书目"}。`, `Bibliography updated; affected scope: ${resolve.data.impact.affected.join(", ") || "bibliography only"}.`)}</p> : null}
      {(audit.error || startAudit.error || resolve.error) ? <p className="message message-error">{(audit.error || startAudit.error || resolve.error)?.message}</p> : null}
    </section>
  );
}
