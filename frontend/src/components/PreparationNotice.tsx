import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { balanceQuery, meQuery, modelCatalogQuery, providerSettingsQuery } from "../api/queries";
import { useUiText } from "../i18n/useUiText";

/** Read-only hints; backend admission remains authoritative. No provider test calls. */
export function PreparationNotice({ kind, model, checkCredit = true }: { kind: "text" | "mineru"; model?: string; checkCredit?: boolean }) {
  const { text } = useUiText();
  const [params] = useSearchParams();
  const project = params.get("project");
  const identity = useQuery(meQuery);
  const catalog = useQuery({ ...modelCatalogQuery, enabled: kind === "text" });
  const providers = useQuery({ ...providerSettingsQuery, enabled: kind === "mineru" });
  const balance = useQuery({ ...balanceQuery, enabled: checkCredit });
  const chosen = catalog.data?.items.find(item => item.id === (model || catalog.data?.default_tier));
  const parser = providers.data?.items.find(item => item.provider_kind === "mineru");
  const unavailable = kind === "text" ? Boolean(catalog.data && (!chosen || chosen.enabled === false))
    : Boolean(providers.data && (!parser || !parser.enabled || !parser.api_key_configured));
  const noCredit = checkCredit && balance.data && Number(balance.data.available_usd) <= 0;
  const unknown = (kind === "text" ? catalog.isError : providers.isError) || (checkCredit && balance.isError);
  if (!unavailable && !noCredit && !unknown) return null;
  const admin = identity.data?.roles?.includes("admin");
  return <aside className="message message-warning" role="status" aria-label={text("开始前请检查", "Before starting")}>
    {unavailable ? <p>{kind === "text" ? text("所选文本模型暂不可用。请选择可用模型，或联系管理员恢复服务。", "The selected text model is unavailable. Choose another model or ask an administrator to restore it.") : text("PDF 解析服务尚未就绪，请联系管理员配置后再导入。无需自行填写 API 密钥。", "PDF parsing is not ready. Ask an administrator to configure it before importing; you do not need an API key.")}</p> : null}
    {noCredit ? <p>{text("当前没有可用额度，请联系管理员补充。已保存的文献和正文仍可查看。", "No credit is available. Ask an administrator to add credit; saved papers and text remain accessible.")}</p> : null}
    {unknown ? <p>{text("暂时无法确认准备状态，已有内容不受影响；可刷新状态后再试。", "Readiness could not be checked. Saved work is unaffected; refresh the status and retry.")}</p> : null}
    <div className="button-row"><Link className="button button-secondary" to={`${admin ? "/admin" : "/settings"}${project ? `?project=${encodeURIComponent(project)}` : ""}`}>{admin ? text("管理服务与额度", "Manage services and credit") : text("查看服务与余额", "View services and balance")}</Link>
      <button type="button" className="button button-quiet" onClick={() => { if (kind === "text") void catalog.refetch(); else void providers.refetch(); if (checkCredit) void balance.refetch(); }}>{text("刷新状态", "Refresh status")}</button></div>
  </aside>;
}
