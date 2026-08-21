import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody } from "../../api/client";
import { balanceQuery, balanceTransactionsQuery, modelCatalogQuery, providerSettingsQuery, queryKeys, usageSummaryQuery, usageTimelineQuery } from "../../api/queries";
import type { CreditTransaction, ModelTier, Project, UsageTimelineItem } from "../../api/types";
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

function transactionLabel(item: CreditTransaction, text: (zh: string, en: string) => string) {
  if (item.transaction_type === "admin_adjustment") return text("管理员额度调整", "Administrative adjustment");
  if (item.transaction_type === "reservation") return text("任务费用冻结", "Job cost reserved");
  if (item.transaction_type === "settlement") return text("任务实际结算", "Job cost settled");
  if (item.transaction_type === "release") return text("冻结额度释放", "Reservation released");
  return item.transaction_type;
}

export function SettingsPage() {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const { projects, selected: project, selectProject } = useSelectedProject();
  const providers = useQuery(providerSettingsQuery);
  const catalog = useQuery(modelCatalogQuery);
  const usage = useQuery(usageSummaryQuery(project?.project_id || ""));
  const timeline = useQuery(usageTimelineQuery(30, project?.project_id || ""));
  const balance = useQuery(balanceQuery);
  const transactions = useQuery(balanceTransactionsQuery);
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
    void balance.refetch();
    void transactions.refetch();
  };
  const isRefreshing = providers.isFetching || catalog.isFetching || usage.isFetching || timeline.isFetching || balance.isFetching || transactions.isFetching;

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

      <section className="surface balance-dashboard">
        <div className="section-heading">
          <div>
            <span className="step-label">BILLING</span>
            <h2>{text("我的余额与消费明细", "My balance and transactions")}</h2>
            <p>{text("外部模型和 PDF 解析调用前会临时冻结预计费用；成功后按实际用量扣除，失败会自动释放。", "Estimated cost is reserved before external model and PDF parsing calls, then settled to actual usage or released on failure.")}</p>
          </div>
          {balance.data ? <span className="billing-mode-badge billing-mode-live">{text("按实际用量结算", "Usage-based billing")}</span> : null}
        </div>
        {balance.error ? <ErrorState error={balance.error} onRetry={() => balance.refetch()} /> : null}
        <div className="balance-summary-grid">
          <article className="balance-primary-card">
            <span>{text("可用余额", "Available balance")}</span>
            <strong>{balance.data ? `$${Number(balance.data.available_usd).toFixed(4)}` : "—"}</strong>
            <small>USD</small>
          </article>
          <article><span>{text("账户余额", "Account balance")}</span><strong>{balance.data ? `$${Number(balance.data.balance_usd).toFixed(4)}` : "—"}</strong><small>{text("含当前冻结额度", "includes current holds")}</small></article>
          <article><span>{text("任务冻结中", "Reserved for jobs")}</span><strong>{balance.data ? `$${Number(balance.data.reserved_usd).toFixed(4)}` : "—"}</strong><small>{text("任务结束后结算或释放", "settled or released after use")}</small></article>
          <article><span>{text("累计实际扣除", "Lifetime debits")}</span><strong>{balance.data ? `$${Number(balance.data.lifetime_debited_usd).toFixed(4)}` : "—"}</strong><small>{text("包含管理员人工扣减", "includes administrative debits")}</small></article>
        </div>
        {transactions.error ? <ErrorState error={transactions.error} onRetry={() => transactions.refetch()} /> : null}
        <div className="balance-ledger">
          <div className="balance-ledger-head"><strong>{text("最近资金流水", "Recent ledger activity")}</strong><small>{text("只追加审计记录，不会覆盖历史", "Append-only audit history")}</small></div>
          {transactions.data?.items.length ? transactions.data.items.map((item) => {
            const balanceDelta = Number(item.balance_delta_usd);
            const reservedDelta = Number(item.reserved_delta_usd);
            const displayDelta = balanceDelta !== 0 ? balanceDelta : reservedDelta;
            return (
              <article key={item.id}>
                <div>
                  <strong>{transactionLabel(item, text)}</strong>
                  <small>{item.reason || text("系统自动记录", "Recorded automatically")}{item.job_id ? ` · Job ${item.job_id.slice(0, 8)}` : ""}</small>
                </div>
                <div className="balance-ledger-amount">
                  <strong className={displayDelta < 0 ? "negative" : displayDelta > 0 ? "positive" : ""}>{displayDelta > 0 ? "+" : ""}${displayDelta.toFixed(4)}</strong>
                  <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time>
                </div>
              </article>
            );
          }) : <div className="empty-state compact-empty">{text("还没有资金流水。管理员添加额度或任务产生费用后会显示在这里。", "No ledger activity yet. Adjustments and task costs will appear here.")}</div>}
        </div>
      </section>

      <section className="surface usage-dashboard">
        <div className="section-heading">
          <div><span className="step-label">{text("累计统计", "Cumulative statistics")}</span><h2>{text("我的项目用量", "My project usage")}</h2><p>{text("统计服务器内部网关已经完成的文本、图像和 MinerU 请求，用于核对实际结算成本。", "Counts completed text, image, and MinerU requests from the internal gateway so you can verify settled costs.")}</p></div>
          {usage.data ? <span className="billing-mode-badge">{usage.data.billing_mode === "credit" ? text("余额结算已启用", "Credit billing active") : text("仅记录", "Record only")}</span> : null}
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
