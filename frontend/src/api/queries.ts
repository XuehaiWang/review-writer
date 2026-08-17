import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "./client";
import type {
  AuthConfig,
  LibraryList,
  Principal,
  ProjectList,
  ProviderSettingsList,
} from "./types";

export const queryKeys = {
  authConfig: ["auth-config"] as const,
  me: ["me"] as const,
  projects: ["projects"] as const,
  providerSettings: ["provider-settings"] as const,
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

export function libraryQuery(query: string) {
  return queryOptions({
    queryKey: queryKeys.library(query),
    queryFn: () => apiRequest<LibraryList>(`/api/v1/library/papers?q=${encodeURIComponent(query)}`),
  });
}
