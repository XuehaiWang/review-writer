"""Advisory coverage check for body-section paragraphs.

Reports whether a generated body paragraph kept the evidence the section's
shopping list asks for. It never blocks publication: the draft-quality gate
remains the only authority, and these findings are surfaced as warnings so the
writer (or the user in the draft editor) can top the paragraph up.

Deliberately reads the existing ``claim_realizations`` structure only. It does
not re-read sources and does not call a model.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# The body shopping list from section_narrative_contracts.build_section_writing_task.
REQUIRED_EVIDENCE = (
    "transformation", "key_catalyst", "outcome", "scope_class", "decisive_boundary",
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _claims(paragraph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in paragraph.get("claim_realizations") or [] if isinstance(c, Mapping)]


def _records(claims: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for c in claims for r in c.get("result_context") or [] if isinstance(r, Mapping)]


def validate_paragraph_coverage(
    paragraph: Mapping[str, Any],
    *,
    primary_papers: Iterable[Any] = (),
) -> list[str]:
    """Return advisory coverage issues for one body paragraph (never blocks)."""
    issues: list[str] = []
    claims = _claims(paragraph)
    if not claims:
        return ["no_bound_claim"]
    records = _records(claims)
    cited = {
        str(ref.get("paper_id") or "")
        for c in claims
        for ref in c.get("evidence_refs") or []
        if isinstance(ref, Mapping) and ref.get("paper_id")
    }
    if any(str(p) and str(p) not in cited for p in primary_papers):
        issues.append("primary_paper_not_covered")
    if records:
        if not any(_clean(r.get("object")) for r in records):
            issues.append("transformation_not_named")
        if not any(_clean(r.get("result")) or _clean(r.get("outcome")) for r in records):
            issues.append("outcome_not_named")
    return issues
