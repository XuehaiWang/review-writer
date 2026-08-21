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

export type TaxonomyProfile = {
  id: string;
  label_zh: string;
  label_en: string;
  description_zh: string;
  description_en: string;
  domain_rules_enabled: boolean;
};

export type TaxonomyProfileCatalog = {
  items: TaxonomyProfile[];
  default_profile: string;
};

export type ProjectTaxonomyProfileUpdate = {
  project: Project;
  changed: boolean;
  matrix_entered: boolean;
  downstream_stale: boolean;
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
  billing_mode: "record_only" | "credit";
};

export type Balance = {
  currency: "USD" | string;
  balance_usd: string;
  reserved_usd: string;
  available_usd: string;
  lifetime_credited_usd: string;
  lifetime_debited_usd: string;
  billing_mode: "credit";
  updated_at: string;
};

export type CreditTransaction = {
  id: string;
  user_id: string;
  job_id: string | null;
  reservation_id: string | null;
  actor_user_id: string | null;
  transaction_type: "admin_adjustment" | "reservation" | "settlement" | "release" | string;
  balance_delta_usd: string;
  reserved_delta_usd: string;
  balance_after_usd: string;
  reserved_after_usd: string;
  currency: string;
  reason: string;
  details: Record<string, unknown>;
  created_at: string;
};

export type CreditTransactionList = {
  items: CreditTransaction[];
  count: number;
};

export type AdminUser = Balance & {
  user_id: string;
  email: string;
  display_name: string;
  role: "user" | "admin";
  status: "active" | "disabled";
  project_count: number;
  estimated_cost_usd: string;
  created_at: string;
  last_login_at: string | null;
};

export type AdminUserList = {
  items: AdminUser[];
  count: number;
};

export type AdminUsageSummary = {
  user_count: number;
  active_user_count: number;
  project_count: number;
  text_request_count: number;
  total_tokens: number;
  image_count: number;
  mineru_billable_pages: number;
  estimated_text_cost_usd: string;
  estimated_image_cost_usd: string;
  estimated_mineru_cost_usd: string;
  estimated_cost_usd: string;
  account_balance_total_usd: string;
  reserved_total_usd: string;
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
  structured_tags_verified?: boolean;
  human_review_status?: string | null;
  needs_human_check?: boolean | null;
  bibliography_audit?: {
    status?: "verified" | "conflict" | "pending_retry" | "not_found" | string;
    checked_at?: string;
    sources?: Record<string, { status?: string; error?: string }>;
    field_provenance?: Record<string, Array<{ source?: string; value?: unknown; confidence?: number }>>;
    conflicts?: Array<{ field?: string; status?: string; candidates?: unknown[] }>;
    manual_review_status?: string;
  };
  index_status?: {
    mineru: "ready" | "unavailable";
    fulltext: "not_indexed" | "queued" | "building" | "ready" | "failed" | "rebuild_required";
    semantic: "disabled" | "ready";
    index_id?: string | null;
    chunk_count: number;
    chunker_version?: string;
    source_lineage_hash?: string;
    error_code?: string;
    error_message?: string;
    updated_at?: string | null;
  };
  search_match?: {
    chunk_id: string;
    page_start?: number | null;
    page_end?: number | null;
    section_path?: string[];
    content: string;
    match_reason?: string;
  } | null;
};

export type LibraryList = {
  items: LibraryPaper[];
  count: number;
  query: string;
  requested_mode?: "metadata" | "fulltext" | "hybrid";
  retrieval_mode?: "metadata" | "lexical" | "lexical_only";
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
