import { useCallback } from "react";

import { usePreferences } from "../state/preferences";

/**
 * Small bilingual helper for workflow copy that contains dynamic values.
 * Stable navigation labels continue to use the keyed message catalogue, while
 * feature-local labels keep both translations beside the component that owns
 * them. This prevents feature pages from silently falling back to hard-coded
 * Chinese when the application language changes.
 */
export function useUiText() {
  const language = usePreferences((state) => state.language);
  const text = useCallback((zh: string, en: string) => (language === "en" ? en : zh), [language]);
  return { language, text };
}
