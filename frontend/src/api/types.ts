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
};

export type ProviderSettingsList = {
  items: ProviderSettings[];
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

export type ApiErrorPayload = {
  detail?: string | { message?: string; code?: string };
  error?: string | { message?: string; code?: string };
  message?: string;
};
