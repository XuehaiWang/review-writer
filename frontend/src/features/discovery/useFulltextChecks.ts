import { useEffect, useState } from "react";
import { apiRequest } from "../../api/client";
import { externalCandidateId, type DiscoveryPayload, type DiscoveryRow } from "./model/discoveryModel";

export function useFulltextChecks(projectId: string, payload?: DiscoveryPayload) {
  const [checks, setChecks] = useState<Record<string, DiscoveryRow["availability"]>>({});
  useEffect(() => {
    setChecks({});
    if (!projectId || payload?.project_id !== projectId) return;
    const controller = new AbortController();
    const ids = [...new Set((payload.results || []).flatMap(g => g.web_results || []).map(externalCandidateId).filter(Boolean))];
    let next = 0;
    setChecks(Object.fromEntries(ids.map(id => [id, { state: "checking" }])));
    const worker = async () => {
      while (!controller.signal.aborted && next < ids.length) {
        const id = ids[next++];
        let result: DiscoveryRow["availability"];
        try {
          result = await apiRequest<NonNullable<DiscoveryRow["availability"]>>(`/api/v1/projects/${encodeURIComponent(projectId)}/discovery/fulltext-check?candidate_id=${encodeURIComponent(id)}`, { signal: controller.signal });
        } catch {
          result = { state: "unknown" };
        }
        if (!controller.signal.aborted) setChecks(current => ({ ...current, [id]: result }));
      }
    };
    void Promise.all(Array.from({ length: Math.min(3, ids.length) }, worker));
    return () => controller.abort();
  }, [projectId, payload?.artifact_id, payload?.revision]);
  return checks;
}
