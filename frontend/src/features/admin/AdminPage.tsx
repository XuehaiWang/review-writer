import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest, jsonBody } from "../../api/client";
import {
  adminProviderAuditQuery,
  adminProviderSettingsQuery,
  queryKeys,
} from "../../api/queries";
import type {
  AdminProviderTestResult,
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

const providerOrder: ProviderKind[] = ["text", "image", "mineru"];

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
              <option value="chat-completions">Chat Completions</option>
              {record.provider_kind === "image" ? <option value="images">Images API</option> : null}
            </select>
          </label>
        ) : null}
        {record.provider_kind === "image" ? (
          <label>
            <span>{text("图像模型", "Image model")}</span>
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

export function AdminPage() {
  const { text } = useUiText();
  const providers = useQuery(adminProviderSettingsQuery);
  const audit = useQuery(adminProviderAuditQuery);
  const records = new Map(providers.data?.items.map((item) => [item.provider_kind, item]));

  return (
    <main className="workspace page-container admin-page">
      <div className="workspace-heading admin-heading">
        <div>
          <p className="eyebrow">{text("管理员后台", "Administration")}</p>
          <h1>{text("服务器 Provider 管理", "Server provider management")}</h1>
          <p className="muted">{text("密钥采用 AES-256-GCM 加密保存。保存后只影响之后启动的任务，浏览器不会再次读取明文密钥。", "Secrets are encrypted with AES-256-GCM. Changes affect newly started jobs, and plaintext keys are never returned to the browser.")}</p>
        </div>
        <button className="button button-quiet" type="button" disabled={providers.isFetching || audit.isFetching} onClick={() => { void providers.refetch(); void audit.refetch(); }}>
          {providers.isFetching || audit.isFetching ? text("刷新中…", "Refreshing…") : text("刷新后台", "Refresh")}
        </button>
      </div>

      {providers.error ? <ErrorState error={providers.error} onRetry={() => providers.refetch()} /> : null}
      <section className="admin-provider-stack">
        {providerOrder.map((kind) => {
          const record = records.get(kind);
          return record ? <ProviderEditor key={kind} record={record} /> : null;
        })}
      </section>

      <section className="surface admin-audit-panel">
        <div className="section-heading compact">
          <div><span className="step-label">AUDIT</span><h2>{text("最近管理记录", "Recent administrative activity")}</h2></div>
        </div>
        {audit.error ? <ErrorState error={audit.error} onRetry={() => audit.refetch()} /> : null}
        <div className="admin-audit-list">
          {audit.data?.items.length ? audit.data.items.map((item) => (
            <article key={item.id}>
              <strong>{item.provider_kind.toUpperCase()} · {item.action}</strong>
              <span>{item.summary}</span>
              <small>{item.actor_email} · {new Date(item.created_at).toLocaleString()}</small>
            </article>
          )) : <div className="empty-state compact-empty">{text("还没有管理记录。", "No administrative activity yet.")}</div>}
        </div>
      </section>
    </main>
  );
}
