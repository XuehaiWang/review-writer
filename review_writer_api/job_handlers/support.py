"""Pure support logic shared by native job-handler stages."""

from __future__ import annotations

import os
import re
from typing import Any

from review_writer_api.scientific_runner import ScientificRunFailed
from review_writer_core.review_fact_readiness import fact_processing_state


SECTION_GENERATION_MIN_TIMEOUT_SECONDS = 15 * 60
SECTION_GENERATION_BASE_TIMEOUT_SECONDS = 5 * 60
SECTION_GENERATION_TIMEOUT_PER_PENDING_SECTION_SECONDS = 6 * 60
SECTION_GENERATION_MAX_TIMEOUT_SECONDS = 90 * 60


def matrix_live_payload(
    status: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    """Build a bounded, UI-safe view of completed Matrix extraction results."""

    entries = checkpoint.get("entries")
    entries = entries if isinstance(entries, dict) else {}
    completed = status.get("completed_papers")
    completed_ids = (
        [str(item) for item in completed if str(item)]
        if isinstance(completed, list)
        else [str(item) for item in entries]
    )
    items: list[dict[str, Any]] = []
    for paper_id in completed_ids:
        entry = entries.get(paper_id)
        result = entry.get("result") if isinstance(entry, dict) else None
        if not isinstance(result, dict):
            continue
        facts = [item for item in result.get("facts") or [] if isinstance(item, dict)]
        tags = result.get("evidence_backed_tags")
        tag_count = (
            sum(len(value) for value in tags.values() if isinstance(value, list))
            if isinstance(tags, dict)
            else 0
        )
        previews = []
        for fact in facts[:3]:
            value = str(fact.get("value") or "").strip()
            previews.append(
                {
                    "fact_id": str(fact.get("fact_id") or ""),
                    "field_id": str(fact.get("field_id") or "fact"),
                    "value": value[:260] + ("…" if len(value) > 260 else ""),
                    "support_level": str(fact.get("support_level") or ""),
                }
            )
        automatic = result.get("automatic_resolution")
        items.append(
            {
                "paper_id": paper_id,
                "status": str(result.get("status") or "complete"),
                "fact_count": len(facts),
                **fact_processing_state(facts, result),
                "classification_count": tag_count,
                "automatic_resolution_status": str(
                    automatic.get("status") or ""
                    if isinstance(automatic, dict)
                    else ""
                ),
                "facts_preview": previews,
            }
        )
    return {
        "schema_version": 1,
        "phase": str(status.get("phase") or "extracting"),
        "current": max(0, int(status.get("current") or 0)),
        "total": max(0, int(status.get("total") or 0)),
        "current_paper_id": str(status.get("current_paper_id") or ""),
        "active_paper_ids": [str(item) for item in status.get("active_paper_ids") or [] if str(item)][:3],
        "target_axis_ids": [
            str(item) for item in status.get("target_axis_ids") or [] if str(item)
        ],
        "items": items,
    }


def bibliography_source_names() -> tuple[str, ...]:
    """Use public Crossref by default and keyed sources only when configured."""

    names = ["crossref"]
    if str(os.environ.get("OPENALEX_API_KEY") or "").strip():
        names.append("openalex")
    return tuple(names)


def bibliography_needs_bounded_agent(audit: Any) -> bool:
    """Use one role-reading fallback only while automatic resolution is incomplete."""

    if not isinstance(audit, dict):
        return True
    if str(audit.get("resolved_by") or "") == "human" and str(
        audit.get("manual_review_status") or ""
    ) in {"resolved", "supporting_only", "rejected"}:
        return False
    missing = audit.get("automatic_resolution_missing_fields")
    return str(audit.get("status") or "") != "verified" or bool(
        isinstance(missing, list) and missing
    )


def section_generation_timeout_seconds(
    tasks: Any, resume_checkpoint: Any = None
) -> int:
    """Scale the subprocess budget to unfinished chapters, preserving resume gains."""

    task_ids = {
        str(task.get("section_id") or "").strip()
        for task in tasks or []
        if isinstance(task, dict) and str(task.get("section_id") or "").strip()
    }
    entries = (
        resume_checkpoint.get("entries")
        if isinstance(resume_checkpoint, dict)
        else None
    )
    completed_ids = {
        str(section_id)
        for section_id, entry in (entries or {}).items()
        if str(section_id) in task_ids and isinstance(entry, dict)
    }
    pending_count = max(1, len(task_ids - completed_ids))
    calculated = (
        SECTION_GENERATION_BASE_TIMEOUT_SECONDS
        + pending_count * SECTION_GENERATION_TIMEOUT_PER_PENDING_SECTION_SECONDS
    )
    return min(
        SECTION_GENERATION_MAX_TIMEOUT_SECONDS,
        max(SECTION_GENERATION_MIN_TIMEOUT_SECONDS, calculated),
    )


def literature_search_failure_message(error: ScientificRunFailed) -> str:
    diagnostic = str((error.details or {}).get("stderr") or "")
    lowered = diagnostic.casefold()
    if "private destination is blocked" in lowered:
        return (
            "Crossref resolved through a private or transparent-proxy address that is not "
            "trusted by this deployment. Configure REVIEW_WRITER_TRUSTED_PROXY_NETWORKS "
            "and retry."
        )
    if any(marker in lowered for marker in ("[winerror 10060]", "timed out", "timeouterror")):
        return "Crossref did not respond before timeout. Check this server's outbound network or proxy, then retry."
    if any(marker in lowered for marker in ("getaddrinfo failed", "name or service not known", "temporary failure in name resolution", "nodename nor servname")):
        return "Crossref could not be resolved by DNS. Check this server's DNS and outbound network, then retry."
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return "Crossref TLS certificate verification failed. Check this server's certificate trust store or HTTPS proxy."
    if "http error 429" in lowered:
        return "Crossref rate-limited the literature search. Wait briefly and retry; providing a contact email may improve reliability."
    if "http error 403" in lowered:
        return "Crossref rejected the literature search request. Check the server network or proxy policy, then retry."
    if re.search(r"http error 5\d\d", lowered):
        return "Crossref is temporarily unavailable. Retry the literature search shortly."
    return "Crossref literature search failed before results were returned. Check outbound access to https://api.crossref.org and retry."
