from review_writer_core.figure_insertion import build_figure_insertion_plan


def _figure(figure_id: str, paper_id: str, paragraph_id: str, **extra):
    row = {
        "figure_id": figure_id,
        "paper_id": paper_id,
        "section_id": paragraph_id.split("-p", 1)[0] if paragraph_id else "",
        "target_paragraph_id": paragraph_id,
        "representative_role": "scope_samples",
        "manuscript_selected": True,
        "usable": True,
    }
    row.update(extra)
    return row


def test_unplaced_paper_asset_is_retained_but_not_inserted() -> None:
    plan = build_figure_insertion_plan([_figure("P001-F01", "P001", "")])
    assert plan[0]["include"] is False
    assert plan[0]["skip_reason"] == "no_supported_paragraph"


def test_plan_uses_representative_subset_per_section() -> None:
    plan = build_figure_insertion_plan(
        [
            _figure("P001-F01", "P001", "S01-p1", representative_role="scope_samples"),
            _figure("P002-F01", "P002", "S01-p2", representative_role="mechanism_model"),
            _figure("P003-F01", "P003", "S01-p3", representative_role="workflow"),
        ]
    )
    assert [row["figure_id"] for row in plan if row["include"]] == [
        "P001-F01",
        "P002-F01",
    ]
    skipped = next(row for row in plan if row["figure_id"] == "P003-F01")
    assert skipped["skip_reason"] == "section_figure_limit"


def test_user_exclusion_is_preserved() -> None:
    plan = build_figure_insertion_plan(
        [_figure("P001-F01", "P001", "S01-p1", manuscript_selected=False)]
    )
    assert plan[0]["include"] is False
    assert plan[0]["skip_reason"] == "user_excluded"


def test_automatically_selected_ineligible_candidate_never_enters_manuscript() -> None:
    plan = build_figure_insertion_plan(
        [
            _figure(
                "P001-F01",
                "P001",
                "S01-p1",
                selection_source="automatic_top_score",
                qualification_enforced=True,
                automatic_selection_eligible=False,
                candidate_qualification={
                    "eligible": False,
                    "reasons": ["table_or_optimization_screenshot"],
                },
            )
        ]
    )
    assert plan[0]["include"] is False
    assert plan[0]["skip_reason"] == "candidate_below_minimum_qualification"


def test_human_selection_can_keep_a_candidate_in_the_paper_pool() -> None:
    plan = build_figure_insertion_plan(
        [
            _figure(
                "P001-F01",
                "P001",
                "S01-p1",
                selection_source="human",
                qualification_enforced=True,
                automatic_selection_eligible=False,
                candidate_qualification={"eligible": False},
            )
        ]
    )
    assert plan[0]["include"] is True
    assert plan[0]["visible_callout_required"] is True
