from review_writer_core.draft_issue_routing import route_draft_issue


def test_required_claim_gap_routes_to_evidence_before_rewrite() -> None:
    routed = route_draft_issue(
        {
            "paragraph_id": "S02-p3",
            "message": "required_claim_01 has no supporting evidence",
        },
        source_status="partially_supported",
        has_original_passages=True,
    )
    assert routed["repair_stage"] == "evidence_package"
    assert routed["repair_route"] == "targeted_evidence_then_paragraph_rewrite"
    assert routed["rewrite_eligible"] is True


def test_synthesis_gap_does_not_get_hidden_by_paragraph_rewrite() -> None:
    routed = route_draft_issue(
        {
            "paragraph_id": "S03-p4",
            "message": "The section has insufficient comparison and no synthesis exit.",
        }
    )
    assert routed["repair_stage"] == "writing_plan"
    assert routed["repair_action"] == "rebuild_section_synthesis_and_writing_plan"
    assert routed["rewrite_eligible"] is False


def test_figure_and_bibliography_findings_have_distinct_owners() -> None:
    figure = route_draft_issue(
        {"message": "visible_callout_or_interpretation_missing"}
    )
    bibliography = route_draft_issue(
        {"message": "reference field is missing journal and article number"}
    )
    assert figure["repair_stage"] == "figures"
    assert bibliography["repair_stage"] == "bibliography"


def test_verified_source_wording_is_not_misrouted_to_evidence_package() -> None:
    routed = route_draft_issue(
        {
            "paragraph_id": "S04-p6",
            "message": "The source-supported comparison needs clearer synthesis.",
            "failed_dimensions": ["P04"],
        },
        source_status="verified",
        evaluator_route="section_rewrite",
        has_original_passages=True,
    )
    assert routed["repair_stage"] == "draft"
    assert routed["repair_route"] == "paragraph_rewrite"


def test_generic_literature_word_does_not_force_discovery_return() -> None:
    routed = route_draft_issue(
        {
            "paragraph_id": "S01-p2",
            "message": "Improve the literature synthesis in this paragraph.",
        },
        source_status="verified",
        evaluator_route="section_rewrite",
    )
    assert routed["repair_stage"] == "draft"


def test_legacy_derived_route_does_not_perpetuate_an_old_misroute() -> None:
    routed = route_draft_issue(
        {
            "paragraph_id": "S01-p2",
            "message": "This introduction compresses three literature anchors.",
            "issue_type": "literature_coverage_gap",
            "repair_stage": "discovery",
            "repair_route": "manual_online_retrieval_decision",
            "failed_dimensions": ["P01"],
        },
        source_status="verified",
        evaluator_route="section_rewrite",
    )
    assert routed["repair_stage"] == "draft"


def test_explicit_coverage_gap_still_routes_to_discovery() -> None:
    routed = route_draft_issue(
        {
            "paragraph_id": "S01-p2",
            "message": "The retrieval coverage is missing a primary study.",
        }
    )
    assert routed["repair_stage"] == "discovery"
    assert routed["auto_repairable"] is False


def test_issue_fingerprint_is_stable_across_diagnosis_wording() -> None:
    first = route_draft_issue(
        {
            "paragraph_id": "S02-p3",
            "message": "required_claim_01 has no supporting evidence",
            "failed_dimensions": ["C01"],
        },
        source_status="partially_supported",
        has_original_passages=True,
    )
    second = route_draft_issue(
        {
            "paragraph_id": "S02-p3",
            "message": "Evidence remains absent for required_claim_01.",
            "failed_dimensions": ["C01"],
        },
        source_status="partially_supported",
        has_original_passages=True,
    )
    assert first["issue_fingerprint"] == second["issue_fingerprint"]
