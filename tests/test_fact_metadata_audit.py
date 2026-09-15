"""Hydration must neither invalidate audits nor admit changed scientific inputs."""
from copy import deepcopy

import pytest

from review_writer_core.scientific_facts import (
    FACT_VALIDATION_VERSION, fact_needs_verification, fact_is_usable, review_fingerprint,
)
from review_writer_core.review_fact_readiness import fact_processing_state


def audited(status="uncertain"):
    ref = dict(evidence_key="sha256:source", chunk_id="chunk-1", paper_id="P1",
               source_lineage_hash="lineage-1", support_excerpt="Yield was 80%.",
               source_file_id=None, source_type=None, page_start=2)
    fact = dict(paper_id="P1", field_id="quantitative_results", value="Yield was 80%.",
                support_excerpt="Yield was 80%.", support_spans=[deepcopy(ref)], evidence_refs=[deepcopy(ref)],
                validation_contract=FACT_VALIDATION_VERSION, support_level="context_only",
                assertion_ceiling="context_only")
    fact["verification"] = dict(contract=FACT_VALIDATION_VERSION, status=status, input_fingerprint=review_fingerprint(fact))
    return fact


def hydrate(fact):
    result = deepcopy(fact)
    for collection in ("evidence_refs", "support_spans"):
        result[collection][0].update(source_file_id="P1", source_type="article")
    return result


@pytest.mark.parametrize("status", ["uncertain", "rejected", "supported"])
def test_hydration_preserves_exact_audit_and_verdict(status):
    original = audited(status)
    result = hydrate(original)
    before = deepcopy(result)
    assert not fact_needs_verification(result)
    assert result == before  # Compatibility read never rewrites the record.
    assert not fact_is_usable(result)  # Context-only is never upgraded.
    assert fact_processing_state([result], {})["processing"]["verification"] == "completed"


@pytest.mark.parametrize("field,value", [("value", "Yield was 90%."), ("evidence_ceiling", "Broader claim"),
    ("paper_id", "P2"), ("experiment_id", "other experiment"), ("epistemic_status", "author_interpretation")])
def test_changed_scientific_input_requires_new_audit(field, value):
    fact = hydrate(audited())
    fact[field] = value
    assert fact_needs_verification(fact)


@pytest.mark.parametrize("field,value", [("chunk_id", "chunk-2"), ("evidence_key", "other-source"),
    ("source_lineage_hash", "lineage-2"), ("support_excerpt", "Yield was 90%."), ("page_start", 3)])
def test_changed_source_identity_is_not_metadata_hydration(field, value):
    fact = hydrate(audited())
    fact["support_spans"][0][field] = value
    assert fact_needs_verification(fact)


def test_changed_existing_metadata_and_pending_audits_are_not_accepted():
    fact = hydrate(audited())
    fact["verification"]["input_fingerprint"] = review_fingerprint(fact)
    fact["support_spans"][0]["source_type"] = "different-type"
    assert fact_needs_verification(fact)


def test_legacy_article_label_is_compatible_but_other_source_types_are_not():
    fact = hydrate(audited())
    for name in ("evidence_refs", "support_spans"):
        fact[name][0]["source_type"] = "main_article"
    fact["verification"]["input_fingerprint"] = review_fingerprint(fact)
    for name in ("evidence_refs", "support_spans"):
        fact[name][0]["source_type"] = "article"
    assert not fact_needs_verification(fact)
    fact["support_spans"][0]["source_type"] = "supplementary_information"
    assert fact_needs_verification(fact)
    fact = hydrate(audited("pending"))
    assert fact_needs_verification(fact)
