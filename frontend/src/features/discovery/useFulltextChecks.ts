import { useEffect, useState } from "react";
import { apiRequest, jsonBody } from "../../api/client";
import { externalCandidateId, type DiscoveryPayload, type DiscoveryRow } from "./model/discoveryModel";

/** Bounded probe batches: one request per batch instead of one per candidate. */
const CHECK_BATCH_SIZE = 20;

export function useFulltextChecks(projectId: string, payload?: DiscoveryPayload) {
  const [checks, setChecks] = useState<Record<string, DiscoveryRow["availability"]>>({});
  useEffect(() => {
    setChecks({});
    if (!projectId || payload?.project_id !== projectId) return;
    const controller = new AbortController();
    const ids = [...new Set((payload.results || []).flatMap(g => g.web_results || []).map(externalCandidateId).filter(Boolean))];
    setChecks(Object.fromEntries(ids.map(id => [id, { state: "checking" }])));
    const batches: string[][] = [];
    for (let index = 0; index < ids.length; index += CHECK_BATCH_SIZE) {
      batches.push(ids.slice(index, index + CHECK_BATCH_SIZE));
    }
    // Probing may reach out to the network, so it is a POST and one request
    // covers a whole batch. Two workers keep results appearing progressively.
    const worker = async () => {
      while (!controller.signal.aborted) {
        const batch = batches.shift();
        if (!batch) return;
        try {
          const result = await apiRequest<{ checks: Record<string, DiscoveryRow["availability"]> }>(
            `/api/v1/projects/${encodeURIComponent(projectId)}/discovery/fulltext-check`,
            { method: "POST", signal: controller.signal, ...jsonBody({ candidate_ids: batch }) });
          if (!controller.signal.aborted) setChecks(current => ({ ...current, ...(result.checks || {}) }));
        } catch {
          if (!controller.signal.aborted) {
            setChecks(current => ({ ...current, ...Object.fromEntries(batch.map(id => [id, { state: "unknown" as const }])) }));
          }
        }
      }
    };
    void Promise.all(Array.from({ length: Math.min(2, batches.length) }, worker));
    return () => controller.abort();
  }, [projectId, payload?.artifact_id, payload?.revision]);
  return checks;
}
