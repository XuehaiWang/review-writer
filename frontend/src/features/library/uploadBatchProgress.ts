import type { UploadBatchSummary } from "../../api/types";

export type LocalUploadState = "queued" | "uploading" | "done" | "failed";

export type UploadBatchCounts = {
  total: number;
  done: number;
  failed: number;
  uploading: number;
  queued: number;
};

export function buildUploadBatchCounts(
  summary: UploadBatchSummary | undefined,
  localStates: LocalUploadState[],
  expectedTotal?: number,
): UploadBatchCounts {
  const localDone = localStates.filter((status) => status === "done").length;
  const localFailed = localStates.filter((status) => status === "failed").length;
  const localUploading = localStates.filter((status) => status === "uploading").length;
  const localQueued = localStates.filter((status) => status === "queued").length;
  const serverTotal = summary?.total || 0;
  const accountedTotal = serverTotal + localStates.length;
  const total = Math.max(expectedTotal || 0, accountedTotal);
  const unreportedQueued = Math.max(0, total - accountedTotal);

  return {
    total,
    done: (summary?.succeeded || 0) + localDone,
    failed: (summary?.failed || 0) + (summary?.cancelled || 0) + (summary?.interrupted || 0) + localFailed,
    uploading: (summary?.running || 0) + (summary?.cancel_requested || 0) + localUploading,
    queued: (summary?.queued || 0) + localQueued + unreportedQueued,
  };
}
