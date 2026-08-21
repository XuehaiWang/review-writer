import { useQuery } from "@tanstack/react-query";

import { apiRequest } from "../api/client";
import { ACTIVE_JOB_POLL_INTERVAL_MS } from "../api/polling";
import { queryKeys } from "../api/queries";
import type { Job } from "../api/types";

const activeStatuses = new Set(["queued", "running", "cancel_requested"]);

export function useJob(jobId: string) {
  return useQuery<Job>({
    queryKey: queryKeys.job(jobId),
    queryFn: () => apiRequest<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`),
    enabled: Boolean(jobId),
    refetchInterval: (query) => activeStatuses.has(query.state.data?.status || "") ? ACTIVE_JOB_POLL_INTERVAL_MS : false,
    refetchIntervalInBackground: true,
  });
}

export function jobIsActive(status?: string): boolean {
  return activeStatuses.has(status || "");
}
