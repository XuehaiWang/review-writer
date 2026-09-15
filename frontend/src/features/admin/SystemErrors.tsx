import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiRequest } from "../../api/client";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

type Failure = { id: string; source: "api" | "job"; email: string | null;
  project_id: string | null; request_id: string; error_code: string;
  message: string; operation: string; status_code: number; created_at: string };

export function SystemErrors({ active = true }: { active?: boolean }) {
  const { text } = useUiText();
  const [input, setInput] = useState("");
  const [q, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [days, setDays] = useState("30");
  const [offset, setOffset] = useState(0);
  const params = new URLSearchParams({ q, source, days, offset: String(offset), limit: "25" });
  const errors = useQuery({ queryKey: ["admin-system-errors", q, source, days, offset],
    queryFn: () => apiRequest<{ items: Failure[]; has_more: boolean }>(`/api/v1/admin/errors?${params}`),
    enabled: active, refetchInterval: active ? 15000 : false });
  const filtered = Boolean(input || q || source || days !== "30" || offset);
  return <section className="surface admin-audit-panel system-errors-panel">
    <header className="system-errors-heading"><div>
      <h2>{text("系统故障记录", "System failures")}</h2>
      <p>{text("按用户、来源和时间查找异常，展开记录查看排查信息。", "Find failures by user, source and time. Expand a record for diagnostic details.")}</p>
    </div><button type="button" className="button button-quiet" disabled={errors.isFetching} onClick={() => void errors.refetch()}>{errors.isFetching ? text("刷新中…", "Refreshing…") : text("刷新记录", "Refresh records")}</button></header>
    <form className="admin-log-filters" onSubmit={(event) => { event.preventDefault(); if (q === input.trim() && offset === 0) void errors.refetch(); else { setQuery(input.trim()); setOffset(0); } }}>
      <label className="system-errors-search">{text("关键词", "Keyword")}<input type="search" value={input} maxLength={160} onChange={(e) => setInput(e.target.value)} placeholder={text("用户邮箱、项目 ID、任务 ID、错误码或请求 ID", "Email, project ID, job ID, error code or request ID")} /></label>
      <label>{text("来源", "Source")}<select value={source} onChange={(e) => { setSource(e.target.value); setOffset(0); }}>
        <option value="">{text("全部", "All")}</option><option value="job">{text("后台任务", "Background jobs")}</option><option value="api">{text("接口请求", "API requests")}</option>
      </select></label>
      <label>{text("时间", "Period")}<select value={days} onChange={(e) => { setDays(e.target.value); setOffset(0); }}>
        {[1, 7, 30].map((d) => <option key={d} value={d}>{text(`最近 ${d} 天`, `Last ${d} days`)}</option>)}</select></label>
      <div className="button-row"><button type="submit" className="button button-primary">{text("查询", "Search")}</button>
        {filtered ? <button type="button" className="button button-quiet" onClick={() => { setInput(""); setQuery(""); setSource(""); setDays("30"); setOffset(0); }}>{text("重置筛选", "Reset filters")}</button> : null}</div>
    </form>
    <div className="system-errors-meta"><span>{errors.data ? text(`本页 ${errors.data.items.length} 条记录`, `${errors.data.items.length} records on this page`) : text("每页最多 25 条记录", "Up to 25 records per page")}</span><span>{text("查看时每 15 秒自动刷新", "Auto-refreshes every 15 seconds while viewing")}{errors.dataUpdatedAt ? ` · ${text("更新于", "Updated")} ${new Date(errors.dataUpdatedAt).toLocaleTimeString()}` : ""}</span></div>
    {errors.error ? <ErrorState error={errors.error} onRetry={() => errors.refetch()} /> : null}
    {errors.isPending ? <p role="status">{text("正在加载…", "Loading…")}</p> : null}
    {!errors.error && errors.data?.items.length === 0 ? <div className="system-errors-empty"><strong>{text("此范围内没有故障记录。", "No failures in this range.")}</strong><p>{text("可以调整关键词、来源或时间范围后重新查询。", "Try a different keyword, source or time range.")}</p></div> : null}
    <div className="system-errors-list">{errors.data?.items.map((item) => <details className="system-error-card" key={`${item.source}-${item.id}`}>
      <summary className="system-error-summary">
        <span className={`system-error-source ${item.source}`}>{item.source === "job" ? text("后台任务", "Background job") : text("接口请求", "API request")}</span>
        <span className="system-error-identity"><strong>{item.operation || (item.source === "job" ? text("任务执行失败", "Job execution failed") : text("接口请求失败", "API request failed"))}</strong><small>{item.email || text("未登录用户", "Anonymous user")}</small></span>
        <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time>
        <span className="system-error-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="system-error-body">
        <dl className="system-error-facts">
          <div><dt>{text("错误码", "Error code")}</dt><dd>{item.error_code || (item.source === "job" ? "JOB_EXECUTION_FAILED" : text("未记录", "Not recorded"))}</dd></div>
          <div><dt>{text("HTTP 状态", "HTTP status")}</dt><dd>{item.status_code || "—"}</dd></div>
          <div><dt>{text("项目 ID", "Project ID")}</dt><dd>{item.project_id || "—"}</dd></div>
          <div><dt>{item.source === "job" ? text("任务 ID", "Job ID") : text("请求 ID", "Request ID")}</dt><dd>{(item.source === "job" ? item.id : item.request_id) || "—"}</dd></div>
        </dl>
        <div className="system-error-message"><h3>{text("错误详情", "Error details")}</h3><pre>{item.message || text("未保留原始响应内容。请使用上方 ID 定位服务日志。", "Raw response not retained. Locate service logs using the ID above.")}</pre></div>
      </div>
    </details>)}</div>
    <footer className="system-errors-footer"><small>{text("接口记录保留 30 天；任务记录按现有规则保留。", "API events are retained for 30 days; job retention is unchanged.")}</small><nav className="system-errors-pagination" aria-label={text("故障记录分页", "Failure pagination")}><button type="button" className="button button-quiet" disabled={!offset || errors.isFetching} onClick={() => setOffset(Math.max(0, offset-25))}>{text("上一页", "Previous")}</button>
      <span>{text(`第 ${Math.floor(offset/25)+1} 页`, `Page ${Math.floor(offset/25)+1}`)}</span><button type="button" className="button button-quiet" disabled={!errors.data?.has_more || errors.isFetching || !!errors.error} onClick={() => setOffset(offset+25)}>{text("下一页", "Next")}</button></nav></footer>
  </section>;
}
