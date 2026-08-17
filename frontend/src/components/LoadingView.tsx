import { usePreferences } from "../state/preferences";
import { translate } from "../i18n/messages";

export function LoadingView() {
  const language = usePreferences((state) => state.language);
  return (
    <main className="center-card" aria-live="polite">
      <div className="spinner" aria-hidden="true" />
      <h1>{translate(language, "loadingTitle")}</h1>
      <p>{translate(language, "loadingBody")}</p>
    </main>
  );
}
