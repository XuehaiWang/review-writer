type DraftJobSelection = {
  activeServerJobId?: string;
  recoveryJobId?: string;
  localJobId?: string;
  latestServerJobId?: string;
  storedJobId?: string;
};

const nonRestorableTerminalStatuses = new Set(["failed", "cancelled", "interrupted"]);

/**
 * A refresh should recover work that is still running and may show the latest
 * successful result, but it must not revive an old terminal failure as the
 * page's current operation.  Failed jobs remain available through the
 * paragraph-level history returned by the Draft API.
 */
export function restorableDraftJobId(jobId?: string, status?: string): string {
  if (!jobId || nonRestorableTerminalStatuses.has(String(status || "").toLowerCase())) return "";
  return jobId;
}

/**
 * Prefer the job the user just submitted over the server's previous completed
 * job.  The active server job still wins once the refreshed payload exposes it.
 */
export function preferredDraftJobId({
  activeServerJobId,
  recoveryJobId,
  localJobId,
  latestServerJobId,
  storedJobId,
}: DraftJobSelection): string {
  return activeServerJobId || recoveryJobId || localJobId || latestServerJobId || storedJobId || "";
}

export function serverJobToRemember({
  activeServerJobId,
  latestServerJobId,
  locallySelectedJobId,
}: {
  activeServerJobId?: string;
  latestServerJobId?: string;
  locallySelectedJobId?: string;
}): string {
  if (activeServerJobId) return activeServerJobId;
  if (locallySelectedJobId) return locallySelectedJobId;
  return latestServerJobId || "";
}
