from copy import deepcopy

from review_writer_core.stages.sections.evidence_resolution import (
    BASE_HIT_KEYS, has_evidence_resolution, merge_supplement_evidence, resolution_record,
)
from review_writer_core.stages.sections.source_supplement import checkpoint_supplement_sections


def example():
    package = {"section_id": "S03", "hits": [{"evidence_key": "E1", "content": "Own result", "lineage": "v1"}],
               "source_manifest": {"P1": "v1"}}
    output = {"generation_mode": "limited_evidence",
              "evidence_resolution": resolution_record(package, pending=False, reason="Partial coverage")}
    extra = [{"evidence_key": "E2", "content": "Additional control", "lineage": "v1"}]
    return package, output, extra


def test_legacy_paid_resolution_survives_validated_append_without_mutating_inputs():
    package, output, extra = example()
    before = deepcopy(package)
    merged = merge_supplement_evidence(package, extra)
    assert has_evidence_resolution(output, merged)
    assert package == before
    assert merged[BASE_HIT_KEYS] == ["E1"]
    assert len(merged["hits"]) == 2
    assert merge_supplement_evidence(merged, extra) == merged


def test_supplement_does_not_hide_changes_to_original_sources_or_scope():
    package, output, extra = example()
    merged = merge_supplement_evidence(package, extra)
    for field, value in (("content", "Changed result"), ("lineage", "v2")):
        damaged = deepcopy(merged)
        damaged["hits"][0][field] = value
        assert not has_evidence_resolution(output, damaged)
    damaged = deepcopy(merged)
    damaged["source_manifest"]["P1"] = "v2"
    assert not has_evidence_resolution(output, damaged)
    damaged = deepcopy(merged)
    damaged["hits"].pop(0)
    assert not has_evidence_resolution(output, damaged)
    damaged = deepcopy(merged)
    damaged[BASE_HIT_KEYS] = []
    assert not has_evidence_resolution(output, damaged)


def test_unmarked_package_change_still_fails():
    package, output, extra = example()
    package["hits"] += extra
    assert not has_evidence_resolution(output, package)


def test_completed_and_inflight_checkpoint_use_same_supplement_projection():
    package, output, extra = example()
    supplement = {"status": "ok", "evidence": extra}
    for checkpoint in (
        {"authoring_states": {"S03": {"state": {"evidence_supplement": supplement}}}},
        {"entries": {"S03": {"synthesis": {"source_review": {"evidence_supplement": supplement}}}}},
    ):
        projected = checkpoint_supplement_sections({"S03": package}, checkpoint)
        assert has_evidence_resolution(output, projected["S03"])
        assert projected["S03"] == merge_supplement_evidence(package, extra)
