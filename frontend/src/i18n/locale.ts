import { usePreferences } from "../state/preferences";

/** Formatting follows the UI selection, not the browser's default language. */
export function uiLocale() {
  return usePreferences.getState().language === "en" ? "en-US" : "zh-CN";
}
