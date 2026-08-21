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
    .sort((left, right) => Date.parse(right.updated_at || right.created_at) - Date.parse(left.updated_at || left.created_at))
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
