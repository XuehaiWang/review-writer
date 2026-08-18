import type { Job, UploadJob } from "../../api/types";

export function uploadJobsNeedingLibraryRefresh(
  jobs: UploadJob[],
  previousStatuses: ReadonlyMap<string, Job["status"]>,
  locallySubmittedJobIds: ReadonlySet<string>,
  refreshedJobIds: ReadonlySet<string>,
): string[] {
  return jobs
    .filter((job) => {
      if (job.status !== "succeeded" || refreshedJobIds.has(job.id)) return false;
      const previous = previousStatuses.get(job.id);
      return locallySubmittedJobIds.has(job.id)
        || (previous !== undefined && previous !== "succeeded");
    })
    .map((job) => job.id);
}
