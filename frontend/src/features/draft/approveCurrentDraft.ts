import { ApiError, apiRequest, jsonBody } from "../../api/client";

type Snapshot = {
  revision: number;
  draft_artifact_id: string;
  quality_artifact_id: string;
  quality: { current?: boolean };
  active_feedback_job_id?: string;
};

export async function approveCurrentDraft(projectId: string, viewed: Snapshot, changedMessage: string) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}/draft`;
  // Candidates and analysis may change independently; approve only viewed prose.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const latest = await apiRequest<Snapshot>(base);
    if (!viewed.draft_artifact_id || latest.draft_artifact_id !== viewed.draft_artifact_id) throw new Error(changedMessage);
    try {
      return await apiRequest(`${base}/approve`, { method: "POST", ...jsonBody({ revision: latest.revision }) });
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409) throw error;
      if (attempt === 1) throw new Error(changedMessage);
    }
  }
}
