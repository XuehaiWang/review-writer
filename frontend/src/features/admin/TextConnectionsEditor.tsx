import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { queryKeys, textConnectionsQuery } from "../../api/queries";
import type { TextConnection } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

const emptyConnection: TextConnection = { id: "", revision: 0, name: "", base_url: "", wire_api: "responses", enabled: true, api_key_configured: false, api_key_hint: "" };

function ConnectionForm({ connection, onClose }: { connection: TextConnection; onClose?: () => void }) {
  const { text } = useUiText();
  const client = useQueryClient();
  const [draft, setDraft] = useState<TextConnection | null>(null);
  const [key, setKey] = useState("");
  const value = draft || connection;
  const save = useMutation({
    mutationFn: () => apiRequest<TextConnection>(`/api/v1/admin/text-connections${connection.id ? `/${encodeURIComponent(connection.id)}` : ""}`, {
      method: connection.id ? "PUT" : "POST",
      ...jsonBody({ name: value.name, base_url: value.base_url, wire_api: value.wire_api, enabled: value.enabled, revision: value.revision, api_key: key || null }),
    }),
    onSuccess: async () => {
      setKey(""); setDraft(null);
      await Promise.all([queryKeys.textConnections, queryKeys.modelCatalog, queryKeys.adminProviderSettings, queryKeys.providerSettings, queryKeys.adminProviderAudit].map(queryKey => client.invalidateQueries({ queryKey })));
      onClose?.();
    },
  });
  const change = (patch: Partial<TextConnection>) => { setDraft({ ...value, ...patch }); save.reset(); };
  return <form className="model-card-body" onSubmit={e => { e.preventDefault(); save.mutate(); }}>
    <fieldset className="model-catalog-fieldset" disabled={save.isPending}>
      <div className="admin-provider-form model-details-grid">
        <label><span>{text("连接名称 / 分组备注", "Connection name / group label")}</span><input required maxLength={100} value={value.name} onChange={e => change({ name: e.target.value })} /></label>
        <label><span>Base URL</span><input required type="url" value={value.base_url} placeholder="https://provider.example/v1" onChange={e => change({ base_url: e.target.value })} /></label>
        <label><span>{text("默认协议", "Default protocol")}</span><select value={value.wire_api} onChange={e => change({ wire_api: e.target.value })}><option value="responses">Responses</option><option value="chat-completions">Chat Completions</option></select></label>
        <label><span>API Key</span><input type="password" autoComplete="new-password" value={key} placeholder={value.api_key_configured ? text("已配置；留空保留原密钥", "Configured; leave blank to keep") : text("填写此分组的密钥", "Enter this group's key")} onChange={e => { setDraft({ ...value }); setKey(e.target.value); save.reset(); }} /></label>
      </div>
      <label className="admin-choice-field"><input type="checkbox" checked={value.enabled} onChange={e => change({ enabled: e.target.checked })} />{text("启用连接", "Enable connection")}</label>
      <p className="muted">{text("分组权限由所填密钥决定，名称仅用于后台识别。停用后不再接收新任务；已提交任务仍使用原连接版本。", "The key determines group access; the name is an admin label. Disabling blocks new jobs; submitted jobs retain their connection revision.")}</p>
      <div className="model-catalog-actions">
        {draft || onClose ? <button type="button" className="button button-quiet" onClick={() => { setDraft(null); setKey(""); save.reset(); onClose?.(); }}>{text("取消修改", "Cancel changes")}</button> : null}
        <button type="submit" className="button button-primary" disabled={!draft || !value.name.trim() || !value.base_url.trim() || (value.enabled && !key && !value.api_key_configured)}>{save.isPending ? text("保存中…", "Saving…") : text("保存连接", "Save connection")}</button>
      </div>
    </fieldset>
    {save.error ? <ErrorState error={save.error} /> : null}
    {save.isSuccess ? <p role="status">{text("连接已保存，对新任务生效。", "Connection saved for new jobs.")}</p> : null}
  </form>;
}

export function TextConnectionsEditor({ active = true }: { active?: boolean }) {
  const { text } = useUiText();
  const query = useQuery({ ...textConnectionsQuery, enabled: active });
  const [adding, setAdding] = useState(false);
  return <section className="surface admin-audit-panel admin-model-catalog">
    <header className="model-catalog-heading"><div><h2>{text("文本服务连接", "Text service connections")}</h2><p>{text("先配置各分组的地址与密钥，再在下方为模型绑定连接。用户端只选择模型。", "Configure each group's URL and key, then bind models below. Users only select a model.")}</p></div><button type="button" className="button button-secondary" disabled={adding} onClick={() => setAdding(true)}>{text("添加连接", "Add connection")}</button></header>
    {query.error ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}
    {query.isPending ? <p role="status">{text("正在加载连接…", "Loading connections…")}</p> : null}
    <div className="model-catalog-list">
      {query.data?.items.map(connection => <details className="model-catalog-card" key={connection.id}><summary className="model-card-summary"><span className="model-card-identity"><strong>{connection.name}</strong><small>{connection.base_url}</small></span><span className={`model-badge ${connection.enabled && connection.api_key_configured ? "enabled" : "disabled"}`}>{connection.enabled && connection.api_key_configured ? text("可用", "Available") : text("未启用 / 未配置密钥", "Disabled / missing key")}</span><span className="model-card-chevron" aria-hidden="true">⌄</span></summary><ConnectionForm connection={connection} /></details>)}
      {adding ? <div className="model-catalog-card"><h3 className="model-card-summary">{text("新连接", "New connection")}</h3><ConnectionForm connection={emptyConnection} onClose={() => setAdding(false)} /></div> : null}
    </div>
  </section>;
}
