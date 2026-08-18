import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody } from "../../api/client";
import { modelCatalogQuery, providerSettingsQuery, queryKeys, usageSummaryQuery, usageTimelineQuery } from "../../api/queries";
import type { ModelTier, Project, UsageTimelineItem } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { useSelectedProject } from "../../components/ProjectSelector";
import { useUiText } from "../../i18n/useUiText";

type UsageMetric = "total_tokens" | "request_count" | "image_count" | "mineru_pages";

function UsageChart({ items }: { items: UsageTimelineItem[] }) {
  const { language, text } = useUiText();
  const [metric, setMetric] = useState<UsageMetric>("total_tokens");
  const metrics: Array<{ id: UsageMetric; label: string }> = [
    { id: "total_tokens", label: "Tokens" },
    { id: "request_count", label: text("文本请求", "Text requests") },
    { id: "image_count", label: text("生成图像", "Generated images") },
    { id: "mineru_pages", label: text("PDF 页数", "PDF pages") },
  ];
  const values = items.map((item) => Number(item[metric] || 0));
  const maximum = Math.max(1, ...values);
  const activeLabel = metrics.find((item) => item.id === metric)?.label || "Tokens";

  return (
    <div className="usage-chart-block">
      <div className="usage-chart-toolbar">
        <div>
          <span className="step-label">{text("近 30 天时间轴", "Last 30 days")}</span>
          <h3>{activeLabel}</h3>
        </div>
        <div className="usage-metric-switch" role="group" aria-label={text("选择用量指标", "Choose usage metric")}>
          {metrics.map((item) => <button key={item.id} type="button" className={metric === item.id ? "active" : ""} onClick={() => setMetric(item.id)}>{item.label}</button>)}
        </div>
      </div>
      <div className="usage-chart-scroll">
        <div className="usage-chart" aria-label={text("每日用量柱状图", "Daily usage bar chart")}>
          {items.map((item, index) => {
            const value = values[index];
            const showDate = index === 0 || index === items.length - 1 || index % 5 === 0;
            const date = new Date(`${item.date}T00:00:00Z`);
            const dateLabel = date.toLocaleDateString(language === "en" ? "en-US" : "zh-CN", { month: "numeric", day: "numeric", timeZone: "UTC" });
            return (
              <div className="usage-bar-column" key={item.date} title={`${item.date} · ${activeLabel}: ${value.toLocaleString()}`}>
                <span className="usage-bar-value">{value > 0 ? value.toLocaleString() : ""}</span>
                <div className="usage-bar-track"><span style={{ height: value > 0 ? `${Math.max(7, (value / maximum) * 100)}%` : "2px" }} /></div>
                <time dateTime={item.date}>{showDate ? dateLabel : ""}</time>
              </div>
            );
          })}
        </div>
      </div>
      <div className="usage-chart-legend"><span />{text("每日实际完成量；没有调用的日期保留为零，便于观察趋势。", "Daily completed usage; zero-use dates remain visible for trend context.")}</div>
    </div>
  );
}

export function SettingsPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const { projects, selected: project, selectProject } = useSelectedProject();
  const providers = useQuery(providerSettingsQuery);
  const catalog = useQuery(modelCatalogQuery);
  const usage = useQuery(usageSummaryQuery);
  const timeline = useQuery(usageTimelineQuery(30));
  const [pendingTier, setPendingTier] = useState<Project["model_tier"]>("terra");
  const [saved, setSaved] = useState(false);
  const records = new Map(providers.data?.items.map((item) => [item.provider_kind, item]));
  const tiers = useMemo(() => {
    const order: Record<string, number> = { terra: 0, luna: 1, sol: 2 };
    return [...(catalog.data?.items || [])].sort((left, right) => order[left.id] - order[right.id]);
  }, [catalog.data?.items]);
  const selectedProjectModel = catalog.data?.items.find(
    (tier) => tier.id === project?.model_tier,
  )?.model;

  useEffect(() => {
    if (project) setPendingTier(project.model_tier);
    setSaved(false);
  }, [project]);

  const saveModel = useMutation({
    mutationFn: () => apiRequest<Project>(`/api/v1/projects/${encodeURIComponent(project!.project_id)}/model-tier`, {
      method: "PATCH",
      ...jsonBody({ model_tier: pendingTier }),
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.projects });
      setSaved(true);
    },
  });

  const refresh = () => {
    void providers.refetch();
    void catalog.refetch();
    void usage.refetch();
    void timeline.refetch();
  };
  const isRefreshing = providers.isFetching || catalog.isFetching || usage.isFetching || timeline.isFetching;

  return (
    <main className="workspace page-container settings-page">
      <div className="workspace-heading settings-heading">
        <div>
          <p className="eyebrow">{text("服务器统一配置", "Server-managed configuration")}</p>
          <h1>{text("模型配置与用量", "Model configuration and usage")}</h1>
          <p className="muted">{text("密钥由服务器统一保管。你只需要为当前项目选择文本模型，任务启动时会锁定所选档位。", "Keys are managed by the server. Choose a text model for the current project; each job locks its tier when it starts.")}</p>
        </div>
        <button className="button button-quiet" type="button" disabled={isRefreshing} onClick={refresh}>{isRefreshing ? text("刷新中…", "Refreshing…") : text("刷新数据", "Refresh data")}</button>
      </div>

      {providers.error ? <ErrorState error={providers.error} onRetry={() => providers.refetch()} /> : null}
      <div className="settings-config-grid">
        <section className="surface settings-service-panel">
          <div className="section-heading compact"><div><span className="step-label">{text("服务状态", "Service status")}</span><h2>{text("服务器连接", "Server connections")}</h2></div></div>
          <div className="settings-service-list">
            {(["text", "image"] as const).map((kind) => {
              const record = records.get(kind);
              const title = kind === "text" ? text("文本生成服务", "Text generation") : text("图像生成服务", "Image generation");
              return (
                <article key={kind}>
                  <span className={`service-status-dot ${record?.enabled ? "online" : ""}`} />
                  <div><strong>{title}</strong><small>{kind === "text" ? selectedProjectModel || text("按当前项目选择", "Selected per project") : record?.model_name || text("由服务器管理员维护", "Managed by server administrator")}</small></div>
                  <em>{record?.enabled ? text("已就绪", "Ready") : text("未配置", "Not configured")}</em>
                </article>
              );
            })}
          </div>
          <p className="settings-security-note">{text("浏览器只能看到是否可用，不会获得 API Key、内部网关地址或供应商凭据。", "The browser can only see availability; API keys, internal gateway URLs, and provider credentials are never returned.")}</p>
        </section>

        <section className="surface model-selection-panel">
          <div className="model-selection-head">
            <div><span className="step-label">{text("当前项目", "Current project")}</span><h2>{text("选择文本模型", "Choose text model")}</h2></div>
            <label className="settings-project-select">
              <span>{text("应用到", "Apply to")}</span>
              <select value={project?.project_id || ""} disabled={!projects.data?.items.length} onChange={(event) => selectProject(event.target.value)}>
                {projects.data?.items.map((item) => <option key={item.project_id} value={item.project_id}>{item.slug}</option>)}
              </select>
            </label>
          </div>
          {!project ? <div className="empty-state compact-empty">{text("请先创建项目，再选择模型。", "Create a project before choosing a model.")}</div> : null}
          <div className="model-tier-grid">
            {tiers.map((tier: ModelTier) => (
              <button className={pendingTier === tier.id ? "model-tier-card selected" : "model-tier-card"} type="button" key={tier.id} disabled={!project || saveModel.isPending} onClick={() => { setPendingTier(tier.id); setSaved(false); }} aria-pressed={pendingTier === tier.id}>
                <span className="model-tier-check">{pendingTier === tier.id ? "✓" : ""}</span>
                <strong>{tier.id === "terra" ? "Terra" : tier.id === "luna" ? "Luna" : "Sol"}</strong>
                <small>{text(tier.description_zh, tier.description_en)}</small>
                <em>Input ${tier.input_usd_per_million} · Output ${tier.output_usd_per_million}</em>
              </button>
            ))}
          </div>
          <div className="model-save-row">
            <div>
              <strong>{text("当前已保存：", "Currently saved: ")}{project?.model_tier ? project.model_tier.toUpperCase() : "—"}</strong>
              <small>{text("只影响之后启动的任务，不改变正在运行的任务。", "Only future jobs are affected; running jobs remain unchanged.")}</small>
            </div>
            <button className="button button-primary" type="button" disabled={!project || saveModel.isPending || pendingTier === project.model_tier} onClick={() => saveModel.mutate()}>{saveModel.isPending ? text("保存中…", "Saving…") : text("确认并保存", "Confirm and save")}</button>
          </div>
          {saved ? <p className="message" role="status">{text("文本模型已保存。", "Text model saved.")}</p> : null}
          {saveModel.error ? <p className="message message-error" role="alert">{saveModel.error.message}</p> : null}
        </section>
      </div>

      <section className="surface usage-dashboard">
        <div className="section-heading">
          <div><span className="step-label">{text("累计统计", "Cumulative statistics")}</span><h2>{text("我的用量", "My usage")}</h2><p>{text("统计已经由服务器内部网关完成的请求；当前仅记录费用，不自动扣费。", "Counts completed internal-gateway requests. Costs are recorded only and are not automatically charged.")}</p></div>
          {usage.data ? <span className="billing-mode-badge">{text("仅记录，不扣费", "Record only")}</span> : null}
        </div>
        {usage.error ? <ErrorState error={usage.error} onRetry={() => usage.refetch()} /> : null}
        <div className="usage-summary-grid">
          <article><span>{text("文本请求", "Text requests")}</span><strong>{usage.data?.request_count.toLocaleString() ?? "—"}</strong><small>{text("累计完成次数", "completed")}</small></article>
          <article><span>Tokens</span><strong>{usage.data?.total_tokens.toLocaleString() ?? "—"}</strong><small>{usage.data ? `${usage.data.cached_input_tokens.toLocaleString()} cached` : "—"}</small></article>
          <article><span>{text("生成图像", "Generated images")}</span><strong>{usage.data?.image_count.toLocaleString() ?? "—"}</strong><small>{usage.data ? `${usage.data.image_request_count.toLocaleString()} requests` : "—"}</small></article>
          <article><span>{text("PDF 解析", "PDF parsing")}</span><strong>{usage.data?.mineru_billable_pages.toLocaleString() ?? "—"}</strong><small>{text("累计页数", "pages")}</small></article>
          <article className="usage-cost-card"><span>{text("估算成本", "Estimated cost")}</span><strong>{usage.data ? `$${Number(usage.data.estimated_cost_usd).toFixed(4)}` : "—"}</strong><small>USD</small></article>
        </div>
        {timeline.error ? <ErrorState error={timeline.error} onRetry={() => timeline.refetch()} /> : null}
        {timeline.data ? <UsageChart items={timeline.data.items} /> : <div className="usage-chart-loading">{text("正在加载时间轴…", "Loading timeline…")}</div>}
      </section>
    </main>
  );
}
