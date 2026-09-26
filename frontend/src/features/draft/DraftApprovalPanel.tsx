import { useUiText } from "../../i18n/useUiText";
import { hardGateDetails, type HardGateFinding } from "./hardGateDetails";

type ApprovalFinding = { issue_id?: string; paragraph_id?: string; section_id?: string; diagnosis?: string; message?: string };
export type ApprovalQuality = Record<string, unknown> & {
  current?: boolean; score?: number; approval_findings?: ApprovalFinding[];
};
type Props = { quality: ApprovalQuality; approved: boolean; onReview: (paragraphId: string) => void };

export function DraftApprovalPanel({ quality, approved, onReview }: Props) {
  const { text } = useUiText();
  const details = hardGateDetails(quality);
  const gateLabel = (gateId: string) => gateId === "paragraph_readability_or_source_failures"
    ? text("段落可读性或来源校验未通过", "Paragraph readability or source validation failed")
    : gateId;
  const gateDiagnosis = (finding: HardGateFinding) => {
    const diagnosis = finding.diagnosis || text("请检查该段落。", "Review this paragraph.");
    const wordRange = /^Paragraph has (\d+) words; configured range is (\d+)-(\d+)\.$/.exec(diagnosis);
    if (wordRange) {
      return text(
        `段落共 ${wordRange[1]} 个英文词，未达到配置范围 ${wordRange[2]}–${wordRange[3]}。`,
        diagnosis,
      );
    }
    if (diagnosis === "No readable local source is registered for at least one cited paper.") {
      return text("该段引用的至少一篇论文没有可读取的本地来源，需要核对或重新解析 PDF。", diagnosis);
    }
    return diagnosis;
  };
  const gateRoute = (route?: string) => ({
    section_rewrite: text("补写或重写该段", "Expand or rewrite this paragraph"),
    local_source_recheck: text("核对本地论文来源", "Check the local paper source"),
    final_polish: text("最终润色", "Final polish"),
  }[route || ""] || route || text("人工检查", "Manual review"));

  return (
    <section className={approved ? "approval-card good" : "approval-card"}>
      <h2>{approved ? text("初稿已人工确认", "Draft manually approved") : text("等待人工确认", "Waiting for human approval")}</h2>
      <p>{text("确认的是当前已保存正文，不包含未保存编辑和未采用候选。", "Approval covers saved text, not unsaved edits or unaccepted candidates.")}</p>
      {Number(quality.blocking_issue_count || 0) > 0 ? (
        <p className="message message-warning">{text(
          `仍有 ${quality.blocking_issue_count} 项事实或论证提醒。可知悉后确认进入终稿，问题记录会保留。`,
          `There are ${quality.blocking_issue_count} substantive findings. You may acknowledge them and continue; the findings remain recorded.`,
        )}</p>
      ) : null}
      {details.length ? <>
        <p className="message message-warning">{text(
          "以下质量检查未通过，供核对参考，不阻止人工确认：",
          "The following quality checks need attention but do not prevent manual approval:",
        )}</p>
        <div className="hard-gate-detail-list">
          {details.map((detail) => (
            <article key={detail.gate_id} className="hard-gate-detail">
              <header>
                <strong>{gateLabel(detail.gate_id)}</strong>
                {detail.findings.length ? <span>{text(`涉及 ${detail.findings.length} 个段落`, `${detail.findings.length} paragraphs`)}</span> : null}
              </header>
              {detail.findings.length ? <div className="hard-gate-paragraphs">
                {detail.findings.map((finding, index) => (
                  <details key={`${detail.gate_id}-${finding.paragraph_id}-${finding.rule || index}`}>
                    <summary><strong>{finding.paragraph_id}</strong> · {gateDiagnosis(finding).slice(0, 100)}</summary>
                    <p>{gateDiagnosis(finding)}</p>
                    <footer>
                      <small>{text("建议处理：", "Suggested action: ")}{gateRoute(finding.route)}</small>
                      <button className="button button-secondary" type="button" onClick={() => onReview(finding.paragraph_id)}>
                        {text("在章节对话中处理", "Review in chapter dialogue")}
                      </button>
                    </footer>
                  </details>
                ))}
              </div> : <p className="muted">{text(
                `检查标识：${detail.gate_id}。旧报告没有段落明细，可在“章节对话”查看；仍可知悉后继续。`,
                `Check: ${detail.gate_id}. This legacy report has no paragraph details; you can review Chapter dialogue or acknowledge and continue.`,
              )}</p>}
            </article>
          ))}
        </div>
      </> : null}
      {quality.approval_findings?.length ? (
        <div className="hard-gate-detail-list">
          {quality.approval_findings.map((finding, index) => (
            <article className="hard-gate-detail" key={finding.issue_id || `${finding.paragraph_id}-${index}`}>
              <strong>{finding.paragraph_id || finding.section_id || text("全文", "Manuscript")}</strong>
              <p>{finding.diagnosis || finding.message || text("请查看评估详情。", "See evaluation details.")}</p>
              {finding.paragraph_id ? <button className="button button-secondary" type="button" onClick={() => onReview(finding.paragraph_id!)}>
                {text("查看对应段落", "View paragraph")}
              </button> : null}
            </article>
          ))}
        </div>
      ) : null}
      <p className="muted">{text(
        "底部确认操作允许继续，但不代表系统已核实所有科学事实；正文和问题记录不会因此改变。",
        "The confirmation action below allows progression but does not verify every scientific claim or alter saved text and findings.",
      )}</p>
    </section>
  );
}
