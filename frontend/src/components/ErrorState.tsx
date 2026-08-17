import { useUiText } from "../i18n/useUiText";

type ErrorStateProps = {
  title?: string;
  error: unknown;
  onRetry?: () => void;
};

export function ErrorState({ title, error, onRetry }: ErrorStateProps) {
  const { text } = useUiText();
  const resolvedTitle = title || text("无法加载", "Unable to load");
  const message = error instanceof Error ? error.message : String(error || text("未知错误", "Unknown error"));
  return (
    <section className="error-state" role="alert">
      <strong>{resolvedTitle}</strong>
      <p>{message}</p>
      {onRetry ? (
        <button className="button button-secondary" type="button" onClick={onRetry}>
          {text("重试", "Retry")}
        </button>
      ) : null}
    </section>
  );
}
