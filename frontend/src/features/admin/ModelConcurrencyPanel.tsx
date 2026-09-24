import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiRequest, jsonBody } from "../../api/client";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

type Kind = "text" | "image" | "embedding";
type Limits = Record<Kind, { global: number; user: number }>;
type Settings = { limits: Limits; version: number; updated_at: string | null };
const kinds: { key: Kind; zh: string; en: string; total: number; user: number }[] = [
  { key: "text", zh: "文本模型", en: "Text", total: 32, user: 8 },
  { key: "image", zh: "图像模型", en: "Images", total: 8, user: 4 },
  { key: "embedding", zh: "向量模型", en: "Embeddings", total: 16, user: 8 },
];
const endpoint = "/api/v1/admin/model-concurrency";

export function ModelConcurrencyPanel({ active }: { active: boolean }) {
  const { text } = useUiText();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Limits | null>(null);
  const [saved, setSaved] = useState(false);
  const query = useQuery({ queryKey: ["admin", "model-concurrency"],
    queryFn: () => apiRequest<Settings>(endpoint), enabled: active });
  const limits = draft ?? query.data?.limits;
  const valid = limits && kinds.every(({ key, total, user }) => {
    const values = limits[key];
    return Number.isInteger(values?.global) && Number.isInteger(values?.user)
      && values.global >= 1 && values.global <= total
      && values.user >= 1 && values.user <= user && values.user <= values.global;
  });
  const save = useMutation({
    mutationFn: () => apiRequest<Settings>(endpoint, { method: "PUT", ...jsonBody({ limits }) }),
    onSuccess: async (value) => {
      client.setQueryData(["admin", "model-concurrency"], value);
      setDraft(null);
      setSaved(true);
    },
  });

  return <section className="surface admin-concurrency-panel">
    <div className="section-heading">
      <div><span className="step-label">{text("运行资源", "Runtime resources")}</span>
        <h2>{text("模型并发额度", "Model concurrency")}</h2>
        <p className="muted">{text("单用户额度在其所有项目间共享。降低额度不会中断正在执行的调用。", "A user's quota is shared across projects. Lowering a limit does not interrupt calls already running.")}</p>
      </div>
    </div>
    {query.error ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : null}
    {query.isPending ? <p role="status">{text("正在读取并发配置…", "Loading concurrency limits…")}</p> : null}
    {limits ? <form onSubmit={event => { event.preventDefault(); if (valid) save.mutate(); }}>
      <div className="admin-concurrency-grid">
        {kinds.map(({ key, zh, en, total, user }) => <div className="admin-concurrency-row" key={key}>
          <strong>{text(zh, en)}</strong>
          <label>{text("公共并发", "Total concurrent calls")}
            <input type="number" min={1} max={total} value={limits[key].global}
              onChange={event => { setSaved(false); setDraft({ ...limits, [key]: { ...limits[key], global: Number(event.target.value) } }); }} />
          </label>
          <label>{text("每用户并发", "Calls per user")}
            <input type="number" min={1} max={user} value={limits[key].user}
              onChange={event => { setSaved(false); setDraft({ ...limits, [key]: { ...limits[key], user: Number(event.target.value) } }); }} />
          </label>
        </div>)}
      </div>
      <div className="admin-concurrency-actions">
        <button className="button" type="submit" disabled={!draft || !valid || save.isPending}>{save.isPending ? text("保存中…", "Saving…") : text("保存并发设置", "Save concurrency limits")}</button>
        {draft ? <button className="button button-quiet" type="button" onClick={() => setDraft(null)}>{text("取消修改", "Discard changes")}</button> : null}
        {saved ? <span role="status">{text("已保存；新请求会使用更新后的额度。", "Saved. New calls will use the updated limits.")}</span> : null}
      </div>
      {!valid ? <p className="form-error">{text("每个数值须在允许范围内，且每用户并发不能超过公共并发。", "Each value must be in range and per-user concurrency cannot exceed the total.")}</p> : null}
      {save.error ? <ErrorState error={save.error} /> : null}
    </form> : null}
  </section>;
}
