from review_writer_core.review_fact_readiness import (
    evidence_problem_type,
    is_figure_callout_only,
    negative_claim_policy,
)


def test_figure_placement_sentence_is_not_a_paper_fact() -> None:
    assert is_figure_callout_only(
        "Figure 2 summarizes representative transformations from the study."
    )
    assert not is_figure_callout_only(
        "Figure 2 shows that the catalyst affords the product in 95% yield."
    )


def test_retrieval_miss_cannot_prove_publication_level_negative() -> None:
    assert negative_claim_policy(
        "The study does not establish the catalyst role.",
        evidence_texts=["The study reports the optimized reaction conditions."],
    ) == "scope_limited_rewrite"


def test_explicit_negative_source_statement_can_remain_bounded() -> None:
    assert negative_claim_policy(
        "The experiment did not produce the target product.",
        evidence_texts=["The control experiment did not produce the target product."],
    ) == "explicit_source_statement"


def test_evidence_problem_root_distinguishes_retrieval_and_binding() -> None:
    assert evidence_problem_type(
        unsupported_claims=["The catalyst gave 95% yield."],
        source_check_status="partially_supported",
        source_ready=True,
    ) == "extraction_miss"
    assert evidence_problem_type(
        unsupported_claims=["The catalyst gave 95% yield."],
        source_check_status="partially_supported",
        source_evidence_refs=["P001:chunk-1"],
        source_ready=True,
    ) == "binding_mismatch"
