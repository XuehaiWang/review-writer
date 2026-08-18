export type AuthConfig = {
  enabled: boolean;
  registration_enabled: boolean;
  password_min_length: number;
};

export type Principal = {
  user_id: string;
  email: string;
  display_name: string;
  roles: string[];
  permissions: string[];
};

export type Project = {
  project_id: string;
  slug: string;
  owner_user_id: string;
  topic: string;
  taxonomy_profile: string;
  model_tier: "sol" | "terra" | "luna";
  discovery_status: string;
  current_stage: string;
  completed_stages: string[];
};

export type ProjectList = {
  items: Project[];
  count: number;
};

export type ProviderKind = "text" | "image" | "mineru";

export type ProviderSettings = {
  provider_kind: ProviderKind;
  base_url: string;
  model_name: string;
  wire_api: string;
  api_key_configured: boolean;
  api_key_hint: string;
  enabled: boolean;
  source: "database" | "environment" | "server" | string;
  updated_at: string | null;
};

export type ProviderSettingsList = {
  items: ProviderSettings[];
};

export type AdminProviderAudit = {
  id: string;
  actor_email: string;
  provider_kind: ProviderKind;
  action: string;
  summary: string;
  created_at: string;
};

export type AdminProviderAuditList = {
  items: AdminProviderAudit[];
};

export type AdminProviderTestResult = {
  provider_kind: ProviderKind;
  ok: boolean;
  status_code: number;
  latency_ms: number;
  message: string;
};

export type ModelTier = {
  id: "sol" | "terra" | "luna";
  model: string;
  label_zh: string;
  label_en: string;
  description_zh: string;
  description_en: string;
  input_usd_per_million: string;
  cached_input_usd_per_million: string;
  output_usd_per_million: string;
};

export type ModelCatalog = {
  items: ModelTier[];
  default_tier: ModelTier["id"];
};

export type UsageSummary = {
  request_count: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  image_request_count: number;
  image_count: number;
  estimated_text_cost_usd: string;
  estimated_image_cost_usd: string;
  mineru_request_count: number;
  mineru_billable_pages: number;
  mineru_cache_hit_count: number;
  estimated_mineru_cost_usd: string;
  estimated_cost_usd: string;
  billing_mode: "record_only";
};

export type UsageTimelineItem = {
  date: string;
  request_count: number;
  total_tokens: number;
  image_count: number;
  mineru_pages: number;
  estimated_cost_usd: string;
};

export type UsageTimeline = {
  days: number;
  start_date: string;
  end_date: string;
  items: UsageTimelineItem[];
};

export type LibraryPaper = {
  id: string;
  paper_id: string;
  title: string;
  authors: string[];
  keywords: string[];
  tags: Record<string, unknown> | string[];
  original_filename: string;
  content_sha256: string;
  artifact_ids: Record<string, string>;
  updated_at: string;
  year?: number | string | null;
  journal?: string;
  doi?: string;
  structured_tags?: Record<string, unknown> | string[];
  human_review_status?: string | null;
  needs_human_check?: boolean | null;
};

export type LibraryList = {
  items: LibraryPaper[];
  count: number;
  query: string;
};

export type Job = {
  id: string;
  project_id: string | null;
  scope: string;
  job_type: string;
  status: "queued" | "running" | "cancel_requested" | "succeeded" | "failed" | "cancelled" | "interrupted";
  result: Record<string, unknown>;
  progress_current: number;
  progress_total: number;
  cancellation_requested: boolean;
  error_code: string;
  error_message: string;
  retry_of_job_id: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  available_actions: string[];
};

export type UploadJob = Job & {
  filename: string;
  batch_id: string;
};

export type UploadJobList = {
  items: UploadJob[];
  count: number;
};

export type ApiErrorPayload = {
  detail?: string | { message?: string; code?: string };
  error?: string | { message?: string; code?: string };
  message?: string;
};
