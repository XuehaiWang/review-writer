from copy import deepcopy
import json
from types import SimpleNamespace

from review_writer_api.domain_services.base import OwnedProjectService
from review_writer_core.scientific_facts import is_additive_fact_repair
from review_writer_core.stages.planning.matrix import refresh_matrix_fact_summary
from review_writer_core.workflow.artifacts import MATRIX


def test_display_refresh_cannot_invalidate_compatible_matrix_snapshot(tmp_path):
    previous = {"review_topic": "Topic", "rows": [{"paper_id": "P1", "scientific_facts": []}]}
    current = deepcopy(previous)
    current["rows"][0]["scientific_facts"].append({
        "field_id": "claim_targeted_fact", "value": "Control: 42% yield",
        "evidence_refs": [{"evidence_key": "b"}], "origin_paragraph_id": "S1-p1",
        "support_excerpt": "Control: 42% yield",
    })
    current["fact_repair_history"] = [{"operation": "draft_targeted_fact_promotion"}]
    assert is_additive_fact_repair(previous, current)
    display = deepcopy(current)
    refresh_matrix_fact_summary(display)
    assert not is_additive_fact_repair(previous, display)
    records = {}
    for identity, payload in (("old", previous), ("current", current)):
        path = tmp_path / (identity + ".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        records[identity] = SimpleNamespace(path=path, artifact=SimpleNamespace(id=identity, project_id="project", logical_name=MATRIX))
    service = OwnedProjectService()
    service.artifacts = SimpleNamespace(resolve_owned_artifact=lambda user, identity: records[identity])
    principal = SimpleNamespace(user_id="owner")
    assert service._matrix_dependency_matches(principal, "project", "old", records["current"].artifact)
    current["review_topic"] = "Changed scientific scope"
    records["current"].path.write_text(json.dumps(current), encoding="utf-8")
    assert not service._matrix_dependency_matches(principal, "project", "old", records["current"].artifact)
