import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { MatrixLiveProgress, readMatrixEnrichmentLive } from "./MatrixLiveProgress";

function job(result: Record<string, unknown>): Job {
  return {
    id: "job-1",
    project_id: "project-1",
    scope: "project",
    job_type: "matrix.enrich",
    status: "running",
    result,
    progress_current: 0,
    progress_total: 2,
    cancellation_requested: false,
    error_code: "",
    error_message: "",
    retry_of_job_id: null,
    created_at: "",
    updated_at: "",
    started_at: null,
    finished_at: null,
    available_actions: [],
  };
}

describe("readMatrixEnrichmentLive", () => {
  it("reads the compact live Matrix payload", () => {
    const live = readMatrixEnrichmentLive(job({
      matrix_enrichment_live: {
        phase: "targeted_recheck",
        current: 1,
        total: 2,
        current_paper_id: "P002",
        target_axis_ids: ["stereochemical_regime"],
        items: [{
          paper_id: "P001",
          status: "complete",
          fact_count: 3,
          classification_count: 1,
          facts_preview: [{ field_id: "reaction_type", value: "Cycloaddition", support_level: "direct" }],
        }],
      },
    }));

    expect(live?.phase).toBe("targeted_recheck");
    expect(live?.current_paper_id).toBe("P002");
    expect(live?.items[0].facts_preview[0].value).toBe("Cycloaddition");
  });

  it("derives live facts from legacy section checkpoints", () => {
    const live = readMatrixEnrichmentLive(job({
      section_progress: {
        phase: "extracting",
        current: 1,
        total: 2,
        current_paper_id: "P002",
        completed_papers: ["P001"],
      },
      section_checkpoint: {
        entries: {
          P001: {
            result: {
              status: "complete",
              facts: [{ fact_id: "F1", field_id: "product", value: "An allene product" }],
              evidence_backed_tags: { product: [{ partition_id: "allene" }] },
            },
          },
        },
      },
    }));

    expect(live?.items[0].fact_count).toBe(1);
    expect(live?.items[0].classification_count).toBe(1);
  });

  it("renders the active paper and completed fact previews underneath", () => {
    render(<MatrixLiveProgress
      job={job({
        matrix_enrichment_live: {
          phase: "extracting",
          current: 1,
          total: 2,
          current_paper_id: "P002",
          items: [{
            paper_id: "P001",
            status: "complete",
            fact_count: 1,
            classification_count: 1,
            facts_preview: [{ field_id: "reaction_type", value: "Cycloaddition", support_level: "direct" }],
          }],
        },
      })}
      papers={[
        { paper_id: "P001", title: "First paper" },
        { paper_id: "P002", title: "Current paper" },
      ]}
    />);

    expect(screen.getByText(/Current paper/)).toBeInTheDocument();
    expect(screen.getByText("Cycloaddition")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
  });
});
