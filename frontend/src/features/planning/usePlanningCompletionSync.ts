import { useEffect, useRef } from "react";

import type { Job } from "../../api/types";

type PlanningJob = Pick<Job, "id" | "status" | "updated_at">;
const terminalStatuses = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

// Planning reads artifacts before job statuses. A response can therefore contain
// old artifacts and a newly finished job; fetch once more after that snapshot.
export function usePlanningCompletionSync(
  projectId: string,
  jobs: readonly PlanningJob[],
  refetch: () => unknown,
) {
  const observed = useRef({ projectId, completions: new Set<string>() });
  useEffect(() => {
    if (observed.current.projectId !== projectId) {
      observed.current = { projectId, completions: new Set<string>() };
    }
    if (!projectId) return;
    let needsRefresh = false;
    for (const job of jobs) {
      if (!terminalStatuses.has(job.status)) continue;
      const key = JSON.stringify([job.id, job.status, job.updated_at]);
      if (observed.current.completions.has(key)) continue;
      observed.current.completions.add(key);
      needsRefresh = true;
    }
    if (needsRefresh) void refetch();
  }, [projectId, jobs, refetch]);
}
