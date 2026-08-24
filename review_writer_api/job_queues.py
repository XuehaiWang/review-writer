"""Stable queue names used by PostgreSQL workers and fairness limits."""

from __future__ import annotations


IMAGE_JOB_TYPES = frozenset({"figures.redraw", "final.overview"})
DOCUMENT_JOB_TYPES = frozenset({"final.export", "final.pdf"})
INGEST_JOB_TYPES = frozenset(
    {
        "library.upload",
        "library.index",
        "library.search",
        "library.download",
    }
)


def queue_for_job_type(job_type: str) -> str:
    """Map a public job type to a small, deployment-stable worker queue."""

    normalized = str(job_type or "").strip()
    if normalized in IMAGE_JOB_TYPES:
        return "image"
    if normalized in DOCUMENT_JOB_TYPES:
        return "document"
    if normalized in INGEST_JOB_TYPES:
        return "ingest"
    return "scientific"
