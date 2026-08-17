const FINAL_JOB_KEY_PREFIX = "review-writer:final-job:";

export function readFinalJobId(projectId: string): string {
  if (!projectId || typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(`${FINAL_JOB_KEY_PREFIX}${projectId}`) || "";
  } catch {
    return "";
  }
}

export function writeFinalJobId(projectId: string, jobId: string): void {
  if (!projectId || !jobId || typeof window === "undefined") return;
  try {
    window.localStorage.setItem(`${FINAL_JOB_KEY_PREFIX}${projectId}`, jobId);
  } catch {
    // The server also exposes the active/latest job id, so progress recovery
    // remains available when browser storage is unavailable.
  }
}
