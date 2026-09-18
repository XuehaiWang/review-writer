import { useState } from "react";
import { ModelCatalogEditor } from "./ModelCatalogEditor";
import { TextConnectionsEditor } from "./TextConnectionsEditor";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody, newIdempotencyKey } from "../../api/client";
import {
  adminUsageQuery,
  adminUsersQuery,
  adminProviderAuditQuery,
  adminProviderSettingsQuery,
  meQuery,
  queryKeys,
} from "../../api/queries";
import type {
  AdminUser,
  AdminProviderTestResult,
  CreditTransaction,
  ProviderKind,
  ProviderSettings,
} from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";
import { SystemErrors } from "./SystemErrors";
import { StorageMaintenance } from "./StorageMaintenance";

type ProviderDraft = {
  base_url: string;
  model_name: string;
  wire_api: string;
  api_key: string;
  enabled: boolean;
};

const providerSections: { id: ProviderKind; zh: string; en: string; description: string; descriptionEn: string }[] = [
  { id: "text", zh: "文本与模型", en: "Text & models", description: "接口连接、可选模型与价格", descriptionEn: "Connection, models and prices" },
  { id: "image", zh: "图像生成", en: "Images", description: "图像生成与重绘服务", descriptionEn: "Generation and redrawing" },
  { id: "mineru", zh: "文档解析", en: "Document parsing", description: "PDF 解析服务", descriptionEn: "PDF parsing service" },
  { id: "embedding", zh: "向量检索", en: "Embeddings", description: "语义检索的向量服务", descriptionEn: "Semantic retrieval provider" },
];

function draftFrom(record: ProviderSettings): ProviderDraft {
  return {
    base_url: record.base_url,
    model_name: record.model_name,
    wire_api: record.wire_api,
    api_key: "",
    enabled: record.enabled,
  };
}

function ProviderEditor({ record }: { record: ProviderSettings }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [changes, setDraft] = useState<ProviderDraft | null>(null);
  const draft = changes ?? draftFrom(record);
  const [message, setMessage] = useState("");

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.adminProviderSettings }),
      queryClient.invalidateQueries({ queryKey: queryKeys.providerSettings }),
      queryClient.invalidateQueries({ queryKey: queryKeys.adminProviderAudit }),
    ]);
  };
  const save = useMutation({
    mutationFn: () => apiRequest<ProviderSettings>(
      `/api/v1/admin/provider-settings/${record.provider_kind}`,
      {
        method: "PUT",
        ...jsonBody({
          ...draft,
          api_key: draft.api_key.trim() || null,
        }),
      },
    ),
    onSuccess: async (saved) => {
      queryClient.setQueryData(queryKeys.adminProviderSettings, (current: { items: ProviderSettings[] } | undefined) => current ? { ...current, items: current.items.map(item => item.provider_kind === saved.provider_kind ? saved : item) } : current);
      setDraft(null);
      setMessage(text("配置已保存，之后启动的任务会立即使用新配置。", "Saved. New tasks will use this configuration immediately."));
      await refresh();
    },
  });
  const reset = useMutation({
    mutationFn: () => apiRequest<ProviderSettings>(
      `/api/v1/admin/provider-settings/${record.provider_kind}`,
      { method: "DELETE" },
    ),
    onSuccess: async () => {
      setDraft(null);
      setMessage(text("已恢复服务器环境变量配置。", "Restored the server environment fallback."));
      await refresh();
    },
  });
  const testConnection = useMutation({
    mutationFn: () => apiRequest<AdminProviderTestResult>(
      `/api/v1/admin/provider-settings/${record.provider_kind}/test`,
      { method: "POST" },
    ),
    onSuccess: async (result) => {
      setMessage(result.ok
        ? text(`连接成功，耗时 ${result.latency_ms} ms。`, `Connected in ${result.latency_ms} ms.`)
        : text(`连接失败：${result.message}`, `Connection failed: ${result.message}`));
      await queryClient.invalidateQueries({ queryKey: queryKeys.adminProviderAudit });
    },
  });
  const busy = save.isPending || reset.isPending || testConnection.isPending;
  const title = record.provider_kind === "text"
    ? text("文本生成服务", "Text generation")
    : record.provider_kind === "image"
      ? text("图像生成服务", "Image generation")
      : record.provider_kind === "embedding"
        ? text("语义检索向量服务", "Semantic retrieval embeddings")
        : text("MinerU 文档解析", "MinerU parsing");

  return (
    <article className="admin-provider-card">
      <header>
        <div>
          <span className="step-label">{record.provider_kind.toUpperCase()}</span>
          <h2>{title}</h2>
        </div>
        <div className="admin-provider-status">
          <span className={`service-status-dot ${record.enabled ? "online" : ""}`} />
          <strong>{record.enabled ? text("已启用", "Enabled") : text("未启用", "Disabled")}</strong>
          <small>{record.source === "database" ? text("后台配置", "Admin configuration") : text("服务器默认配置", "Server defaults")}</small>
        </div>
      </header>

      <div className="admin-provider-form">
        {record.provider_kind !== "mineru" ? <label className="admin-wide-field">
          <span>{text("API Base URL", "API base URL")}</span>
          <input
            type="url"
            value={draft.base_url}
            disabled={busy}
            onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
          />
        </label> : <p className="muted admin-wide-field">{text("使用固定的 MinerU 解析接口，只需配置密钥并启用服务。", "Uses the fixed MinerU endpoint. Configure the key and enable the service.")}</p>}
        {record.provider_kind !== "mineru" ? (
          <label>
            <span>{text("接口协议", "Wire API")}</span>
            <select
              value={draft.wire_api}
              disabled={busy}
              onChange={(event) => setDraft({ ...draft, wire_api: event.target.value })}
            >
              {record.provider_kind === "text" ? <option value="responses">Responses</option> : null}
              {record.provider_kind !== "embedding" ? <option value="chat-completions">Chat Completions</option> : null}
              {record.provider_kind === "image" ? <option value="images">Images API</option> : null}
              {record.provider_kind === "embedding" ? <option value="embeddings">Embeddings API</option> : null}
            </select>
          </label>
        ) : null}
        {record.provider_kind === "image" || record.provider_kind === "embedding" ? (
          <label>
            <span>{record.provider_kind === "embedding" ? text("向量模型", "Embedding model") : text("图像模型", "Image model")}</span>
            <input
              value={draft.model_name}
              disabled={busy}
              onChange={(event) => setDraft({ ...draft, model_name: event.target.value })}
            />
          </label>
        ) : null}
        <label className={record.provider_kind === "text" ? undefined : "admin-wide-field"}>
          <span>API Key</span>
          <input
            type="password"
            autoComplete="new-password"
            value={draft.api_key}
            disabled={busy}
            placeholder={record.api_key_configured
              ? text(`已保存 ${record.api_key_hint}；留空表示不更换`, `Saved ${record.api_key_hint}; leave blank to keep it`)
              : text("输入服务器 API Key", "Enter the server API key")}
            onChange={(event) => setDraft({ ...draft, api_key: event.target.value })}
          />
        </label>
        <label className="admin-provider-toggle">
          <input
            type="checkbox"
            checked={draft.enabled}
            disabled={busy}
            onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
          />
          <span>{text("允许新任务使用此服务", "Allow new jobs to use this provider")}</span>
        </label>
      </div>

      <footer>
        <div>
          <small>{changes ? text("有未保存修改；保存后再测试连接。", "Unsaved changes; save before testing.") : text("启用不代表连接正常，可测试已保存的配置。", "Enabled does not confirm connectivity. Test the saved configuration.")}</small>
          {message ? <p className="admin-provider-message" role="status">{message}</p> : null}
          {save.error || reset.error || testConnection.error ? (
            <p className="message message-error" role="alert">{(save.error || reset.error || testConnection.error)?.message}</p>
          ) : null}
        </div>
        <div className="admin-provider-actions">
          <button className="button button-quiet" type="button" disabled={busy || !record.enabled || !!changes} onClick={() => testConnection.mutate()}>
            {testConnection.isPending ? text("测试中…", "Testing…") : text("测试连接", "Test connection")}
          </button>
          <button className="button button-primary" type="button" disabled={busy || !changes} onClick={() => save.mutate()}>
            {save.isPending ? text("保存中…", "Saving…") : text("保存服务配置", "Save provider")}
          </button>
        </div>
        <details className="admin-advanced">
          <summary>{text("高级维护", "Advanced maintenance")}</summary>
          <p className="muted">{text("恢复后将移除该服务的后台覆盖设置，后续任务改用服务器环境配置。", "Restoring removes this provider's admin override. New tasks use server environment settings.")}</p>
          <small>{record.updated_at ? text(`最近更新：${new Date(record.updated_at).toLocaleString()}`, `Updated: ${new Date(record.updated_at).toLocaleString()}`) : text("当前使用服务器默认配置", "Using server defaults")}</small>
          <div className="button-row"><button className="button button-quiet" type="button" disabled={busy || !!changes || record.source !== "database"} onClick={() => reset.mutate()}>{text("恢复服务器默认配置", "Restore server defaults")}</button>
          {changes ? <button className="button button-quiet" type="button" disabled={busy} onClick={() => { setDraft(null); setMessage(""); save.reset(); }}>{text("放弃未保存修改", "Discard changes")}</button> : null}</div>
        </details>
      </footer>
    </article>
  );
}

function UserAndCreditManagement({ users, currentUserId }: { users: AdminUser[]; currentUserId: string }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [targetUserId, setTargetUserId] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState("");
  const [adjustmentKey, setAdjustmentKey] = useState(() => newIdempotencyKey());

  const targetUser = users.find(user => user.user_id === targetUserId);

  const refreshBilling = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers }),
      queryClient.invalidateQueries({ queryKey: queryKeys.adminUsage }),
      queryClient.invalidateQueries({ queryKey: queryKeys.balance }),
      queryClient.invalidateQueries({ queryKey: queryKeys.balanceTransactions }),
    ]);
  };
  const adjustment = useMutation({
    mutationFn: () => apiRequest<CreditTransaction>("/api/v1/admin/credits/adjustments", {
      method: "POST",
      ...jsonBody({ target_user_id: targetUserId, amount_usd: amount, reason }),
      headers: { "Content-Type": "application/json", "Idempotency-Key": adjustmentKey },
    }),
    onSuccess: async () => {
      setMessage(text("额度调整已写入不可变资金流水。", "Adjustment was written to the append-only ledger."));
      setAmount("");
      setReason("");
      setTargetUserId("");
      setAdjustmentKey(newIdempotencyKey());
      await refreshBilling();
    },
  });
  const updateUser = useMutation({
    mutationFn: ({ userId, patch }: { userId: string; patch: { role?: string; status?: string } }) => apiRequest<AdminUser>(
      `/api/v1/admin/users/${encodeURIComponent(userId)}`,
      { method: "PATCH", ...jsonBody(patch) },
    ),
    onSuccess: refreshBilling,
  });
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleUsers = users.filter((user) => !normalizedQuery
    || user.email.toLocaleLowerCase().includes(normalizedQuery)
    || user.display_name.toLocaleLowerCase().includes(normalizedQuery));

  return (
    <section className="surface admin-user-panel">
      <div className="section-heading admin-user-heading">
        <div><h2>{text("用户与余额", "Users & balances")}</h2><p>{text("在用户行中调整余额或管理权限。停用会立即撤销登录会话；余额调整保留完整流水。", "Adjust balances or access from each user row. Disabling revokes sessions; balance changes are recorded in the ledger.")}</p></div>
        <label className="admin-user-search"><span>{text("查找用户", "Find user")}</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text("邮箱或显示名称", "Email or display name")} /></label>
      </div>

      {targetUser ? <section key={targetUserId} className="admin-credit-editor" aria-label={text("调整用户余额", "Adjust user balance")}>
        <div className="admin-credit-target"><div><strong>{text("调整余额：", "Adjust balance: ")}{targetUser.display_name || targetUser.email}</strong><small>{targetUser.email} · {text("可用余额", "Available")} ${Number(targetUser.available_usd).toFixed(4)}</small></div><button className="button button-quiet" type="button" disabled={adjustment.isPending} onClick={() => setTargetUserId("")}>{text("取消调整", "Cancel adjustment")}</button></div>
        <fieldset className="admin-credit-form" disabled={adjustment.isPending}>
        <label><span>{text("调整金额（USD）", "Amount (USD)")}</span><input autoFocus type="number" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder={text("增加填正数，扣减填负数", "Positive to add, negative to deduct")} /></label>
        <label className="admin-credit-reason"><span>{text("调整原因", "Reason")}</span><input value={reason} maxLength={2000} onChange={(event) => setReason(event.target.value)} placeholder={text("例如：测试额度、人工退款或纠正记录", "For example: test credit, refund, or correction")} /></label>
        <button className="button button-primary" type="button" disabled={adjustment.isPending || !Number.isFinite(Number(amount)) || Number(amount) === 0 || !reason.trim()} onClick={() => adjustment.mutate()}>{adjustment.isPending ? text("写入中…", "Posting…") : text("确认调整余额", "Post adjustment")}</button>
        </fieldset>
      </section> : null}
      {message ? <p className="message" role="status">{message}</p> : null}
      {adjustment.error || updateUser.error ? <p className="message message-error" role="alert">{(adjustment.error || updateUser.error)?.message}</p> : null}

      <div className="admin-user-table-wrap">
        <table className="admin-user-table">
          <thead><tr><th>{text("用户", "User")}</th><th>{text("可用余额", "Available")}</th><th>{text("累计成本", "Usage cost")}</th><th>{text("项目", "Projects")}</th><th>{text("角色", "Role")}</th><th>{text("状态", "Status")}</th><th>{text("操作", "Actions")}</th></tr></thead>
          <tbody>{visibleUsers.map((user) => {
            const isSelf = user.user_id === currentUserId;
            return <tr key={user.user_id}>
              <td><strong>{user.display_name || text("未命名用户", "Unnamed user")}{isSelf ? text("（当前账户）", " (you)") : ""}</strong><small>{user.email}</small></td>
              <td><strong>${Number(user.available_usd).toFixed(4)}</strong><small>{Number(user.reserved_usd) > 0 ? text(`冻结 $${Number(user.reserved_usd).toFixed(4)}`, `$${Number(user.reserved_usd).toFixed(4)} reserved`) : text("无冻结", "No hold")}</small></td>
              <td><strong>${Number(user.estimated_cost_usd).toFixed(4)}</strong><small>USD</small></td>
              <td>{user.project_count.toLocaleString()}</td>
              <td><select aria-label={text(`${user.email} 的角色`, `Role for ${user.email}`)} value={user.role} disabled={updateUser.isPending || isSelf} onChange={(event) => updateUser.mutate({ userId: user.user_id, patch: { role: event.target.value } })}><option value="user">User</option><option value="admin">Admin</option></select></td>
              <td><select aria-label={text(`${user.email} 的状态`, `Status for ${user.email}`)} value={user.status} disabled={updateUser.isPending || isSelf} onChange={(event) => updateUser.mutate({ userId: user.user_id, patch: { status: event.target.value } })}><option value="active">{text("正常", "Active")}</option><option value="disabled">{text("停用", "Disabled")}</option></select></td>
              <td><button className="button button-quiet" type="button" aria-label={text(`调整 ${user.email} 的余额`, `Adjust balance for ${user.email}`)} disabled={adjustment.isPending} onClick={() => { setTargetUserId(user.user_id); setAmount(""); setReason(""); setMessage(""); adjustment.reset(); setAdjustmentKey(newIdempotencyKey()); }}>{text("调整余额", "Adjust balance")}</button></td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {!visibleUsers.length ? <div className="empty-state compact-empty">{text("没有匹配的用户。", "No matching users.")}</div> : null}
    </section>
  );
}

export function AdminPage() {
  const { text } = useUiText();
  const client = useQueryClient();
  const [section, setSection] = useState("accounts");
  const [visited, setVisited] = useState(() => new Set(["accounts"]));
  const [providerKind, setProviderKind] = useState<ProviderKind>("text");
  const providers = useQuery({ ...adminProviderSettingsQuery, enabled: section === "services" });
  const audit = useQuery({ ...adminProviderAuditQuery, enabled: section === "activity" });
  const users = useQuery({ ...adminUsersQuery, enabled: section === "accounts" });
  const usage = useQuery(adminUsageQuery);
  const me = useQuery(meQuery);
  const records = new Map(providers.data?.items.map((item) => [item.provider_kind, item]));
  const sections = [
    { id: "accounts", zh: "用户与余额", en: "Users & balances", description: "账户、权限与余额调整", descriptionEn: "Accounts, access and balances" },
    { id: "services", zh: "模型与服务", en: "Models & services", description: "服务连接、模型与价格", descriptionEn: "Connections, models and prices" },
    { id: "activity", zh: "运行记录", en: "Activity", description: "故障排查与配置记录", descriptionEn: "Failures and configuration history" },
  ];
  const refresh = useMutation({ mutationFn: async () => {
    await Promise.all([
      usage.refetch(),
      section === "accounts" ? users.refetch() : undefined,
      section === "services" ? providers.refetch() : undefined,
      section === "services" && providerKind === "text" ? client.invalidateQueries({ queryKey: queryKeys.modelCatalog }) : undefined,
      section === "services" && providerKind === "text" ? client.invalidateQueries({ queryKey: queryKeys.adminModelCatalog }) : undefined,
      section === "services" && providerKind === "text" ? client.invalidateQueries({ queryKey: queryKeys.textConnections }) : undefined,
      section === "activity" ? audit.refetch() : undefined,
      section === "activity" ? client.invalidateQueries({ queryKey: ["admin-system-errors"] }) : undefined,
      section === "activity" ? client.invalidateQueries({ queryKey: ["admin-storage"] }) : undefined,
    ]);
  } });

  return (
    <main className="workspace page-container admin-page">
      <div className="workspace-heading admin-heading">
        <div>
          <p className="eyebrow">{text("管理员后台", "Administration")}</p>
          <h1>{text("管理后台", "Administration")}</h1>
          <p className="muted">{text("管理账户和余额，配置生成服务，查看运行情况。", "Manage accounts and balances, configure services, and review activity.")}</p>
        </div>
        <button className="button button-quiet" type="button" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          {refresh.isPending ? text("刷新中…", "Refreshing…") : text("刷新当前页面", "Refresh current view")}
        </button>
      </div>

      {usage.error ? <ErrorState error={usage.error} onRetry={() => usage.refetch()} /> : null}
      <section className="admin-overview-grid">
        <article><span>{text("注册用户", "Registered users")}</span><strong>{usage.data?.user_count.toLocaleString() ?? "—"}</strong><small>{usage.data ? text(`${usage.data.active_user_count} 个正常账户`, `${usage.data.active_user_count} active`) : "—"}</small></article>
        <article><span>{text("有效项目", "Active projects")}</span><strong>{usage.data?.project_count.toLocaleString() ?? "—"}</strong><small>{text("未删除项目", "not deleted")}</small></article>
        <article><span>{text("累计 Tokens", "Lifetime tokens")}</span><strong>{usage.data?.total_tokens.toLocaleString() ?? "—"}</strong><small>{usage.data ? text(`${usage.data.text_request_count} 次文本请求`, `${usage.data.text_request_count} text requests`) : "—"}</small></article>
        <article><span>{text("外部服务成本", "Provider cost")}</span><strong>{usage.data ? `$${Number(usage.data.estimated_cost_usd).toFixed(4)}` : "—"}</strong><small>{text("文本 + 图像 + MinerU", "text + image + MinerU")}</small></article>
        <article><span>{text("用户余额总额", "Account balances")}</span><strong>{usage.data ? `$${Number(usage.data.account_balance_total_usd).toFixed(4)}` : "—"}</strong><small>{usage.data ? text(`冻结 $${Number(usage.data.reserved_total_usd).toFixed(4)}`, `$${Number(usage.data.reserved_total_usd).toFixed(4)} reserved`) : "—"}</small></article>
      </section>

      <nav className="admin-section-nav" aria-label={text("后台分区", "Administration sections")}>
        {sections.map(item => <button key={item.id} type="button" aria-pressed={section === item.id} aria-controls={`admin-${item.id}`} onClick={() => { setSection(item.id); setVisited(current => new Set([...current, item.id])); }}><strong>{text(item.zh, item.en)}</strong><small>{text(item.description, item.descriptionEn)}</small></button>)}
      </nav>

      <div id="admin-accounts" className="admin-content-panel" hidden={section !== "accounts"}>
        {users.isPending || me.isPending ? <p role="status">{text("正在加载用户…", "Loading users…")}</p> : null}
        {users.error || me.error ? <ErrorState error={users.error || me.error} onRetry={() => { void users.refetch(); void me.refetch(); }} /> : null}
        {users.data && me.data ? <UserAndCreditManagement users={users.data.items} currentUserId={me.data.user_id} /> : null}
      </div>

      {visited.has("services") ? <div id="admin-services" className="admin-services-layout" hidden={section !== "services"}>
        <nav className="admin-service-nav" aria-label={text("服务类型", "Service types")}>
          {providerSections.map(item => <button type="button" key={item.id} aria-pressed={providerKind === item.id} aria-controls={`admin-service-${item.id}`} onClick={() => setProviderKind(item.id)}><strong>{text(item.zh, item.en)}</strong><small>{text(item.description, item.descriptionEn)}</small></button>)}
        </nav>
        <div className="admin-service-content">
          {providers.isPending ? <p role="status">{text("正在加载服务配置…", "Loading providers…")}</p> : null}
          {providers.error ? <ErrorState error={providers.error} onRetry={() => providers.refetch()} /> : null}
          {providerSections.map(({ id }) => <div id={`admin-service-${id}`} className="admin-content-panel" key={id} hidden={providerKind !== id}>
            {id === "text" ? <TextConnectionsEditor active={section === "services" && providerKind === "text"} /> : records.get(id) ? <ProviderEditor record={records.get(id)!} /> : providers.isSuccess ? <p className="empty-state">{text("暂未获取到此服务配置，请刷新重试。", "Configuration unavailable. Refresh to retry.")}</p> : null}
            {id === "text" ? <ModelCatalogEditor active={section === "services" && providerKind === "text"} /> : null}
            {id === "embedding" ? <p className="muted">{text("此处仅配置向量服务，不改变系统的检索启用策略。", "This configures the embedding provider; it does not change the system's retrieval enablement policy.")}</p> : null}
          </div>)}
          <p className="muted admin-security-note">{text("密钥加密保存，浏览器不会读取已保存的明文密钥。", "Keys are encrypted; saved plaintext secrets are never returned to the browser.")}</p>
        </div>
      </div> : null}

      {visited.has("activity") ? <div id="admin-activity" className="admin-content-panel" hidden={section !== "activity"}>
      <StorageMaintenance active={section === "activity"} />
      <SystemErrors active={section === "activity"} />
      <section className="surface admin-audit-panel">
        {audit.isPending ? <p role="status">{text("正在加载配置记录…", "Loading configuration history…")}</p> : null}
        {audit.error ? <ErrorState error={audit.error} onRetry={() => audit.refetch()} /> : null}
        <details className="admin-audit-disclosure">
          <summary className="admin-audit-summary">
            <div>
              <span className="step-label">AUDIT</span>
              <h2>{text("服务与模型配置记录", "Service & model configuration history")}</h2>
              <small>{audit.data?.items.length ? text(`${audit.data.items.length} 条记录，展开后可滚动查看`, `${audit.data.items.length} records · scroll after expanding`) : text("还没有管理记录", "No administrative activity yet")}</small>
            </div>
            <span className="admin-audit-toggle">{text("查看记录", "View activity")}</span>
          </summary>
          <div className="admin-audit-list">
            {audit.data?.items.length ? audit.data.items.map((item) => (
              <article key={item.id}>
                <strong>{item.provider_kind.toUpperCase()} · {item.action}</strong>
                <span>{item.summary}</span>
                <small>{item.actor_email} · {new Date(item.created_at).toLocaleString()}</small>
              </article>
            )) : <div className="empty-state compact-empty">{text("服务器配置发生变更后会显示在这里。", "Provider configuration changes will appear here.")}</div>}
          </div>
        </details>
      </section>
      </div> : null}
    </main>
  );
}
