import type { Job } from "../../api/types";

type CurrentDraftPublication = {
  revision?: number;
  draft_artifact_id?: string;
  quality_artifact_id?: string;
};

function resultString(job: Job | undefined, key: string): string {
  const value = job?.result?.[key];
  return typeof value === "string" ? value : "";
}

function resultRevision(job: Job | undefined): number {
  const value = Number(job?.result?.revision || 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function draftPublicationIsPending(
  job: Job | undefined,
  current: CurrentDraftPublication | undefined,
): boolean {
  if (!job || job.status !== "succeeded" || !current) return false;

  const expectedRevision = resultRevision(job);
  const currentRevision = Number(current.revision || 0);
  if (expectedRevision > 0) {
    // A later edit may already have superseded this job. Never poll forever
    // waiting for an older artifact to become current again.
    return currentRevision < expectedRevision;
  }

  const expectedDraft = resultString(job, "draft_artifact_id");
  const expectedQuality = resultString(job, "quality_artifact_id");
  return Boolean(
    (expectedDraft && current.draft_artifact_id !== expectedDraft)
      || (expectedQuality && current.quality_artifact_id !== expectedQuality),
  );
}
