import type { Job } from "../../api/types";

type SectionTaskIdentity = {
  section_id?: string;
  heading?: string;
};

type SectionCheckpoint = {
  task_ids?: unknown;
  entries?: unknown;
};

function checkpointFor(job: Job): SectionCheckpoint | null {
  const value = job.result.section_checkpoint;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as SectionCheckpoint
    : null;
}

function jobTimestamp(job: Job): number {
  return Date.parse(job.updated_at || job.created_at) || 0;
}

export function findResumableSectionJob(
  jobs: Job[],
  tasks: SectionTaskIdentity[],
): Job | undefined {
  const expectedIds = tasks.map((task) => String(task.section_id || task.heading || ""));
  if (!expectedIds.length || expectedIds.some((value) => !value)) return undefined;
  const headings = new Map(
    tasks.map((task) => [
      String(task.section_id || task.heading || ""),
      String(task.heading || task.section_id || ""),
    ]),
  );

  return [...jobs]
    .sort((left, right) => jobTimestamp(right) - jobTimestamp(left))
    .find((job) => {
      const retryable = (job.available_actions || []).includes("retry")
        || ["failed", "cancelled", "interrupted"].includes(job.status);
      if (!retryable) return false;
      const checkpoint = checkpointFor(job);
      if (!checkpoint || !Array.isArray(checkpoint.task_ids)) return false;
      const checkpointIds = checkpoint.task_ids.map((value) => String(value || ""));
      if (
        checkpointIds.length !== expectedIds.length
        || checkpointIds.some((value, index) => value !== expectedIds[index])
      ) return false;
      if (!checkpoint.entries || typeof checkpoint.entries !== "object" || Array.isArray(checkpoint.entries)) {
        return false;
      }
      return Object.entries(checkpoint.entries).every(([sectionId, entry]) => {
        if (!headings.has(sectionId) || !entry || typeof entry !== "object" || Array.isArray(entry)) {
          return false;
        }
        const checkpointHeading = String((entry as { heading?: unknown }).heading || "");
        return !checkpointHeading || checkpointHeading === headings.get(sectionId);
      });
    });
}

export function findSectionJobForDisplay(
  jobs: Job[],
  tasks: SectionTaskIdentity[],
  outputsCurrent: boolean,
): Job | undefined {
  const ordered = [...jobs].sort((left, right) => jobTimestamp(right) - jobTimestamp(left));
  const active = ordered.find((job) => ["queued", "running", "cancel_requested"].includes(job.status));
  if (active) return active;
  if (outputsCurrent) return ordered[0];
  return findResumableSectionJob(ordered, tasks);
}

export function replaceSectionJobSnapshot(
  jobs: Job[],
  current?: Job,
): Job[] {
  if (!current) return jobs;
  let replaced = false;
  const merged = jobs.map((job) => {
    if (job.id !== current.id) return job;
    replaced = true;
    return current;
  });
  return replaced ? merged : [current, ...merged];
}
