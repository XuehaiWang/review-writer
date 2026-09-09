"""Pure Matrix value and serialization helpers."""

from __future__ import annotations

import json
import re
from typing import Any
from review_writer_core.review_fact_readiness import fact_readiness_report, fact_processing_state


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def has_fact_sources(payload):
    """A ready index with zero query hits is not an absent index."""
    return bool(payload.get("fulltext_indexed_paper_count") or payload.get("fulltext_candidate_paper_count"))


def refresh_matrix_fact_summary(matrix):
    """Refresh counters from current rows after extraction or a local revision."""
    rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
    for row in rows:
        enrichment = row.setdefault("fact_enrichment", {})
        facts = row.get("scientific_facts") or []
        enrichment.update(fact_readiness_report(facts=facts,
            required_roles=enrichment.get("required_fact_roles") or [],
            extraction_status=str(enrichment.get("status") or "pending"), baseline=True,
            topic=str(matrix.get("review_topic") or "")))
        enrichment.update(fact_processing_state(facts, enrichment))
    summary = dict(matrix.get("fact_enrichment_summary") or {})
    statuses = [(row.get("fact_enrichment") or {}).get("status", "pending") for row in rows]
    readiness = [(row.get("fact_enrichment") or {}).get("review_readiness", "source_not_established") for row in rows]
    summary.update({f"{status}_count": statuses.count(status) for status in ("complete", "partial", "limited", "failed", "pending")})
    summary.update(review_ready_count=readiness.count("complete"), review_partial_count=readiness.count("partial"),
                   source_not_established_count=readiness.count("source_not_established"),
                   needs_review_count=sum((row.get("fact_enrichment") or {}).get("review_status") == "needs_review" for row in rows),
                   evidence_backed_tag_paper_count=sum(bool(row.get("evidence_backed_tags")) for row in rows))
    for status in ("classified", "insufficient_evidence", "cross_category", "out_of_scope"):
        summary[f"topic_partition_{status}_count"] = sum(
            (row.get("topic_partition_classification") or {}).get("status") == status for row in rows)
    matrix["fact_enrichment_summary"] = summary
    return summary


def _paper_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("paper_id")) for row in rows if str(row.get("paper_id") or "").strip()]


def _publication_year(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value")
    match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _matrix_publication_year(row: dict[str, Any]) -> int | None:
    return (
        _publication_year(row.get("first_publication_date"))
        or _publication_year(row.get("bibliographic_year"))
        or _publication_year(row.get("year"))
    )
