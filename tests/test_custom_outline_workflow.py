from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from review_writer_core.stages.planning.academic_planning import enhance_blueprint, plan_structure
from review_writer_core.stages.planning.outline import outline_sections, outline_markdown_from_sections
from review_writer_api.domain_services.sections import SectionsService
from review_writer_api.domain_services.drafts import DraftsService


def custom_input():
    return {
        "outline_snapshot": {"manually_edited": True, "outline_md": "## User question"},
        "matrix_snapshot": {"rows": [{"paper_id": "P1"}, {"paper_id": "P2"}]},
        "section_blueprint": {"review_topic": "Water treatment", "sections": [
            {"section_id": "S01", "title": "User question", "section_role": "body",
             "primary_papers": ["P1"], "review_problem": "Which treatment conditions were tested?"},
            {"section_id": "S02", "title": "Background", "section_role": "introduction",
             "primary_papers": []},
        ]},
    }


def test_custom_routes_preserve_user_structure_and_explicit_assignment():
    data = custom_input()
    original = deepcopy(data)
    def model(*args, **kwargs):
        return {"sections": [
            {"section_id": "S02", "title": "Wrong title", "primary_papers": ["P1", "P2"]},
            {"section_id": "S01", "primary_papers": ["P2", "outside"]},
            {"section_id": "injected", "primary_papers": ["P1"]},
        ]}
    sections, unused = plan_structure(data, model, {})
    assert [s["title"] for s in sections] == ["User question", "Background"]
    assert sections[0]["primary_papers"] == ["P1", "P2"]
    assert sections[1]["primary_papers"] == []
    assert unused == [] and data == original


def test_custom_provider_failure_keeps_a_retrieval_plan_without_claims():
    def unavailable(*args, **kwargs):
        raise RuntimeError("Provider unavailable")
    result = enhance_blueprint(custom_input(), model_call=unavailable, checkpoint={}, report=lambda *a: None)
    blueprint = result["section_blueprint"]
    assert blueprint["academic_planning"]["status"] == "completed"
    section = blueprint["sections"][0]
    assert section["scientific_claims"] == []
    assert section["retrieval_directions"] and section["planning_notes"]
    assert "P2" in section["context_papers"]
    assert "P2" not in section["primary_papers"]


def test_malformed_routes_do_not_replace_headings_or_override_explicit_exclusions():
    data = custom_input()
    for section in data["section_blueprint"]["sections"]:
        section["excluded_papers"] = [{"paper_id": "P2", "reason": "Outside the requested scope"}]
    sections, unused = plan_structure(data, lambda *a, **k: {"sections": 42}, {})
    assert [s["title"] for s in sections] == ["User question", "Background"]
    assert all("P2" not in s["context_papers"] for s in sections)
    assert [row["paper_id"] for row in unused] == ["P2"]


def test_nested_outline_round_trip_and_parent_is_not_a_writing_task():
    parsed = outline_sections("## Methods\n<!-- section_id: S-parent -->\n### Adsorption\n"
                              "<!-- section_id: S-child -->\nAssigned papers: P1.\nPurpose: Compare measured uptake.\n")
    assert parsed[0]["organizing_only"]
    assert parsed[1]["parent_headings"] == [{"section_id": "S-parent", "title": "Methods"}]
    rendered = outline_markdown_from_sections(parsed, outline_style="custom")
    assert "###" in rendered
    assert [s["section_id"] for s in outline_sections(rendered)] == ["S-parent", "S-child"]
    data = custom_input()
    data["section_blueprint"]["sections"] = [
        {**s, "primary_papers": s["paper_ids"]} for s in parsed
    ]
    data["section_blueprint"]["paper_assignment_policy"] = {"mode": "argument_based"}
    tasks = SectionsService.tasks_from_blueprint(data["section_blueprint"])
    assert [t["section_id"] for t in tasks] == ["S-child"]
    assert tasks[0]["parent_headings"][0]["title"] == "Methods"


def test_pending_sections_never_enter_manuscript_and_parent_heading_is_emitted_once():
    parent = [{"section_id": "S-parent", "title": "Treatment methods"}]
    sections = [
        {"section_id": "S0", "heading": "No source", "generation_mode": "pending_evidence",
         "draft_md": "Evidence pending", "paragraphs": []},
        *[{"section_id": sid, "heading": heading, "heading_level": 3, "parent_headings": parent,
           "paragraphs": [{"paragraph_id": sid + "-p1", "text": "A source-bounded observation.",
                           "paper_id": "P1", "cited_paper_ids": ["P1"]}]}
          for sid, heading in [("S1", "Adsorption"), ("S2", "Filtration")]],
    ]
    markdown = DraftsService._assemble_markdown("Review", {"sections": sections}, {"figures": []},
                                               {"rows": [{"paper_id": "P1", "title": "Study"}]})
    assert "Evidence pending" not in markdown and "No source" not in markdown
    assert markdown.count("## Treatment methods") == 1
    assert "### Adsorption" in markdown and "### Filtration" in markdown


@pytest.mark.parametrize("status,source,expected", [("approved", "bp", ["S2"]),
                                                     ("ready", "bp", ["S1", "S2"]),
                                                     ("approved", "older-bp", ["S1", "S2"])])
def test_only_confirmed_current_omissions_change_evaluation_scope(status, source, expected):
    from review_writer_api.domain_services.actions.draft import quality
    blueprint = {"sections": [{"section_id": "S1"}, {"section_id": "S2"}]}
    index = {"source_blueprint_artifact_id": source, "sections": [
        {"section_id": "S1", "generation_mode": "pending_evidence"}, {"section_id": "S2"}]}
    def read(principal, project_id, name, **kwargs):
        if name == quality.BLUEPRINT_LOGICAL_NAME:
            return deepcopy(blueprint), SimpleNamespace(id="bp")
        if name == quality.SECTION_INDEX:
            return deepcopy(index), SimpleNamespace(id="sections")
        return {}, None
    service = SimpleNamespace(_read_json=read, repository=SimpleNamespace(
        get_owned_project=lambda *a: None, get_stage_state=lambda *a: SimpleNamespace(status=status)))
    result = quality.DraftQualityActionsMixin.compatibility_payload(service, SimpleNamespace(user_id="u"), "project")
    assert [s["section_id"] for s in result["blueprint"]["sections"]] == expected
    assert len(blueprint["sections"]) == 2


def test_empty_body_cannot_be_confirmed_as_a_completed_manuscript():
    from review_writer_api.domain_services.sections import SectionOutputsMissing
    repository = Mock()
    payload = {"handoff": {"current": True}, "section_files": [{}], "section_tasks": [{}],
               "section_drafts": {"sections": [{"section_role": "body", "generation_mode": "pending_evidence"}]}}
    service = SimpleNamespace(get=lambda *a: payload, repository=repository)
    with pytest.raises(SectionOutputsMissing, match="No body section"):
        SectionsService.confirm(service, Mock(user_id="u"), "project", revision=1)
    repository.compare_and_set_stage.assert_not_called()
