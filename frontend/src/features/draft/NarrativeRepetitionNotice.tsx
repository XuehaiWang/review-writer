import { useUiText } from "../../i18n/useUiText";

export type NarrativeRepetitionHint = {
  kind: "wording_overlap" | "repeated_closing";
  paragraph_id?: string;
  other_paragraph_id?: string;
  paragraph_ids?: string[];
  phrase?: string;
  excerpt?: string;
  other_excerpt?: string;
  basis: "surface_wording_only";
};

export type NarrativeRepetitionReport = {
  status: "complete" | "not_ready";
  method: "surface_clues_v1";
  covered_paragraph_count: number;
  total_paragraph_count: number;
  hints: NarrativeRepetitionHint[];
};

export function NarrativeRepetitionNotice({ report, onSelect }: {
  report?: NarrativeRepetitionReport;
  onSelect: (paragraphId: string) => void;
}) {
  const { text } = useUiText();
  if (!report || report.status !== "complete" || !report.hints.length) return null;
  return <details className="message message-warning draft-repetition-notice">
    <summary>{text(`发现 ${report.hints.length} 条可能重复的表达线索`, `${report.hints.length} possible wording repetitions`)}</summary>
    <p>{text("这是免费的措辞提示，不是科学命题重复判定；原文未被修改，也不影响进入终稿。", "These are free wording clues, not a scientific-claim verdict. Saved text is unchanged and approval is unaffected.")}</p>
    {report.covered_paragraph_count < report.total_paragraph_count ? <p>{text("仅检查了可读取的正文段落，未覆盖全文。", "Only readable prose paragraphs were checked; coverage is incomplete.")}</p> : null}
    <ul>{report.hints.map((hint, index) => <li key={`${hint.kind}-${hint.paragraph_id || hint.phrase}-${index}`}>
      {hint.kind === "repeated_closing" ? <>
        {text("多个段落使用相同的收尾开头：", "Several paragraphs share a closing opener: ")}<q>{hint.phrase}</q>
        {hint.paragraph_ids?.[0] ? <button type="button" className="button button-quiet" onClick={() => onSelect(hint.paragraph_ids![0])}>{text("查看段落", "View paragraph")}</button> : null}
      </> : <>
        {text("两处表述相近：", "Similar wording in two places: ")}<q>{hint.excerpt}</q>
        {hint.paragraph_id ? <button type="button" className="button button-quiet" onClick={() => onSelect(hint.paragraph_id!)}>{text("查看后一处", "View later paragraph")}</button> : null}
      </>}
    </li>)}</ul>
  </details>;
}
