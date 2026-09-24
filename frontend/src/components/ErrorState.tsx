import { useUiText } from "../i18n/useUiText";
import { ApiError } from "../api/client";
import { StageNotReady } from "./StageNotReady";
import { diagnosticText } from "../i18n/diagnostics";

type ErrorStateProps = {
  title?: string;
  error: unknown;
  onRetry?: () => void;
};

export function ErrorState({ title, error, onRetry }: ErrorStateProps) {
  const { text, language } = useUiText();
  if (error instanceof ApiError && error.code === "WORKFLOW_STAGE_NOT_READY") {
    return <StageNotReady details={error.details} onRefresh={onRetry} />;
  }
  const resolvedTitle = title || text("无法加载", "Unable to load");
  const message = error instanceof Error ? error.message : String(error || text("未知错误", "Unknown error"));
  return (
    <section className="error-state" role="alert">
      <strong>{resolvedTitle}</strong>
      <p>{diagnosticText(error, language)}</p>
      <details><summary>{text("技术详情（保留原文）", "Technical details (original text)")}</summary><pre>{message}</pre></details>
      {onRetry ? (
        <button className="button button-secondary" type="button" onClick={onRetry}>
          {text("重试", "Retry")}
        </button>
      ) : null}
    </section>
  );
}
