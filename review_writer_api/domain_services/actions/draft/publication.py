"""Source-version metadata shared by accepted Draft repair publications."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from review_writer_api.workflow_repository import ArtifactRecord
from review_writer_core.workflow.artifacts import (
    DRAFT_MANUSCRIPT,
    DRAFT_QUALITY_REPORT,
    DRAFT_REWRITE_OVERLAYS,
    MATRIX,
    SECTION_EVIDENCE_PACKAGE,
)


def repaired_evidence_content(source, published):
    """Bind the evidence body to the Matrix published in the same transaction."""
    value = deepcopy(source)
    if MATRIX in published:
        value["source_matrix_artifact_id"] = published[MATRIX].id
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def repaired_artifact_metadata(
    common_metadata: dict[str, Any],
    logical_name: str,
    published_so_far: dict[str, ArtifactRecord],
) -> dict[str, Any]:
    """Bind new source versions without modifying previous artifact metadata."""
    value = dict(common_metadata)
    for source, field in (
        (SECTION_EVIDENCE_PACKAGE, "source_section_evidence_artifact_id"),
        (MATRIX, "source_matrix_artifact_id"),
    ):
        if source in published_so_far:
            value[field] = published_so_far[source].id
    if DRAFT_MANUSCRIPT in published_so_far and logical_name in {
        DRAFT_QUALITY_REPORT, DRAFT_REWRITE_OVERLAYS,
    }:
        value["source_draft_artifact_id"] = published_so_far[DRAFT_MANUSCRIPT].id
    return value
