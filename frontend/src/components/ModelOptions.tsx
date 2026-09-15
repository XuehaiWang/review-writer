import { useQuery } from "@tanstack/react-query";
import { modelCatalogQuery } from "../api/queries";
import { useUiText } from "../i18n/useUiText";

export function ModelOptions({ selected = "" }: { selected?: string }) {
  const catalog = useQuery(modelCatalogQuery);
  const { text } = useUiText();
  return <>
    {selected && !catalog.data?.items.some(item => item.id === selected) ? <option value={selected} disabled>{selected}</option> : null}
    {catalog.data?.items.map(item => <option key={item.id} value={item.id} disabled={item.enabled === false}>
      {text(item.label_zh, item.label_en) || item.model}{item.enabled === false ? text("（已停用）", " (disabled)") : ""}
    </option>)}
  </>;
}
