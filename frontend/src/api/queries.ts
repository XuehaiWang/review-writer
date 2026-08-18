import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "./client";
import type {
  AuthConfig,
  AdminProviderAuditList,
  LibraryList,
  ModelCatalog,
  Principal,
  ProjectList,
  ProviderSettingsList,
  UsageSummary,
  UsageTimeline,
} from "./types";

export const queryKeys = {
  authConfig: ["auth-config"] as const,
  me: ["me"] as const,
  projects: ["projects"] as const,
  providerSettings: ["provider-settings"] as const,
  adminProviderSettings: ["admin", "provider-settings"] as const,
  adminProviderAudit: ["admin", "provider-audit"] as const,
  modelCatalog: ["model-catalog"] as const,
  usageSummary: ["usage-summary"] as const,
  usageTimeline: (days: number) => ["usage-timeline", days] as const,
  libraryUploadJobs: ["library", "upload-jobs"] as const,
  library: (query: string) => ["library", query] as const,
  libraryMetadata: (paperId: string) => ["library", paperId, "metadata"] as const,
  libraryMarkdown: (paperId: string) => ["library", paperId, "markdown"] as const,
  job: (jobId: string) => ["job", jobId] as const,
};

export const authConfigQuery = queryOptions({
  queryKey: queryKeys.authConfig,
  queryFn: () => apiRequest<AuthConfig>("/api/v1/auth/config"),
  staleTime: 5 * 60 * 1000,
});

export const meQuery = queryOptions({
  queryKey: queryKeys.me,
  queryFn: () => apiRequest<Principal>("/api/v1/me"),
  retry: false,
});

export const projectsQuery = queryOptions({
  queryKey: queryKeys.projects,
  queryFn: () => apiRequest<ProjectList>("/api/v1/projects"),
});

export const providerSettingsQuery = queryOptions({
  queryKey: queryKeys.providerSettings,
  queryFn: () => apiRequest<ProviderSettingsList>("/api/v1/provider-settings"),
});

export const adminProviderSettingsQuery = queryOptions({
  queryKey: queryKeys.adminProviderSettings,
  queryFn: () => apiRequest<ProviderSettingsList>("/api/v1/admin/provider-settings"),
});

export const adminProviderAuditQuery = queryOptions({
  queryKey: queryKeys.adminProviderAudit,
  queryFn: () => apiRequest<AdminProviderAuditList>("/api/v1/admin/provider-audit?limit=50"),
});

export const modelCatalogQuery = queryOptions({
  queryKey: queryKeys.modelCatalog,
  queryFn: () => apiRequest<ModelCatalog>("/api/v1/model-catalog"),
  staleTime: 30 * 60 * 1000,
});

export const usageSummaryQuery = queryOptions({
  queryKey: queryKeys.usageSummary,
  queryFn: () => apiRequest<UsageSummary>("/api/v1/usage/summary"),
});

export function usageTimelineQuery(days = 30) {
  return queryOptions({
    queryKey: queryKeys.usageTimeline(days),
    queryFn: () => apiRequest<UsageTimeline>(`/api/v1/usage/timeline?days=${days}`),
  });
}

export function libraryQuery(query: string) {
  return queryOptions({
    queryKey: queryKeys.library(query),
    queryFn: () => apiRequest<LibraryList>(`/api/v1/library/papers?q=${encodeURIComponent(query)}`),
  });
}
