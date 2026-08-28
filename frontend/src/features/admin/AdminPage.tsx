import { useEffect, useState } from "react";
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

type ProviderDraft = {
  base_url: string;
  model_name: string;
  wire_api: string;
  api_key: string;
  enabled: boolean;
};

const providerOrder: ProviderKind[] = ["text", "image", "embedding", "mineru"];

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
  const [draft, setDraft] = useState<ProviderDraft>(() => draftFrom(record));
  const [message, setMessage] = useState("");

  useEffect(() => {
    setDraft(draftFrom(record));
  }, [record]);

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
    onSuccess: async () => {
      setDraft((current) => ({ ...current, api_key: "" }));
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
          <strong>{record.enabled ? text("已就绪", "Ready") : text("未启用", "Disabled")}</strong>
          <small>{record.source === "database" ? text("数据库实时配置", "Live database override") : text("环境变量后备", "Environment fallback")}</small>
        </div>
      </header>

      <div className="admin-provider-form">
        <label className="admin-wide-field">
          <span>{text("API Base URL", "API base URL")}</span>
          <input
            type="url"
            value={draft.base_url}
            disabled={record.provider_kind === "mineru" || busy}
            onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
          />
        </label>
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
        <label className="admin-wide-field">
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
          <small>{record.updated_at ? text(`最近更新：${new Date(record.updated_at).toLocaleString()}`, `Updated: ${new Date(record.updated_at).toLocaleString()}`) : text("当前未设置数据库覆盖", "No database override")}</small>
          {message ? <p className="admin-provider-message" role="status">{message}</p> : null}
          {save.error || reset.error || testConnection.error ? (
            <p className="message message-error" role="alert">{(save.error || reset.error || testConnection.error)?.message}</p>
          ) : null}
        </div>
        <div className="admin-provider-actions">
          <button className="button button-quiet" type="button" disabled={busy || !record.enabled} onClick={() => testConnection.mutate()}>
            {testConnection.isPending ? text("测试中…", "Testing…") : text("测试连接", "Test connection")}
          </button>
          <button className="button button-quiet" type="button" disabled={busy || record.source !== "database"} onClick={() => reset.mutate()}>
            {text("恢复环境配置", "Restore environment")}
          </button>
          <button className="button button-primary" type="button" disabled={busy} onClick={() => save.mutate()}>
            {save.isPending ? text("保存中…", "Saving…") : text("保存并实时生效", "Save and apply")}
          </button>
        </div>
      </footer>
    </article>
  );
}

function UserAndCreditManagement({ users, currentUserId }: { users: AdminUser[]; currentUserId: string }) {
  const { text } = useUiText();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [targetUserId, setTargetUserId] = useState(users[0]?.user_id || "");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState("");
  const [adjustmentKey, setAdjustmentKey] = useState(() => newIdempotencyKey());

  useEffect(() => {
    if (!targetUserId && users[0]) setTargetUserId(users[0].user_id);
  }, [targetUserId, users]);

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
        <div><span className="step-label">ACCOUNTS</span><h2>{text("用户与额度管理", "Users and credits")}</h2><p>{text("停用账户会立即撤销其登录会话；额度调整必须填写原因，并完整保留管理员审计信息。", "Disabling an account immediately revokes its sessions. Credit changes require a reason and preserve administrator audit data.")}</p></div>
        <label className="admin-user-search"><span>{text("查找用户", "Find user")}</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={text("邮箱或显示名称", "Email or display name")} /></label>
      </div>

      <div className="admin-credit-form">
        <label><span>{text("目标用户", "Target user")}</span><select value={targetUserId} onChange={(event) => setTargetUserId(event.target.value)}>{users.map((user) => <option value={user.user_id} key={user.user_id}>{user.display_name || user.email} · {user.email}</option>)}</select></label>
        <label><span>{text("调整金额（USD）", "Amount (USD)")}</span><input type="number" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder={text("增加填正数，扣减填负数", "Positive to add, negative to deduct")} /></label>
        <label className="admin-credit-reason"><span>{text("调整原因", "Reason")}</span><input value={reason} maxLength={2000} onChange={(event) => setReason(event.target.value)} placeholder={text("例如：测试额度、人工退款或纠正记录", "For example: test credit, refund, or correction")} /></label>
        <button className="button button-primary" type="button" disabled={adjustment.isPending || !targetUserId || !amount || !reason.trim()} onClick={() => adjustment.mutate()}>{adjustment.isPending ? text("写入中…", "Posting…") : text("确认调整额度", "Post adjustment")}</button>
      </div>
      {message ? <p className="message" role="status">{message}</p> : null}
      {adjustment.error || updateUser.error ? <p className="message message-error" role="alert">{(adjustment.error || updateUser.error)?.message}</p> : null}

      <div className="admin-user-table-wrap">
        <table className="admin-user-table">
          <thead><tr><th>{text("用户", "User")}</th><th>{text("可用余额", "Available")}</th><th>{text("累计成本", "Usage cost")}</th><th>{text("项目", "Projects")}</th><th>{text("角色", "Role")}</th><th>{text("状态", "Status")}</th></tr></thead>
          <tbody>{visibleUsers.map((user) => {
            const isSelf = user.user_id === currentUserId;
            return <tr key={user.user_id}>
              <td><strong>{user.display_name || text("未命名用户", "Unnamed user")}{isSelf ? text("（当前账户）", " (you)") : ""}</strong><small>{user.email}</small></td>
              <td><strong>${Number(user.available_usd).toFixed(4)}</strong><small>{Number(user.reserved_usd) > 0 ? text(`冻结 $${Number(user.reserved_usd).toFixed(4)}`, `$${Number(user.reserved_usd).toFixed(4)} reserved`) : text("无冻结", "No hold")}</small></td>
              <td><strong>${Number(user.estimated_cost_usd).toFixed(4)}</strong><small>USD</small></td>
              <td>{user.project_count.toLocaleString()}</td>
              <td><select aria-label={text(`${user.email} 的角色`, `Role for ${user.email}`)} value={user.role} disabled={updateUser.isPending || isSelf} onChange={(event) => updateUser.mutate({ userId: user.user_id, patch: { role: event.target.value } })}><option value="user">User</option><option value="admin">Admin</option></select></td>
              <td><select aria-label={text(`${user.email} 的状态`, `Status for ${user.email}`)} value={user.status} disabled={updateUser.isPending || isSelf} onChange={(event) => updateUser.mutate({ userId: user.user_id, patch: { status: event.target.value } })}><option value="active">{text("正常", "Active")}</option><option value="disabled">{text("停用", "Disabled")}</option></select></td>
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
  const providers = useQuery(adminProviderSettingsQuery);
  const audit = useQuery(adminProviderAuditQuery);
  const users = useQuery(adminUsersQuery);
  const usage = useQuery(adminUsageQuery);
  const me = useQuery(meQuery);
  const records = new Map(providers.data?.items.map((item) => [item.provider_kind, item]));

  return (
    <main className="workspace page-container admin-page">
      <div className="workspace-heading admin-heading">
        <div>
          <p className="eyebrow">{text("管理员后台", "Administration")}</p>
          <h1>{text("用户、额度与 Provider 管理", "Users, credits, and providers")}</h1>
          <p className="muted">{text("集中查看全站用量、管理用户状态与额度，并维护服务器外部服务连接。所有资金变化都会写入可追溯流水。", "Review site-wide usage, manage user access and credits, and maintain external provider connections. Every balance change is written to an auditable ledger.")}</p>
        </div>
        <button className="button button-quiet" type="button" disabled={providers.isFetching || audit.isFetching || users.isFetching || usage.isFetching} onClick={() => { void providers.refetch(); void audit.refetch(); void users.refetch(); void usage.refetch(); }}>
          {providers.isFetching || audit.isFetching || users.isFetching || usage.isFetching ? text("刷新中…", "Refreshing…") : text("刷新后台", "Refresh")}
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

      {users.error ? <ErrorState error={users.error} onRetry={() => users.refetch()} /> : null}
      {users.data && me.data ? <UserAndCreditManagement users={users.data.items} currentUserId={me.data.user_id} /> : null}

      <div className="section-heading admin-provider-section-heading"><div><span className="step-label">PROVIDERS</span><h2>{text("服务器外部服务", "Server providers")}</h2><p>{text("密钥采用 AES-256-GCM 加密保存；浏览器不会再次读取明文密钥。", "Secrets are encrypted with AES-256-GCM and plaintext keys are never returned to the browser.")}</p></div></div>
      {providers.error ? <ErrorState error={providers.error} onRetry={() => providers.refetch()} /> : null}
      <section className="admin-provider-stack">
        {providerOrder.map((kind) => {
          const record = records.get(kind);
          return record ? <ProviderEditor key={kind} record={record} /> : null;
        })}
      </section>

      <section className="surface admin-audit-panel">
        {audit.error ? <ErrorState error={audit.error} onRetry={() => audit.refetch()} /> : null}
        <details className="admin-audit-disclosure">
          <summary className="admin-audit-summary">
            <div>
              <span className="step-label">AUDIT</span>
              <h2>{text("最近管理记录", "Recent administrative activity")}</h2>
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
    </main>
  );
}
