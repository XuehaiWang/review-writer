import { sectionErrorMessage } from "./sectionErrorMessage";
import { useUiText } from "../../i18n/useUiText";

type Props = {
  current: boolean;
  active: boolean;
  resumable: boolean;
  progress: number;
  total: number;
  generating: boolean;
  regenerating: boolean;
  confirming: boolean;
  onGenerate: () => void;
  onRegenerate: () => void;
  onConfirm: () => void;
  error?: { message: string } | null;
  pendingHeadings?: string[];
};

export function SectionStageActions({
  current, active, resumable, progress, total, generating, regenerating, confirming,
  onGenerate, onRegenerate, onConfirm, error, pendingHeadings = [],
}: Props) {
  const { text } = useUiText();
  const title = active
    ? text(current ? "正在重新生成章节" : "正在生成章节", current ? "Regenerating sections" : "Generating sections")
    : current && resumable
      ? text("部分章节待继续", "Some sections need continuation")
      : current
      ? text("确认或重新生成章节", "Confirm or regenerate sections")
      : resumable
        ? text("继续未完成章节", "Resume unfinished sections")
        : text("生成所有章节", "Generate all sections");
  const detail = active
    ? text(`生成中 ${progress}/${total}`, `Generating ${progress}/${total}`)
    : current && resumable
      ? text(`已保留 ${progress}/${total} 个章节；继续时只处理失败章节及依赖它们的总结。`, `${progress}/${total} sections are retained; continuation processes only failed sections and their dependent conclusion.`)
      : current
      ? text("可以确认当前版本进入图像处理，也可以使用当前 Blueprint 重新生成全部章节。", "Confirm the current version for figure processing, or regenerate every section from the current blueprint.")
      : resumable
        ? text(`已保留 ${progress}/${total} 个章节，继续时仅处理未完成章节。`, `${progress}/${total} sections are checkpointed; resume processes only unfinished sections.`)
        : text("根据当前 Blueprint 写作要求生成全部章节。", "Generate every section from the current blueprint requirements.");

  return <div className="stage-action-bar">
    <div><strong>{title}</strong><p>{detail}</p>
      {current && pendingHeadings.length > 0 ? <p role="status">
        {text(`本次不包含：${pendingHeadings.join("、")}。确认后，其余正文进入下一阶段；这些章节仍保留在大纲中。`,
          `Not included this time: ${pendingHeadings.join(", ")}. Confirm to continue with the available prose; these sections remain in your outline.`)}
      </p> : null}
      {error ? <p role="alert" className="message message-error section-action-error">{sectionErrorMessage(error.message, text)}</p> : null}
    </div>
    <div className="stage-action-buttons">
      {current ? <>
        {resumable ? <button className="button button-primary" type="button" disabled={generating || regenerating || confirming || active} onClick={onGenerate}>
          {generating ? text("正在继续…", "Resuming…") : text("继续生成失败章节", "Continue failed sections")}
        </button> : null}
        <button className="button button-secondary" type="button" disabled={generating || regenerating || confirming || active} onClick={onRegenerate}>
          {active ? text("正在重新生成…", "Regenerating…") : regenerating ? text("正在提交…", "Submitting…") : text("重新生成全部章节", "Regenerate all sections")}
        </button>
        <button className={`button ${resumable ? "button-secondary" : "button-primary"}`} type="button" disabled={confirming || generating || regenerating || active} onClick={onConfirm}>
          {confirming ? text("确认中…", "Confirming…") : text("确认并进入图像处理", "Confirm and enter figure processing")}
        </button>
      </> : <button className="button button-primary" type="button" disabled={generating || active} onClick={onGenerate}>
        {active ? text("正在生成…", "Generating…") : generating ? text("正在提交…", "Submitting…") : resumable ? text("继续生成", "Resume generation") : text("生成所有章节草稿", "Generate all section drafts")}
      </button>}
    </div>
  </div>;
}
