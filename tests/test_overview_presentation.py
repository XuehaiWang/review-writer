from review_writer_core.stages.figures.overview_presentation import (
    build_overview_display_contract,
    compact_claim_for_display,
    display_contract_prompt_rows,
    display_texts_are_near_duplicates,
    display_text_is_within_budget,
)


LONG_CLAIM = (
    "The propargylic-alcohol platform emerged after an initial chiral-amine approach "
    "proved operationally unattractive, requiring at least stoichiometric amounts of "
    "expensive chiral amine 6 and failing with an unprotected propargyl alcohol."
)


def test_long_source_claim_is_bounded_for_pixels():
    text = compact_claim_for_display(LONG_CLAIM)
    assert text
    assert display_text_is_within_budget(text)
    assert len(text.split()) <= 12
    assert text != LONG_CLAIM


def test_display_contract_keeps_ids_out_of_visible_prompt():
    execution = {
        "sections": [
            {
                "section_id": "S01",
                "claims": [
                    {
                        "claim_id": "C01",
                        "claim": LONG_CLAIM,
                        "paper_ids": ["P010"],
                        "binding_level": "source_passage",
                    },
                    {
                        "claim_id": "C02",
                        "claim": "A second claim that must not increase card density.",
                        "paper_ids": ["P011"],
                    },
                ],
            }
        ]
    }
    contract = build_overview_display_contract(
        modules=["Axially chiral"],
        argument_execution=execution,
        evidence_bindings={"Axially chiral": {"section_id": "S01"}},
    )
    assert len(contract["modules"][0]["items"]) == 2
    item = contract["modules"][0]["items"][0]
    assert item["claim_id"] == "C01"
    assert item["paper_ids"] == ["P010"]
    assert contract["omitted_claim_count"] == 0

    prompt = display_contract_prompt_rows(contract)
    assert item["text"] in prompt
    assert LONG_CLAIM not in prompt
    assert "P010" not in prompt
    assert "source studies:" not in prompt.lower()


def test_legacy_content_pack_is_compacted_but_not_assigned_by_position():
    contract = build_overview_display_contract(
        modules=["Route A", "Route B"],
        argument_execution=None,
        evidence_bindings=None,
        content_pack={
            "key_findings": [LONG_CLAIM, "Allene 3aa reached 98% ee in 16 h."],
            "cross_cutting": ["Condition-dependent stereoselectivity comparisons"],
            "take_home": ["Mechanistic explanations remain incompletely established"],
        },
    )
    assert len(contract["modules"]) == 2
    assert all(len(module["items"]) <= 1 for module in contract["modules"])
    assert all(not module["items"] for module in contract["modules"])
    assert all(display_text_is_within_budget(text) for text in contract["unassigned_findings"])
    assert len(contract["cross_cutting"][0].split()) <= 4
    assert len(contract["take_home"][0].split()) <= 6


def test_model_mapping_assigns_two_distinct_findings_per_module():
    contract = build_overview_display_contract(
        modules=["Route A", "Route B"],
        argument_execution=None,
        evidence_bindings=None,
        content_pack={
            "key_findings": [
                "Route A reached 98% ee",
                "Route A completed in 16 h",
                "Route B reached 93% yield",
                "Shared condition effect remained unresolved",
            ],
            "module_findings": [
                {"module": "Route A", "finding_indices": [1, 2]},
                {"module": "Route B", "finding_indices": [3]},
            ],
            "cross_cutting": [],
            "take_home": [],
        },
    )
    assert [len(module["items"]) for module in contract["modules"]] == [2, 1]
    assert contract["unassigned_findings"] == [
        "Shared condition effect remained unresolved"
    ]
    assert contract["unassigned_finding_count"] == 1


def test_display_contract_limits_module_count():
    contract = build_overview_display_contract(
        modules=[f"Module {index}" for index in range(8)],
        argument_execution={},
        evidence_bindings={},
    )
    assert len(contract["modules"]) == 5


def test_near_duplicate_metrics_and_claims_are_removed_across_regions():
    contract = build_overview_display_contract(
        modules=["Axially chiral", "Chiral allenes"],
        argument_execution=None,
        evidence_bindings=None,
        content_pack={
            "key_findings": [
                "98% ee, 16 h",
                "Propargylamines gave R-allenes, up to 93% yield",
            ],
            "cross_cutting": ["Origins of stereocontrol", "Chirality transfer"],
            "take_home": [
                "Allenol formation reached 98% ee",
                "Propargylamines access R-allenes",
                "Pd routes furnish allenylmethylsilanes",
            ],
        },
    )
    # Shared 98% ee alone does not establish the same scientific finding.
    assert contract["take_home"] == ["Allenol formation reached 98% ee", "Pd routes furnish allenylmethylsilanes"]
    assert contract["omitted_duplicate_count"] == 1
    assert display_texts_are_near_duplicates(
        "Propargylamines gave R-allenes, up to 93% yield",
        "Propargylamines access R-allenes",
    )


def test_visible_prompt_declares_single_use_card_statements():
    contract = build_overview_display_contract(
        modules=["Route A"],
        argument_execution=None,
        evidence_bindings=None,
        content_pack={"key_findings": ["Route A reached 95% yield"]},
    )
    prompt = display_contract_prompt_rows(contract)
    assert "at most TWO distinct evidence cards" in prompt
    assert "exactly once" in prompt
    assert "never copy it into another card" in prompt
