import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";

import { apiRequest, jsonBody } from "../../api/client";
import { providerSettingsQuery, queryKeys } from "../../api/queries";
import type { ProviderKind, ProviderSettings } from "../../api/types";
import { ErrorState } from "../../components/ErrorState";
import { useUiText } from "../../i18n/useUiText";

type ProviderFields = {
  base_url: string;
  model_name: string;
  wire_api: string;
  api_key: string;
};

const providerDefinitions: Array<{
  kind: ProviderKind;
  icon: string;
  titleZh: string;
  titleEn: string;
  descriptionZh: string;
  descriptionEn: string;
  wireOptions: Array<{ value: string; label: string }>;
}> = [
  {
    kind: "text",
    icon: "T",
    titleZh: "文本模型",
    titleEn: "Text model",
    descriptionZh: "检索规划、矩阵、章节、反馈循环与终稿",
    descriptionEn: "Discovery planning, matrix, sections, feedback loop, and final draft",
    wireOptions: [
      { value: "chat-completions", label: "Chat Completions" },
      { value: "responses", label: "Responses" },
    ],
  },
  {
    kind: "image",
    icon: "I",
    titleZh: "图像模型",
    titleEn: "Image model",
    descriptionZh: "化学图像重绘与Review Overview Figure",
    descriptionEn: "Chemistry figure redraw and review overview figure",
    wireOptions: [
      { value: "images", label: "Images" },
      { value: "chat-completions", label: "Chat Completions" },
    ],
  },
  {
    kind: "mineru",
    icon: "M",
    titleZh: "MinerU",
    titleEn: "MinerU",
    descriptionZh: "PDF解析、Markdown、版面和图像提取",
    descriptionEn: "PDF parsing, Markdown, layout, and figure extraction",
    wireOptions: [{ value: "", label: "MinerU API" }],
  },
];

function ProviderCard({ definition, record }: {
  definition: (typeof providerDefinitions)[number];
  record?: ProviderSettings;
}) {
  const { text } = useUiText();
  const title = text(definition.titleZh, definition.titleEn);
  const queryClient = useQueryClient();
  const storedKeyMask = record?.api_key_configured ? (record.api_key_hint || "••••••••") : "";
  const { register, handleSubmit, reset, formState } = useForm<ProviderFields>({
    defaultValues: {
      base_url: record?.base_url || "",
      model_name: record?.model_name || "",
      wire_api: record?.wire_api || definition.wireOptions[0]?.value || "",
      api_key: storedKeyMask,
    },
  });
  useEffect(() => {
    reset((current) => ({
      base_url: record?.base_url || "",
      model_name: record?.model_name || "",
      wire_api: record?.wire_api || definition.wireOptions[0]?.value || "",
      // Keep an entered key in this mounted SPA session. The server never returns
      // the stored secret, and the key is never written to local/session storage.
      api_key: current.api_key || storedKeyMask,
    }));
  }, [definition.wireOptions, record, reset]);
  const save = useMutation({
    mutationFn: (values: ProviderFields) =>
      apiRequest<ProviderSettings>(`/api/v1/provider-settings/${definition.kind}`, {
        method: "PUT",
        ...jsonBody({
          base_url: values.base_url.trim(),
          model_name: values.model_name.trim(),
          wire_api: values.wire_api,
          api_key: values.api_key.trim() && values.api_key.trim() !== storedKeyMask ? values.api_key.trim() : null,
          enabled: true,
        }),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.providerSettings });
    },
  });

  const mineru = definition.kind === "mineru";
  return (
    <form className="surface provider-card" onSubmit={handleSubmit((values) => save.mutate(values))}>
      <div className="provider-title">
        <span className="provider-icon">{definition.icon}</span>
        <div><h3>{title}</h3><p>{text(definition.descriptionZh, definition.descriptionEn)}</p></div>
      </div>
      {!mineru ? <label>Base URL<input type="url" placeholder="https://provider.example/v1" {...register("base_url")} /></label> : null}
      {!mineru ? <label>{text("模型", "Model")}<input placeholder="model-name" {...register("model_name")} /></label> : null}
      {!mineru ? (
        <label>
          {text("接口类型", "API protocol")}
          <select {...register("wire_api")}>
            {definition.wireOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </label>
      ) : null}
      <label>
        {mineru ? "API Token" : "API Key"}
        <input type="password" autoComplete="off" placeholder={record?.api_key_configured ? text("输入新密钥可替换现有配置", "Enter a new key to replace the saved key") : text("请输入密钥", "Enter a key")} {...register("api_key")} />
      </label>
      <div className={record?.api_key_configured ? "key-status configured" : "key-status"}>
        {record?.api_key_configured ? `${text("已配置", "Configured")} ${record.api_key_hint || text("加密密钥", "encrypted key")}` : text("尚未配置", "Not configured")}
      </div>
      <button className="button button-secondary button-block" type="submit" disabled={save.isPending || formState.isSubmitting}>
        {save.isPending ? text("正在加密保存…", "Encrypting and saving…") : text(`保存${definition.titleZh}设置`, `Save ${definition.titleEn} settings`)}
      </button>
      {save.error ? <p className="message message-error" role="alert">{save.error.message}</p> : null}
      {save.isSuccess ? <p className="message" role="status">{text("设置已保存。", "Settings saved.")}</p> : null}
    </form>
  );
}

export function SettingsPage() {
  const { text } = useUiText();
  const settings = useQuery(providerSettingsQuery);
  const records = new Map(settings.data?.items.map((item) => [item.provider_kind, item]));
  return (
    <main className="workspace page-container">
      <div className="workspace-heading">
        <div>
          <p className="eyebrow">{text("按用户加密配置", "Encrypted per-user configuration")}</p>
          <h1>{text("个人API设置", "Personal API settings")}</h1>
          <p className="muted">{text("密钥按当前账户加密保存；页面只显示配置状态，不读取服务端明文。", "Keys are encrypted per account. The page shows configuration status without reading server-side plaintext.")}</p>
        </div>
        <button className="button button-quiet" type="button" disabled={settings.isFetching} onClick={() => settings.refetch()}>{text("刷新", "Refresh")}</button>
      </div>
      {settings.error ? <ErrorState error={settings.error} onRetry={() => settings.refetch()} /> : null}
      {settings.isPending ? <div className="empty-state">{text("正在读取设置…", "Loading settings…")}</div> : null}
      <section className="provider-grid">
        {providerDefinitions.map((definition) => (
          <ProviderCard key={definition.kind} definition={definition} record={records.get(definition.kind)} />
        ))}
      </section>
    </main>
  );
}
