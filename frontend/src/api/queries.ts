import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "./client";
import { ACTIVE_JOB_POLL_INTERVAL_MS } from "./polling";
import type {
  AuthConfig,
  AdminUsageSummary,
  AdminUserList,
  AdminProviderAuditList,
  Balance,
  CreditTransactionList,
  LibraryList,
  ModelCatalog,
  Principal,
  ProjectList,
  ProviderSettingsList,
  UsageSummary,
  UsageTimeline,
  TaxonomyProfileCatalog,
} from "./types";

export const queryKeys = {
  authConfig: ["auth-config"] as const,
  me: ["me"] as const,
  projects: ["projects"] as const,
  taxonomyProfiles: ["taxonomy-profiles"] as const,
  providerSettings: ["provider-settings"] as const,
  adminProviderSettings: ["admin", "provider-settings"] as const,
  adminProviderAudit: ["admin", "provider-audit"] as const,
  modelCatalog: ["model-catalog"] as const,
  usageSummary: (projectId = "") => ["usage-summary", projectId] as const,
  usageTimeline: (days: number, projectId = "") => ["usage-timeline", projectId, days] as const,
  balance: ["balance"] as const,
  balanceTransactions: ["balance", "transactions"] as const,
  adminUsers: ["admin", "users"] as const,
  adminUsage: ["admin", "usage"] as const,
  libraryUploadJobs: ["library", "upload-jobs"] as const,
  library: (query: string) => ["library", query] as const,
  libraryMetadata: (paperId: string) => ["library", paperId, "metadata"] as const,
  libraryBibliographyAudit: (paperId: string) => ["library", paperId, "bibliography-audit"] as const,
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

export const taxonomyProfilesQuery = queryOptions({
  queryKey: queryKeys.taxonomyProfiles,
  queryFn: () => apiRequest<TaxonomyProfileCatalog>("/api/v1/taxonomy-profiles"),
  staleTime: 30 * 60 * 1000,
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

export function usageSummaryQuery(projectId = "") {
  const suffix = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return queryOptions({
    queryKey: queryKeys.usageSummary(projectId),
    queryFn: () => apiRequest<UsageSummary>(`/api/v1/usage/summary${suffix}`),
  });
}

export function usageTimelineQuery(days = 30, projectId = "") {
  const project = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
  return queryOptions({
    queryKey: queryKeys.usageTimeline(days, projectId),
    queryFn: () => apiRequest<UsageTimeline>(`/api/v1/usage/timeline?days=${days}${project}`),
  });
}

export const balanceQuery = queryOptions({
  queryKey: queryKeys.balance,
  queryFn: () => apiRequest<Balance>("/api/v1/balance"),
});

export const balanceTransactionsQuery = queryOptions({
  queryKey: queryKeys.balanceTransactions,
  queryFn: () => apiRequest<CreditTransactionList>("/api/v1/balance/transactions?limit=100"),
});

export const adminUsersQuery = queryOptions({
  queryKey: queryKeys.adminUsers,
  queryFn: () => apiRequest<AdminUserList>("/api/v1/admin/users?limit=200"),
});

export const adminUsageQuery = queryOptions({
  queryKey: queryKeys.adminUsage,
  queryFn: () => apiRequest<AdminUsageSummary>("/api/v1/admin/usage"),
});

export function libraryQuery(query: string) {
  return queryOptions({
    queryKey: queryKeys.library(query),
    queryFn: () => apiRequest<LibraryList>(`/api/v1/library/papers?q=${encodeURIComponent(query)}&mode=hybrid`),
    refetchInterval: (state) => {
      const data = state.state.data;
      const fulltextActive = data?.items.some((paper) =>
        ["queued", "building"].includes(paper.index_status?.fulltext || "")
      );
      const semanticBackfillActive = ["queued", "running", "cancel_requested"].includes(
        data?.semantic_backfill?.status || "",
      );
      if (fulltextActive || semanticBackfillActive) {
        return ACTIVE_JOB_POLL_INTERVAL_MS;
      }
      return ["waiting_retry", "blocked_credit"].includes(
        data?.semantic_backfill?.status || "",
      )
        ? 5 * 60 * 1000
        : false;
    },
  });
}
