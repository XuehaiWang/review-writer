import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../../api/client";
import { MarkdownView } from "../../components/MarkdownView";
import { useUiText } from "../../i18n/useUiText";
import { EvidenceLinks, type DialogueSource } from "./EvidenceLinks";

export type DialogueParagraph = { paragraph_key: string; paragraph_id: string; text: string; text_sha256: string };
export type DialogueCandidate = { candidate_id: string; paragraph_key: string; paragraph_id: string; original_text: string; candidate_text: string; reply: string; status: string; save_guidance?: "existing" | "missing"; validation_errors?: string[]; validation_warnings?: string[]; source_refs?: string[]; sources?: DialogueSource[]; context_paragraph_ids?: string[]; scientific_changes?: { field: string; before: string[]; after: string[] }[]; evidence_review?: string };
type Turn = { job_id: string; status: string; error?: string; message: string; candidate?: DialogueCandidate };

export function CandidateReply({ candidate }: { candidate: DialogueCandidate }) {
  const { text } = useUiText();
  if (candidate.validation_errors?.length) return <p role="alert" className="message message-error">{text("本段修改未通过技术校验，未生成可保存候选；预览保留原文。", "Technical validation failed; no saveable candidate was generated. The preview retains the original.")} {candidate.paragraph_id}: {candidate.validation_errors.join(", ")}</p>;
  return <p>{candidate.save_guidance === "missing"
    ? text("当前没有待保存的修改候选。请说明希望怎样修改（例如删除哪句话），我会先生成候选供你保存。", "No pending proposal exists. Describe the edit you want, and I will generate a proposal for you to save.")
    : candidate.save_guidance === "existing"
      ? text("本轮没有写入正文。请找到此前对应的待确认候选，点击“保存修改”；如果已处理，请以候选卡的当前状态为准。", "This turn did not save text. Find the earlier pending proposal and click Save changes; if already handled, follow its current card status.")
      : candidate.reply}</p>;
}

export function CandidateEvidenceReview({ candidate }: { candidate: DialogueCandidate }) {
  const { text } = useUiText();
  const fields: Record<string, string> = { numbers: text("数值", "Numbers"), stereo: text("立体化学", "Stereochemistry"), chemical_identities: text("名称或缩写", "Names or abbreviations"), required_labels: text("科学标识", "Scientific labels") };
  if (!candidate.evidence_review) return null;
  return <aside className="message message-warning">
    <p>{candidate.sources?.length ? text("以下证据供核对，不代表已验证修改正确；你可以确认并保存。", "Sources are provided for comparison, not verification of the changes. You may accept and save.") : text("未提供对应支持证据，请核对；仍可保存本次人工修改。", "No supporting passage was provided. Please review; you may still save this author-directed edit.")}</p>
    {candidate.scientific_changes?.map(change => <p key={change.field}>{fields[change.field] || change.field}: {change.before.join(", ") || "—"} → {change.after.join(", ") || "—"}</p>)}
  </aside>;
}

export function CandidateStatus({ status }: { status: string }) {
  const { text } = useUiText();
  const labels: Record<string, string> = { pending: text("待确认", "Pending"), accepted: text("已保存", "Saved"), rejected: text("已放弃", "Discarded"), superseded: text("已被替代", "Superseded"), stale: text("正文或依据已改变，仅供查看", "Text or sources changed; read only"), kept_original: text("保留原文", "Original retained") };
  return <strong>{labels[status] || status}</strong>;
}

export function CandidateComparison({ candidate, disabled, readOnly, decide }: { candidate: DialogueCandidate; disabled?: boolean; readOnly?: boolean; decide: (id: string, decision: "accept" | "reject") => void }) {
  const { text } = useUiText();
  return <section className="dialogue-candidate">
    <CandidateStatus status={candidate.status} />
    <CandidateReply candidate={candidate} />
    <CandidateEvidenceReview candidate={candidate} />
    {candidate.context_paragraph_ids?.length ? <p className="muted">{text("本轮只读上下文：", "Read-only context this turn: ")}{candidate.context_paragraph_ids.join(", ")}</p> : null}
    {candidate.candidate_text ? <div className="optimization-comparison"><div><h4>{text("保存前正文", "Saved original")}</h4><MarkdownView content={candidate.original_text} /></div><div><h4>{text("候选内容", "Candidate")}</h4><MarkdownView content={candidate.candidate_text} /></div></div> : null}
    {[...(candidate.validation_errors || []), ...(candidate.validation_warnings || [])].map((value, i) => <p role="status" key={i}>{value}</p>)}
    <EvidenceLinks sources={candidate.sources} legacyRefs={candidate.source_refs} />
    {candidate.status === "pending" && !readOnly ? <footer><button className="button button-primary" disabled={disabled} onClick={() => decide(candidate.candidate_id, "accept")}>{text("保存候选", "Save candidate")}</button><button className="button button-secondary" disabled={disabled} onClick={() => decide(candidate.candidate_id, "reject")}>{text("放弃候选", "Discard candidate")}</button></footer> : null}
  </section>;
}

export function ParagraphDialogue({ projectId, paragraph }: { projectId: string; paragraph: DialogueParagraph }) {
  const { text } = useUiText();
  const history = useQuery({ queryKey: ["draft-dialogue", projectId, paragraph.paragraph_key],
    queryFn: () => apiRequest<{ messages: Turn[] }>(`/api/v1/projects/${encodeURIComponent(projectId)}/draft/dialogues/${paragraph.paragraph_key}`) });
  return <section>
    <p className="muted">{text("旧记录仅供查看。请在上方章节对话中继续修改。", "Previous records are read-only. Continue revisions in the chapter dialogue above.")}</p>
    {history.error ? <p role="alert">{history.error.message}</p> : null}
    {history.data?.messages.map((turn, i) => <article className="dialogue-turn" key={turn.job_id + i}>
      <p>{turn.message}</p>
      {turn.candidate ? <CandidateComparison candidate={turn.candidate} readOnly decide={() => {}} /> : <p>{turn.error || turn.status}</p>}
    </article>)}
  </section>;
}
