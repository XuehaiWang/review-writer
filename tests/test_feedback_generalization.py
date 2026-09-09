import pytest

from review_writer_core.draft_issue_routing import route_draft_issue
from review_writer_core.prose_text import prose_sentences, prose_comparison_key
from review_writer_core.provider_errors import normalize_provider_error
from review_writer_core.writing_contracts import paragraph_finding_is_blocking
from review_writer_api.domain_services.drafts import DraftsService


@pytest.mark.parametrize("diagnosis", [
    "The bibliography is correct; the figure callout is appropriate.",
    "图示引用缺失。", "Un libellé complètement différent.",
])
def test_structured_category_does_not_depend_on_diagnosis_language(diagnosis):
    for category, stage in [("presentation", "draft"), ("figure", "figures"), ("bibliography", "bibliography")]:
        issue = {"finding_category": category, "diagnosis": diagnosis}
        assert route_draft_issue(issue, source_status="verified")["repair_stage"] == stage


@pytest.mark.parametrize(("diagnosis", "stage"), [
    ("The bibliography is correct; this paragraph needs clearer wording.", "draft"),
    ("The figure callout is poorly positioned.", "figures"),
    ("图示引用缺失。", "figures"),
])
def test_legacy_report_defects_are_distinguished_from_positive_mentions(diagnosis, stage):
    assert route_draft_issue({"diagnosis": diagnosis}, source_status="verified")["repair_stage"] == stage


def test_new_style_rule_uses_category_but_cannot_override_a_scientific_violation():
    issue = {"rule": "STYLE_READABILITY", "finding_category": "presentation", "severity": "major", "source_check_status": "verified"}
    assert not paragraph_finding_is_blocking(issue)
    assert paragraph_finding_is_blocking({**issue, "unsupported_claims": ["unreported yield"]})
    assert paragraph_finding_is_blocking({**issue, "rule": "C04"})
    assert paragraph_finding_is_blocking({**issue, "missing_core_claim_ids": ["thesis"]})


def test_score_category_reaches_public_issue_and_historical_routing_is_replaced():
    issues, _ = DraftsService._quality_routing({
        "issues": [{"issue_id": "I1", "paragraph_id": "p1"}],
        "paragraph_scores": [{"paragraph_id": "p1", "finding_category": "figure", "source_check_status": "verified"}],
    }, {"section_index": {}, "section_evidence": {}})
    assert issues[0]["repair_stage"] == "figures"
    old = {"repair_routing_version": 3, "repair_stage": "bibliography", "repair_route": "bibliography_repair",
           "diagnosis": "The bibliography is correct; the wording needs improvement.", "source_check_status": "verified"}
    assert DraftsService._public_issue_repair_metadata(old)["repair_stage"] == "draft"


@pytest.mark.parametrize("text", ["结果一致。结果一致。", "Δοκιμή. Δοκιμή.", "Результат. Результат."])
def test_unicode_repeat_keys_retain_non_latin_meaning(text):
    sentences = prose_sentences(text)
    assert len(sentences) == 2
    keys = [prose_comparison_key(sentence) for sentence in sentences]
    assert keys[0] and keys[0] == keys[1]
    assert prose_comparison_key("结果不同") != prose_comparison_key("结果一致")


@pytest.mark.parametrize(("code", "category"), [
    ("insufficient_quota", "quota_exhausted"), ("context_length_exceeded", "context_limit"),
    ("too_many_concurrent_requests", "rate_limited"), ("invalid_api_key", "authentication"),
])
def test_provider_code_overrides_free_form_error_text(code, category):
    error = normalize_provider_error(503, {"error": {"code": code, "message": "Arbitrary translated provider message"}})
    assert error["category"] == category
    wrapped = {"code": error["code"], "message": "Gateway wrapper", "details": error}
    assert normalize_provider_error(502, wrapped)["category"] == category
