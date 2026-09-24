import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { adminModelCatalogQuery, textConnectionsQuery, queryKeys } from "../../api/queries";
import type { ModelCatalog, ModelTier, ModelChannel, AdminProviderTestResult } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

import { useLocalizedMessage } from "../../i18n/useLocalizedMessage";
const channelsFor = (model: ModelTier): ModelChannel[] => model.channels?.length ? model.channels : [{
  connection_id: model.connection_id || "default", model: model.model, wire_api: model.wire_api || "",
}];
export function ModelCatalogEditor({ active = true }: { active?: boolean }) {
  const { text } = useUiText();
  const client = useQueryClient();
  const query = useQuery({ ...adminModelCatalogQuery, enabled: active });
  const connections = useQuery({ ...textConnectionsQuery, enabled: active });
  const [draft, setDraft] = useState<ModelCatalog | null>(null);
  const [message, setMessage] = useLocalizedMessage();
  const catalog = draft || query.data;
  const save = useMutation({
    mutationFn: () => apiRequest<ModelCatalog>("/api/v1/admin/model-catalog", { method: "PUT", ...jsonBody(draft) }),
    onSuccess: async data => {
      client.setQueryData(adminModelCatalogQuery.queryKey, data);
      setDraft(null);
      setMessage(["模型目录已保存。已提交的任务保持原模型和价格。", "Catalog saved. Submitted jobs retain their model and prices."]);
      await client.invalidateQueries({ queryKey: queryKeys.adminProviderAudit });
      await client.invalidateQueries({ queryKey: queryKeys.modelCatalog });
    },
  });
  const test = useMutation({
    mutationFn: (id: string) => apiRequest<AdminProviderTestResult>(`/api/v1/admin/provider-settings/text/test?model_id=${encodeURIComponent(id)}`, { method: "POST" }),
    onSuccess: data => setMessage(data.ok ? ["已配置渠道的模型调用及 JSON 输出测试全部通过。", "Model call and JSON output tests passed for all configured channels."] : data.message),
  });
  const update = (index: number, change: Partial<ModelTier>) => {
    if (catalog) setDraft({ ...catalog, items: catalog.items.map((item, i) => i === index ? { ...item, ...change } : item) });
    setMessage("");
  };
  const busy = save.isPending || test.isPending;
  const updateChannels = (index: number, channels: ModelChannel[]) => update(index, {
    channels, model: channels[0].model, connection_id: channels[0].connection_id, wire_api: channels[0].wire_api,
  });
  const addModel = () => catalog && setDraft({ ...catalog, items: [...catalog.items, { id: "", model: "", label_zh: "", label_en: "", description_zh: "", description_en: "", enabled: true, wire_api: "", input_usd_per_million: "0", cached_input_usd_per_million: "0", output_usd_per_million: "0" }] });
  const prices = [
    ["input_usd_per_million", text("输入价格", "Input price")],
    ["cached_input_usd_per_million", text("缓存输入价格", "Cached input price")],
    ["output_usd_per_million", text("输出价格", "Output price")],
  ] as const;
  return <section className="surface admin-audit-panel admin-model-catalog">
    <header className="model-catalog-heading">
      <div><h2>{text("文本模型目录", "Text model catalog")}</h2><p>{text("一个模型可添加多个渠道，系统按空闲名额分担请求。用户只选择模型，价格保持统一。", "A model can use multiple channels. Requests go to available capacity; users choose one model at a consistent price.")}</p></div>
      <button type="button" className="button button-secondary" disabled={busy || !catalog} onClick={addModel}>{text("添加模型", "Add model")}</button>
    </header>
    {catalog ? <div className="model-catalog-meta"><span>{text(`${catalog.items.length} 个模型 · ${catalog.items.filter(item => item.enabled !== false).length} 个启用`, `${catalog.items.length} models · ${catalog.items.filter(item => item.enabled !== false).length} enabled`)}</span><span>{text("价格单位：USD / 百万 Token", "Prices: USD / million tokens")}</span></div> : null}
    {query.error ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}
    {connections.error ? <ErrorState error={connections.error} onRetry={() => connections.refetch()} /> : null}
    {query.isPending ? <p role="status">{text("正在加载模型目录…", "Loading model catalog…")}</p> : null}
    <fieldset disabled={busy} className="model-catalog-fieldset">
      <div className="model-catalog-list">
      {catalog?.items.map((model, index) => <details key={index} className="model-catalog-card" open={!query.data?.items.some(item => item.id === model.id)}>
        <summary className="model-card-summary">
          <span className="model-card-identity"><strong>{text(model.label_zh, model.label_en) || model.model || text("新模型", "New model")}</strong><small>{model.model || text("尚未填写平台模型名称", "Provider model ID not set")}</small></span>
          <span className="model-card-badges">{catalog.default_tier === model.id ? <span className="model-badge default">{text("默认", "Default")}</span> : null}<span className={`model-badge ${model.enabled === false ? "disabled" : "enabled"}`}>{model.enabled === false ? text("已停用", "Disabled") : text("已启用", "Enabled")}</span></span>
          <span className="model-card-chevron" aria-hidden="true">⌄</span>
        </summary>
        <div className="model-card-body">
        <section className="model-field-section">
          <h3>{text("基本信息", "Model details")}</h3>
          <div className="admin-provider-form model-details-grid">
          <label><span>{text("内部 ID（保存后不可改）", "Stable ID (immutable after saving)")}</span><input maxLength={32} value={model.id} disabled={!!query.data?.items.some(item => item.id === model.id)} onChange={e => update(index, { id: e.target.value })} /></label>
          <label><span>{text("中文显示名称", "Chinese display name")}</span><input value={model.label_zh} onChange={e => update(index, { label_zh: e.target.value })} /></label>
          <label><span>{text("英文显示名称", "English display name")}</span><input value={model.label_en} onChange={e => update(index, { label_en: e.target.value })} /></label>
          </div>
        </section>
        <section className="model-field-section">
          <div className="model-field-heading"><h3>{text("请求分担渠道", "Request channels")}</h3><small>{text("并发上限在服务连接中设置，同一连接下所有模型共用。", "Set shared concurrency limits in service connections.")}</small></div>
          {channelsFor(model).map((channel, ci, channels) => <div className="model-channel-row" key={ci}>
            <div className="admin-provider-form model-details-grid">
              <label><span>{text("服务连接", "Service connection")}</span><select value={channel.connection_id} disabled={!connections.data} onChange={e => updateChannels(index, channels.map((c, i) => i === ci ? { ...c, connection_id: e.target.value } : c))}>
                {!connections.data?.items.some(item => item.id === channel.connection_id) ? <option value={channel.connection_id}>{text("当前连接（待加载）", "Current connection (loading)")}</option> : null}
                {connections.data?.items.map(item => <option key={item.id} value={item.id} disabled={!item.enabled || !item.api_key_configured || channels.some((c, i) => i !== ci && c.connection_id === item.id)}>{item.name}{!item.enabled || !item.api_key_configured ? text("（不可用）", " (unavailable)") : ""}</option>)}
              </select></label>
              <label><span>{text("平台模型名称（精确匹配）", "Exact provider model ID")}</span><input maxLength={255} value={channel.model} onChange={e => updateChannels(index, channels.map((c, i) => i === ci ? { ...c, model: e.target.value } : c))} /></label>
              <label><span>{text("协议", "Protocol")}</span><select value={channel.wire_api || ""} onChange={e => updateChannels(index, channels.map((c, i) => i === ci ? { ...c, wire_api: e.target.value } : c))}><option value="">{text("沿用连接设置", "Inherit connection setting")}</option><option value="chat-completions">Chat Completions</option><option value="responses">Responses</option></select></label>
            </div>
            {channels.length > 1 ? <button type="button" className="button button-quiet" onClick={() => updateChannels(index, channels.filter((_, i) => i !== ci))}>{text("移除此渠道", "Remove channel")}</button> : null}
          </div>)}
          <button type="button" className="button button-secondary" disabled={!connections.data?.items.some(c => c.enabled && c.api_key_configured && !channelsFor(model).some(route => route.connection_id === c.id))} onClick={() => {
            const next = connections.data?.items.find(c => c.enabled && c.api_key_configured && !channelsFor(model).some(route => route.connection_id === c.id));
            if (next) updateChannels(index, [...channelsFor(model), { connection_id: next.id, model: model.model, wire_api: "" }]);
          }}>{text("添加分担渠道", "Add channel")}</button>
        </section>
        <section className="model-field-section model-pricing-section">
          <div className="model-field-heading"><h3>{text("调用价格", "Token prices")}</h3><small>{text("USD / 百万 Token", "USD / million tokens")}</small></div>
          <div className="admin-provider-form model-price-grid">
            {prices.map(([key, label]) => <label key={key}><span>{label}</span><input type="number" min="0" step="any" value={model[key]} onChange={e => update(index, { [key]: e.target.value })} /></label>)}
          </div>
        </section>
        <div className="admin-provider-form model-availability">
          <label className="admin-choice-field"><input type="checkbox" checked={model.enabled !== false} onChange={e => update(index, { enabled: e.target.checked })} />{text("启用", "Enabled")}</label>
          <label className="admin-choice-field"><input type="radio" name="default-text-model" checked={catalog.default_tier === model.id} disabled={!model.id || model.enabled === false} onChange={() => setDraft({ ...catalog, default_tier: model.id })} />{text("默认模型", "Default model")}</label>
        </div>
        <div className="model-test-row"><small>{draft ? text("请先保存修改，再测试模型。", "Save changes before testing.") : text("测试会逐一调用已保存的渠道，可能产生费用。", "Tests call every saved channel and may incur provider charges.")}</small><button type="button" className="button button-quiet" aria-label={text("测试已保存模型与 JSON 输出", "Test saved model and JSON output")} disabled={!!draft || busy} onClick={() => test.mutate(model.id)}>{test.isPending && test.variables === model.id ? text("测试中…", "Testing…") : text("测试模型", "Test model")}</button></div>
        </div>
      </details>)}
      </div>
      {catalog?.items.length === 0 ? <p className="empty-state">{text("暂无模型，点击“添加模型”开始配置。", "No models yet. Add a model to get started.")}</p> : null}
      <footer className="model-catalog-footer"><small>{draft ? text("有未保存修改，保存后对新任务生效。", "Unsaved changes apply to new jobs after saving.") : text("已提交的任务保持原模型和价格。", "Submitted jobs retain their model and prices.")}</small><div className="model-catalog-actions">
        {draft ? <button type="button" className="button button-quiet" onClick={() => { setDraft(null); save.reset(); setMessage(""); void query.refetch(); }}>{text("放弃修改", "Discard changes")}</button> : null}
        <button type="button" className="button button-primary" disabled={!draft} onClick={() => save.mutate()}>{save.isPending ? text("保存中…", "Saving…") : text("保存模型目录", "Save catalog")}</button>
      </div></footer>
    </fieldset>
    {message ? <p role="status">{message}</p> : null}
    {save.error || test.error ? <ErrorState error={save.error || test.error} /> : null}
  </section>;
}
