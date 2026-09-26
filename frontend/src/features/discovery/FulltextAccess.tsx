import { useUiText } from "../../i18n/useUiText";
import type { DiscoveryRow } from "./model/discoveryModel";

type Text = (zh: string, en: string) => string;

export function articleSourceUrl(row: DiscoveryRow): string {
  for (const raw of [row.landing_url, row.url, row.pdf_url]) {
    try {
      const url = new URL(String(raw || ""));
      if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password) return url.href;
    } catch { /* Try DOI when the supplied source link is invalid. */ }
  }
  const doi = String(row.doi || "").trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "").replace(/^doi:\s*/i, "");
  return /^10\.\d{4,9}\/\S+$/i.test(doi) ? `https://doi.org/${encodeURI(doi).replace(/#/g, "%23").replace(/\?/g, "%3F")}` : "";
}

export function fulltextLabel(row: DiscoveryRow, text: Text): string {
  if (row.fulltext_access?.state === "acquiring") return text("正在获取全文", "Acquiring full text");
  if (row.fulltext_access?.state === "downloaded") return text("全文已入库 · 解析状态见文献库", "Downloaded · check parsing in Library");
  if (row.access_status === "downloaded_to_library") return text("已入库", "In library");
  switch (row.availability?.state) {
    case "checking": return text("正在检查能否下载…", "Checking PDF access…");
    case "available": return text("可下载 · 已验证 PDF 链接", "Downloadable · PDF link verified");
    case "restricted": return text("来源限制访问", "Source restricts access");
    case "not_found": return text("暂未找到开放 PDF", "No open PDF found");
    case "unknown": return text("暂时无法确认能否下载", "Download availability uncertain");
  }
  if (row.fulltext_access?.state === "not_acquired") return text("暂未获取全文", "Full text not acquired");
  if (row.access_status === "open_access_downloadable" || row.pdf_url) return text("开放全文线索 · 可尝试下载", "Open full-text lead · try download");
  return text("全文获取待确认", "Full-text access unverified");
}

export function fulltextHint(row: DiscoveryRow, text: Text): string {
  if (!row.fulltext_access || row.fulltext_access.state === "not_acquired") {
    switch (row.availability?.state) {
      case "checking": return text("后台自动检查，无需先点击下载。", "Checking automatically before download.");
      case "available": return text("最近检查已返回 PDF 文件头；可直接打开下载，链接可能随时间失效。", "The latest check returned a PDF header. Open it to download; availability may change.");
      case "restricted": return text("来源拒绝自动访问，不一定是付费墙；可打开原文使用已有权限获取。", "The source rejected automated access, not necessarily a paywall. Open the article with your existing access.");
      case "not_found": return text("已检查的链接及开放来源未找到可用 PDF；可打开原文或手动上传。", "Checked links and open sources yielded no PDF. Open the article or upload a copy.");
      case "unknown": return text("网络超时、限流或来源异常，不能据此判断不可下载。", "A network or source issue prevented verification; this does not mean the PDF is unavailable.");
    }
  }
  const reason = row.fulltext_access?.reason;
  if (reason === "rate_limited") return text("来源暂时限流，可稍后重试或打开原文。", "The source is rate-limited. Retry later or open the article.");
  if (reason === "timeout") return text("来源连接超时，可稍后重试。", "The source timed out. Retry later.");
  if (reason === "access_restricted") return text("来源限制自动访问；可打开原文，使用已有机构权限获取后上传。", "Automated access was restricted. Open the article and upload a copy obtained through your institutional access.");
  if (reason === "broken_link") return text("来源链接已失效；可打开文章页面查找其他合法副本。", "The download link is unavailable. Open the article to look for a lawful alternative.");
  if (reason === "no_valid_pdf") return text("已尝试的来源未返回有效 PDF，不能视为已取得全文。", "The attempted sources did not return a valid PDF; full text has not been acquired.");
  if (reason === "interrupted") return text("获取已中断，可按需重试。", "Acquisition was interrupted. Retry if needed.");
  if (row.fulltext_access?.state === "not_acquired") return text("暂未取得可用全文；书目已保留，可打开原文或自行上传 PDF。", "No usable full text was acquired. The record is retained; open the source or upload a PDF.");
  if (row.fulltext_access?.state === "downloaded") return text("下载不代表解析完成，可在文献库查看处理进度。", "Downloading does not imply parsing is complete. Check progress in Library.");
  if (row.access_status === "institution_required") return text("可能需要机构权限，也可先尝试查找合法开放副本。", "Institutional access may be needed; you can first look for a lawful open copy.");
  return text("开放标记或链接尚未验证；只有取得有效全文后才能用于全文分析。", "Open-access flags and links are unverified; full-text analysis requires a valid acquired document.");
}

export function FulltextAccess({ row, processing = false, disabled = false, showStatus = true, libraryUrl, onDownload }: {
  row: DiscoveryRow; processing?: boolean; disabled?: boolean; showStatus?: boolean; libraryUrl: string; onDownload: () => void;
}) {
  const { text } = useUiText();
  const current = processing ? { ...row, fulltext_access: { state: "acquiring" as const, reason: "" } } : row;
  const url = articleSourceUrl(row);
  const acquired = row.fulltext_access?.state === "downloaded" || row.access_status === "downloaded_to_library";
  const searchable = Boolean(row.doi || row.pdf_url || row.access_status === "open_access_downloadable");
  return <div className="discovery-fulltext" onClick={(event) => event.stopPropagation()}>
    {showStatus ? <span className={`fulltext-badge ${acquired || row.availability?.state === "available" ? "available" : "pending"}`} role="status">{fulltextLabel(current, text)}</span> : null}
    <p>{fulltextHint(current, text)}</p>
    <div className="result-actions">
      {!acquired && row.availability?.state === "available" && row.availability.pdf_url ? <a className="button button-secondary" href={row.availability.pdf_url} target="_blank" rel="noopener noreferrer">{text("下载 PDF", "Download PDF")}</a> : null}
      {!acquired && searchable ? <button type="button" className="button button-secondary" disabled={disabled || processing} onClick={onDownload}>
        {processing ? text("处理中…", "Processing…") : row.fulltext_access?.state === "not_acquired" ? text("重试获取", "Retry acquisition") : row.access_status === "open_access_downloadable" || row.pdf_url ? text("尝试下载并解析", "Try download and parse") : text("查找开放全文", "Find open full text")}
      </button> : null}
      {url ? <a className="button button-secondary" href={url} target="_blank" rel="noopener noreferrer">{text("打开原文", "Open article")}</a> : null}
      <a className="button button-ghost" href={libraryUrl}>{acquired ? text("查看文献库", "View library") : text("上传 PDF", "Upload PDF")}</a>
    </div>
  </div>;
}
