"""Language-independent finding policy shared by evaluation, routing and approval."""

from functools import lru_cache
import json
from pathlib import Path
import re

FINDING_CATEGORIES = frozenset({
    "presentation", "evidence", "core_argument", "figure", "bibliography",
    "export", "synthesis", "discovery", "metadata", "planning",
})


@lru_cache(maxsize=1)
def rule_categories() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / "skills/review-first-draft-feedback-loop/references/unified_rubric.json"
    rubric = json.loads(path.read_text(encoding="utf-8"))
    return {row["id"]: row["finding_category"]
            for row in [*rubric["dimensions"], *rubric.get("local_checks", [])]
            if row.get("finding_category")}


def finding_category(finding: dict) -> str:
    if (finding.get("unsupported_claims")
            or str(finding.get("evidence_problem_type") or "none") not in {"", "none", "not_assessed"}
            or finding.get("source_check_status") in {"unsupported", "contradicted"}):
        return "evidence"
    if finding.get("missing_core_claim_ids"):
        return "core_argument"
    rules = set(re.split(r"[/,\s]+", str(finding.get("rule") or finding.get("rule_id") or ""))) - {""}
    rules.update(finding.get("failed_dimensions") or [])
    registered = rule_categories()
    categories = {registered[rule] for rule in rules if rule in registered}
    if "source_dependent" in categories:
        categories.remove("source_dependent")
        categories.add("presentation" if finding.get("source_check_status") == "verified" else "evidence")
    # A model-authored style label cannot override a registered scientific rule.
    for category in ("evidence", "core_argument", "figure", "bibliography", "export",
                     "metadata", "discovery", "planning", "synthesis"):
        if category in categories:
            return category
    explicit = str(finding.get("finding_category") or "")
    if explicit in FINDING_CATEGORIES:
        return explicit
    if categories == {"presentation"} and rules.issubset(registered):
        return "presentation"
    return "unknown"
