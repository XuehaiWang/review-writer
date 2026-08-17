const DRAFT_JOB_KEY_PREFIX = "review-writer:draft-job:";

export function readDraftJobId(projectId: string): string {
  if (!projectId || typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(`${DRAFT_JOB_KEY_PREFIX}${projectId}`) || "";
  } catch {
    return "";
  }
}

export function writeDraftJobId(projectId: string, jobId: string): void {
  if (!projectId || !jobId || typeof window === "undefined") return;
  try {
    window.localStorage.setItem(`${DRAFT_JOB_KEY_PREFIX}${projectId}`, jobId);
  } catch {
    // Job recovery also uses the server-side active job id. Storage can be
    // unavailable in private browsing without breaking the workflow.
  }
}
