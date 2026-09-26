"""Stable queue names used by PostgreSQL workers and fairness limits."""

from __future__ import annotations


JOB_QUEUES = frozenset({"scientific", "image", "ingest", "document", "bibliography", "model"})
DELEGATED_TEXT_PARENT_JOB_TYPES = frozenset({"sections.generate", "matrix.enrich"})
INTERACTIVE_JOB_TYPES = frozenset({
    "draft.rewrite", "draft.optimize", "draft.synthesis", "final.conclusion",
})
IMAGE_JOB_TYPES = frozenset({"figures.redraw", "final.overview"})
DOCUMENT_JOB_TYPES = frozenset({"final.export", "final.pdf"})
INGEST_JOB_TYPES = frozenset(
    {
        "library.upload",
        "library.archive",
        "library.index",
        "library.semantic-backfill",
        "library.search",
        "library.download",
    }
)


def queue_for_job_type(job_type: str) -> str:
    """Map a public job type to a small, deployment-stable worker queue."""

    normalized = str(job_type or "").strip()
    if normalized == "model.dispatch":
        return "model"
    if normalized == "library.bibliography-audit":
        return "bibliography"
    if normalized in IMAGE_JOB_TYPES:
        return "image"
    if normalized in DOCUMENT_JOB_TYPES:
        return "document"
    if normalized in INGEST_JOB_TYPES:
        return "ingest"
    return "scientific"
