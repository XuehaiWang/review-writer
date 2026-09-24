import { diagnosticText } from "../i18n/diagnostics";
import { useUiText } from "../i18n/useUiText";
import { useState } from "react";

export function LocalizedError({ error }: { error: unknown }) {
  const { text, language } = useUiText();
  const [expanded, setExpanded] = useState(false);
  const raw = error instanceof Error ? error.message : String(error || "");
  const summary = diagnosticText(error, language);
  return <span className="localized-diagnostic">{summary}
    {raw && raw !== summary && <> <button type="button" className="button button-quiet" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{text("技术详情", "Technical details")}</button>{expanded && <span className="diagnostic-raw">{raw}</span>}</>}
  </span>;
}
