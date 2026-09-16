#!/usr/bin/env python3
"""Score and iteratively improve a merged review draft without whole-draft regeneration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BOOTSTRAP_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / "review_writer_core").is_dir()),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.provider_errors import normalize_provider_error, provider_error_message
from review_writer_core.providers import (  # noqa: E402
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_TEXT_MODEL,
    DEFAULT_TEXT_WIRE_API,
    openai_endpoint,
)
from review_writer_core.paragraph_markers import (  # noqa: E402
    PARAGRAPH_MARKER_RE,
    ensure_prose_paragraph_markers,
    parse_marked_paragraphs,
    split_body_and_references as split_body_references,
)
from review_writer_core.prose_text import prose_sentences, prose_comparison_key
from review_writer_core.stages.draft.source_corrections import verified_corrections, corrected_baseline
from review_writer_core.text_safety import make_xml_compatible  # noqa: E402
from review_writer_core.scientific_facts import fact_support_spans, REVIEW_COMPARISON_POLICY  # noqa: E402
from review_writer_core.claim_contracts import argument_projection  # noqa: E402
from review_writer_core.quality_rules import FINDING_CATEGORIES, finding_category
from review_writer_core.draft_issue_routing import repair_input_fingerprint, paragraph_repair_contract, select_rewrite_mode  # noqa: E402
from review_writer_core.draft_quality import (  # noqa: E402
    DEFAULT_SCORE_TOLERANCE,
    DRAFT_QUALITY_RULE_VERSION,
    FULL_DRAFT_QUALITY_SCOPE,
)
from review_writer_core.publication_voice import publication_voice_issues  # noqa: E402
from review_writer_core.source_attribution import (  # noqa: E402
    SOURCE_ATTRIBUTION_POLICY, CONTRIBUTION_WRITING_POLICY, SECTION_THREAD_POLICY,
    section_navigation_context, attribution_repair_instruction, validated_attribution_repair,
)
from review_writer_core.section_narrative_contracts import (  # noqa: E402
    canonical_argument_role,
)
from review_writer_core.writing_contracts import (  # noqa: E402
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    DRAFT_PASS_THRESHOLD,
    PARAGRAPH_PASS_THRESHOLD,
    paragraph_finding_is_blocking,
)
from review_writer_core.taxonomy_verification import (  # noqa: E402
    load_taxonomy_verification_profile,
)
from review_writer_core.review_fact_readiness import (  # noqa: E402
    evidence_problem_type,
    is_figure_callout_only,
    is_strong_negative_claim,
    negative_claim_policy,
)
from review_writer_core.model_gateway_client import (  # noqa: E402
    GatewayRequestError,
    call_json_model as gateway_call_json_model,
    parse_json_object_text,
)


MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
INSERTED_FIGURE_RE = re.compile(r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.S)
REFERENCES_RE = re.compile(
    r"^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$",
    re.I | re.M,
)
CALLOUT_RE = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")
LABEL_SCAFFOLD_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)(?:reaction conditions?|substrate scope|selectivity|mechanism|"
    r"limitations?|evidence ceiling|method and activation mode)\s*:\s*",
    re.I,
)
SCAFFOLD_RE = re.compile(
    r"The paper reports the following|At the reaction level|The method description further emphasizes|"
    r"For operational context|The retained metric record|The evidence ceiling is equally important",
    re.I,
)
# Cloudflare uses 524 when it connected to the configured model provider but
# the provider did not finish within Cloudflare's proxy read window.  This is
# a transient upstream timeout, just like 504, and must not turn an otherwise
# valid draft into a permanent scientific-evaluation failure.
TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 524}
MAX_REWRITE_ATTEMPTS = 2
DEFAULT_EVALUATION_BATCH_SIZE = 8
DEFAULT_PROVIDER_REQUEST_ATTEMPTS = 5
MAX_PROVIDER_REQUEST_ATTEMPTS = 8
REVIEW_SYNTHESIS_ROLES = (
    "section_frame", "cross_study_comparison", "mechanism_boundary",
    "scope_limitation", "section_synthesis_exit",
)
REVIEW_EVIDENCE_POLICY = (
    "Resolve each original_passages.text_ref against passage_texts in the same evidence object. "
    "Read the complete passage, including its final sentences and table notes; text deduplication never changes "
    "a passage's paper_id, ref, or claim binding. Return original passage refs, never text_ref identifiers. "
    "Assess current text against current evidence independently of earlier diagnoses or proposed downgrades. "
    "A review author's explicit choice of organization or clearly labelled synthesis is not an original paper's "
    "scientific claim merely because it appears beside a citation. Do not flag it solely because no source uses "
    "the same wording. Still verify every factual premise, number, scope, causal or mechanistic assertion; "
    "calling a statement synthesis does not license unsupported scientific conclusions. "
    "Keep diagnosis, unsupported_claims, failed_dimensions and source_check_status consistent. "
    "For each missing_core_claim_id, identify in the diagnosis which required proposition is absent "
    "from the current paragraph; assess semantic coverage, not verbatim repetition of the plan or ID. "
    "Do not report a missing thesis while praising its complete realization in the same paragraph. "
)


class ProviderDeadlineExceeded(RuntimeError):
    """The upstream proxy deadline was exceeded for a bounded model request."""


class ProviderRequestBodyBudgetExceeded(RuntimeError):
    """The provider relay rejected a request before model execution."""


def provider_request_attempts() -> int:
    """Return a bounded retry count for one provider request.

    A feedback-loop task can make several paid model calls. Retrying the
    individual failed call is safer than replaying the whole task after some
    earlier calls have already completed. Keep the value configurable for
    deployments whose proxy has a different recovery window.
    """

    raw = str(
        os.environ.get("REVIEW_WRITING_PROVIDER_ATTEMPTS")
        or DEFAULT_PROVIDER_REQUEST_ATTEMPTS
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_PROVIDER_REQUEST_ATTEMPTS
    return max(1, min(value, MAX_PROVIDER_REQUEST_ATTEMPTS))


def provider_retry_delay(attempt: int, retry_after: str = "") -> float:
    """Use provider guidance when present, otherwise a bounded backoff."""

    try:
        requested = float(str(retry_after or "").strip())
    except ValueError:
        requested = 0.0
    if requested > 0:
        return min(requested, 30.0)
    return min(2.0 ** max(0, attempt), 20.0)


def provider_concurrency_retry_delay(attempt: int, retry_after: str = "") -> float:
    """Give a saturated relay enough time to release an execution slot."""

    try:
        requested = float(str(retry_after or "").strip())
    except ValueError:
        requested = 0.0
    if requested > 0:
        return min(max(requested, 5.0), 60.0)
    return min(5.0 * (2.0 ** max(0, attempt - 1)), 30.0)


def request_body_budget_exhausted(body: str) -> bool:
    folded = str(body or "").casefold()
    return (
        "request_body_budget_exhausted" in folded
        or "request body budget is exhausted" in folded
    )


def provider_concurrency_exhausted(body: str) -> bool:
    folded = str(body or "").casefold()
    return (
        "too_many_concurrent_requests" in folded
        or "too many concurrent requests" in folded
    )


def recoverable_paragraph_provider_failure(exc: BaseException) -> bool:
    """Return whether one paragraph can be deferred without aborting the batch.

    Authentication/configuration failures are intentionally excluded: retrying
    every paragraph with an invalid global configuration only wastes time.  The
    failures below are request-local or transient provider conditions, so the
    remaining paragraph queue can still make useful progress.
    """

    if isinstance(exc, GatewayRequestError):
        return exc.status_code in TRANSIENT_HTTP_CODES
    if isinstance(
        exc,
        (ProviderDeadlineExceeded, ProviderRequestBodyBudgetExceeded),
    ):
        return True
    folded = str(exc or "").casefold()
    transient_markers = (
        "http 408",
        "http 409",
        "http 425",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "http 524",
        "too_many_concurrent_requests",
        "too many concurrent requests",
        "request_body_budget_exhausted",
        "request body budget is exhausted",
        "transport failed",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "json failure",
        "empty response",
    )
    return any(marker in folded for marker in transient_markers)


def repeated_run_junk_token(value: str) -> bool:
    """Recognize obvious OCR/user noise without mistaking normal chemistry labels."""

    token = str(value or "").strip(".,;:!?()[]{}\"'")
    if len(token) < 8 or not token.isalpha():
        return False
    run_lengths: list[int] = []
    run_length = 0
    previous = ""
    for character in token.casefold():
        if character == previous:
            run_length += 1
        else:
            if run_length:
                run_lengths.append(run_length)
            previous = character
            run_length = 1
    if run_length:
        run_lengths.append(run_length)
    repeated_runs = [length for length in run_lengths if length >= 2]
    return (
        len(repeated_runs) >= 3
        and sum(repeated_runs) / len(token) >= 0.75
    )


def edge_junk_tokens(text: str) -> list[str]:
    words = str(text or "").strip().split()
    if not words:
        return []
    values = [words[0]]
    if len(words) > 1:
        values.append(words[-1])
    return [value for value in values if repeated_run_junk_token(value)]


def remove_edge_junk_tokens(text: str) -> str:
    """Remove only high-confidence repeated-run noise at paragraph boundaries."""

    words = str(text or "").strip().split()
    while words and repeated_run_junk_token(words[0]):
        words.pop(0)
    while words and repeated_run_junk_token(words[-1]):
        words.pop()
    return " ".join(words)

PROTECTED_NUMBER_RE = re.compile(
    r"(?<![A-Za-z])(?:\d+(?:\.\d+)?(?:\s*(?:%|mol%|°C|K|h|min|s|equiv|M|mM))?|"
    r"\d+\s*:\s*\d+)(?![A-Za-z])",
    re.I,
)
STEREO_RE = re.compile(
    r"\b(?:ee|er|dr|de|R|S|E|Z)\b",
    re.I,
)
EVIDENCE_BOUNDARY_RE = re.compile(
    r"\b(?:does not establish|cannot (?:establish|support|demonstrate)|"
    r"insufficient evidence|evidence (?:is|remains) (?:insufficient|limited)|"
    r"cannot be concluded from (?:the )?(?:available|selected) evidence)\b",
    re.I,
)
# Protect only explicit manuscript/chemical labels.  The former expression
# allowed `int` to consume the prefix of ordinary words such as
# "interpretation", "intermolecular", and "into", rejecting harmless prose
# rewrites as if an intermediate label had changed.
REQUIRED_LABEL_RE = re.compile(
    r"(?:"
    r"\b(?i:int(?:ermediate)?)\b\s*[-:]?\s*(?:[IVX]+|\d+[A-Za-z]*|[A-Z])\b"
    r"|\b(?i:TS)(?:\s*[-:]?\s*\d+[A-Za-z]*|\s+[A-Z])\b"
    r"|\b(?i:complex|compound|product|species)\b\s*[-:]?\s*(?:\d+[A-Za-z]*|[A-Z])\b"
    r")"
)
CHEMICAL_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+*/().\-′']*")
CHEMICAL_SUFFIXES: tuple[str, ...] = ()
CHEMICAL_ELEMENTS_AND_METALS: set[str] = set()
EXPLICIT_CHEMICAL_SYMBOLS: set[str] = set()
SOFT_STEREO_RE = re.compile(r"(?!x)x")
HARD_PROTECTED_FIELDS = {
    "callouts",
    "numbers",
    "stereo",
    "chemical_identities",
    "required_labels",
    "images",
    "figure_metadata",
}
SOFT_PROTECTED_FIELDS = {"soft_chemical_terms", "soft_stereo_terms"}
SOURCE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+*/().\-′']{2,}|\d+(?:\.\d+)?(?:%|°C)?")
SOURCE_CJK_RE = re.compile(r"[\u3400-\u9fff]{2,}")
PAPER_PARAGRAPH_ID_RE = re.compile(r"^(P\d{3,})(?:[-_.]|$)", re.I)
SOURCE_STOPWORDS = {
    "about", "after", "also", "among", "because", "been", "before", "between",
    "could", "from", "have", "into", "more", "only", "other", "reported", "study",
    "than", "that", "their", "these", "this", "through", "under", "using", "were",
    "whereas", "which", "with", "without", "would",
}

MAX_SOURCE_PASSAGES_PER_PAPER = 4
MAX_SOURCE_PASSAGE_CHARS = 700
CROSS_LANGUAGE_CHEMISTRY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = ()


def apply_verification_profile(profile: dict[str, Any]) -> None:
    """Install the active project's domain guards for this worker process."""

    global CHEMICAL_SUFFIXES
    global CHEMICAL_ELEMENTS_AND_METALS
    global EXPLICIT_CHEMICAL_SYMBOLS
    global SOFT_STEREO_RE
    global CROSS_LANGUAGE_CHEMISTRY_TERMS

    CHEMICAL_SUFFIXES = tuple(
        str(value).casefold() for value in profile.get("chemical_suffixes") or []
    )
    CHEMICAL_ELEMENTS_AND_METALS = {
        str(value).casefold() for value in profile.get("named_entities") or []
    }
    EXPLICIT_CHEMICAL_SYMBOLS = {
        str(value) for value in profile.get("explicit_symbols") or []
    }
    stereo = [
        re.escape(str(value))
        for value in profile.get("soft_stereo_terms") or []
        if str(value).strip()
    ]
    SOFT_STEREO_RE = (
        re.compile(r"\b(?:" + "|".join(stereo) + r")\b", re.I)
        if stereo
        else re.compile(r"(?!x)x")
    )
    CROSS_LANGUAGE_CHEMISTRY_TERMS = tuple(
        (
            str(item[0]).casefold(),
            tuple(str(value) for value in item[1]),
        )
        for item in profile.get("cross_language_terms") or []
        if isinstance(item, (list, tuple))
        and len(item) == 2
        and isinstance(item[1], (list, tuple))
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_rewrite_queue_checkpoint(
    path: Path,
    *,
    project_id: str,
    run_id: str,
    iteration: int,
    source_draft_sha256: str,
    current_draft_sha256: str,
    rewrite_items: list[dict[str, Any]],
    accepted: int,
    rejected: int,
    deferred: int,
    state: str = "running",
) -> dict[str, Any]:
    """Atomically persist paragraph-level progress for refresh and recovery."""

    payload = {
        "schema_version": 1,
        "project_id": project_id,
        "run_id": run_id,
        "iteration": int(iteration),
        "state": state,
        "source_draft_sha256": source_draft_sha256,
        "current_draft_sha256": current_draft_sha256,
        "rewrite_total": len(rewrite_items),
        "rewrite_completed": sum(
            1
            for item in rewrite_items
            if str(item.get("status") or "")
            in {"completed", "rejected", "deferred", "skipped"}
        ),
        "rewrite_accepted": int(accepted),
        "rewrite_rejected": int(rejected),
        "rewrite_deferred": int(deferred),
        "items": rewrite_items,
        "updated_at": utc_now(),
    }
    write_json(path, payload)
    return payload


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_contains_text(container: Any, excerpt: Any) -> bool:
    """Return whether an exact, whitespace-normalized source excerpt exists."""

    haystack = clean_text(container).casefold()
    needle = clean_text(excerpt).casefold()
    return bool(needle) and needle in haystack


def numeric_tokens(value: Any) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", clean_text(value)))


def validated_claim_fact_bindings(
    raw_bindings: Any,
    *,
    paragraph_text: str,
    paragraph_evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate model-proposed Claim→Fact bindings against original passages.

    This is deliberately stricter than ordinary source checking.  A passage
    may repair the Evidence Package without becoming a Matrix Fact; promotion
    is allowed only when the model supplies an exact excerpt, the paper and
    reference are in the paragraph contract, and every numeric value is
    present in that excerpt.
    """

    passage_by_ref: dict[str, dict[str, str]] = {}
    for paper in paragraph_evidence.get("evidence") or []:
        if not isinstance(paper, dict):
            continue
        paper_id = clean_text(paper.get("paper_id"))
        for passage in paper.get("original_passages") or []:
            if not isinstance(passage, dict):
                continue
            ref = clean_text(passage.get("ref"))
            text = clean_text(passage.get("text"))
            if ref and paper_id and text:
                passage_by_ref[ref] = {"paper_id": paper_id, "text": text}

    paragraph_normalized = clean_text(paragraph_text).casefold()
    accepted: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_bindings or []:
        if not isinstance(raw, dict):
            continue
        claim_text = clean_text(raw.get("claim_text"))
        paper_id = clean_text(raw.get("paper_id"))
        source_ref = clean_text(raw.get("source_ref"))
        excerpt = clean_text(raw.get("support_excerpt"))
        subject = clean_text(raw.get("subject"))
        predicate = clean_text(raw.get("predicate"))
        value = clean_text(raw.get("value") or raw.get("normalized_value"))
        source = passage_by_ref.get(source_ref)
        if (
            not claim_text
            or claim_text.casefold() not in paragraph_normalized
            or source is None
            or source["paper_id"] != paper_id
            or not normalized_contains_text(source["text"], excerpt)
            or not subject
            or not predicate
            or not value
        ):
            continue
        # Numbers are high-risk anchors.  Do not allow the model to promote a
        # value that cannot be found verbatim in the cited original excerpt.
        if not fact_support_spans({"value": value, "evidence_key": source_ref, "support_excerpt": excerpt},
                                  {source_ref: {"content": source["text"]}}):
            continue
        if is_strong_negative_claim(claim_text) and negative_claim_policy(
            claim_text, evidence_texts=[excerpt]
        ) != "explicit_negative_supported":
            continue
        qualifiers = raw.get("qualifiers")
        qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
        safe_qualifiers = {
            clean_text(key): clean_text(item)
            for key, item in qualifiers.items()
            if clean_text(key)
            and clean_text(item)
            and normalized_contains_text(excerpt, item)
        }
        key = (paper_id, source_ref, value.casefold())
        if key in seen:
            continue
        seen.add(key)
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        accepted.append(
            {
                "claim_text": claim_text,
                "paper_id": paper_id,
                "source_ref": source_ref,
                "support_excerpt": excerpt,
                "fact_type": clean_text(raw.get("fact_type") or "claim_support"),
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "normalized_value": clean_text(raw.get("normalized_value") or value),
                "unit": clean_text(raw.get("unit")),
                "qualifiers": safe_qualifiers,
                "confidence": confidence,
            }
        )
    return accepted


def metadata_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def section_payload(project: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = project / "02_section_drafting" / "section_drafts.json"
    payload = read_json(path, {})
    sections = payload.get("sections") if isinstance(payload, dict) else payload
    if not isinstance(sections, list):
        raise RuntimeError(f"Invalid section draft envelope: {path}")
    return payload if isinstance(payload, dict) else {"sections": sections}, sections


def paragraph_metadata(project: Path) -> dict[str, dict[str, Any]]:
    _, sections = section_payload(project)
    result: dict[str, dict[str, Any]] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if isinstance(paragraph, dict) and paragraph.get("paragraph_id"):
                result[str(paragraph["paragraph_id"])] = paragraph
    writing = draft_writing_plan(project)
    for section in writing.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for index, paragraph in enumerate(section.get("paragraphs") or []):
            if not isinstance(paragraph, dict) or not paragraph.get("paragraph_id"):
                continue
            paragraph_id = str(paragraph["paragraph_id"])
            current = dict(result.get(paragraph_id) or {})
            current.update(
                {
                    key: value
                    for key, value in paragraph.items()
                    if value not in (None, "", [], {})
                }
            )
            current["argument_role"] = canonical_argument_role(
                current.get("argument_role"),
                paragraph_index=index,
                paragraph_count=len(section.get("paragraphs") or []),
                paper_count=len(current.get("paper_ids") or []),
            )
            result[paragraph_id] = current
    return result


def paragraph_argument_role(structured_paragraph: dict[str, Any]) -> str:
    """Return the Writing Plan role without treating all prose as a case."""

    if not structured_paragraph:
        return "supporting"
    declared = str(structured_paragraph.get("argument_role") or "").strip()
    if not declared:
        # Legacy section artifacts predate role-aware Writing Plans.  Preserve
        # their previous case-paragraph behavior until they are regenerated.
        return "anchor_case"
    return canonical_argument_role(
        declared,
        paper_count=len(structured_paragraph.get("paper_ids") or []),
    )


def draft_writing_plan(project: Path, *, effective: bool = True) -> dict[str, Any]:
    from review_writer_core.stages.draft.revisions import effective_writing_plan
    path = project / "02_section_drafting" / "baseline_writing_plan.json"
    if not path.is_file():
        path = project / "02_section_drafting" / "writing_plan.json"
    baseline = read_json(path, {})
    return effective_writing_plan(baseline, read_json(
        project / "04_first_draft" / "feedback_loop_rewrites.json", {})) if effective else baseline


def claim_evidence_contract(project: Path) -> dict[str, Any]:
    """Load the exact Claim-to-chunk contract for draft evaluation."""

    writing = draft_writing_plan(project)
    package = read_json(project / "02_section_drafting" / "section_evidence.json", {})
    claims: dict[str, dict[str, Any]] = {}
    paragraph_claim_ids: dict[str, list[str]] = {}
    for section in writing.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for claim in section.get("claims") or []:
            if not isinstance(claim, dict) or not claim.get("claim_id"):
                continue
            claim_id = str(claim["claim_id"])
            claims[claim_id] = claim
            paragraph_id = str(claim.get("paragraph_id") or "")
            if paragraph_id:
                paragraph_claim_ids.setdefault(paragraph_id, []).append(claim_id)
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, dict) or not paragraph.get("paragraph_id"):
                continue
            paragraph_id = str(paragraph["paragraph_id"])
            declared = [
                str(value)
                for value in paragraph.get("claim_ids") or []
                if str(value) in claims
            ]
            if declared:
                paragraph_claim_ids[paragraph_id] = declared

    evidence_by_key: dict[str, dict[str, Any]] = {}
    for row in package.get("evidence_registry") or []:
        if isinstance(row, dict) and row.get("evidence_key"):
            evidence_by_key[str(row["evidence_key"])] = row
    # A section-local direct hit is stronger than a global registry entry that
    # first encountered the same chunk only as neighboring context.
    for section in package.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for row in section.get("hits") or []:
            if not isinstance(row, dict) or not row.get("evidence_key"):
                continue
            key = str(row["evidence_key"])
            current = evidence_by_key.get(key)
            if current is None or (
                bool(row.get("claim_eligible"))
                and not bool(current.get("claim_eligible"))
            ):
                evidence_by_key[key] = row
    draft_path = project / "04_first_draft" / "first_draft.md"
    current_paragraphs = parse_marked_paragraphs(draft_path.read_text(encoding="utf-8", errors="replace")) if draft_path.is_file() else []
    return {
        "claims": claims,
        "paragraph_claim_ids": paragraph_claim_ids,
        "evidence_by_key": evidence_by_key,
        "paragraph_fact_supplements": package.get("paragraph_fact_supplements") or {},
        "section_navigation": section_navigation_context(writing, current_paragraphs),
    }


def claim_bound_evidence(
    paragraph_id: str,
    structured: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Resolve exact indexed chunks for a generated paragraph's Claims."""

    claim_ids = [
        str(item.get("claim_id") or "")
        for item in structured.get("claim_realizations") or []
        if isinstance(item, dict) and str(item.get("claim_id") or "")
    ]
    if not claim_ids:
        claim_ids = list(
            (contract.get("paragraph_claim_ids") or {}).get(paragraph_id) or []
        )
    claims = contract.get("claims") or {}
    evidence_by_key = contract.get("evidence_by_key") or {}
    by_paper: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for claim_id in claim_ids:
        claim = claims.get(claim_id)
        if not isinstance(claim, dict):
            continue
        allowed_papers = {
            str(value) for value in claim.get("citation_group") or [] if str(value)
        }
        for ref in claim.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            evidence_key = str(ref.get("evidence_key") or "")
            row = evidence_by_key.get(evidence_key)
            if (
                not isinstance(row, dict)
                or not bool(row.get("claim_eligible"))
                or not clean_text(row.get("content") or row.get("evidence"))
            ):
                continue
            paper_id = str(row.get("paper_id") or "")
            if not paper_id or (allowed_papers and paper_id not in allowed_papers):
                continue
            identity = (paper_id, evidence_key)
            if identity in seen:
                continue
            seen.add(identity)
            by_paper.setdefault(paper_id, []).append(
                {
                    "ref": evidence_key,
                    "evidence_key": evidence_key,
                    "chunk_id": str(row.get("chunk_id") or ""),
                    "page": row.get("page_start"),
                    "page_end": row.get("page_end"),
                    "claim_id": claim_id,
                    "text": clean_text(row.get("content") or row.get("evidence")),
                }
            )
    allowed = {str(value) for claim_id in claim_ids
               for value in (claims.get(claim_id) or {}).get("citation_group") or []}
    for ref in (contract.get("paragraph_fact_supplements") or {}).get(paragraph_id) or []:
        key = str(ref.get("evidence_key") or "")
        row = evidence_by_key.get(key) or {}
        paper_id = str(row.get("paper_id") or "")
        if paper_id not in allowed or not row.get("claim_eligible") or (paper_id, key) in seen:
            continue
        text = clean_text(row.get("content") or row.get("text"))
        if not text:
            continue
        seen.add((paper_id, key))
        by_paper.setdefault(paper_id, []).append({
            "ref": key, "evidence_key": key, "chunk_id": str(row.get("chunk_id") or ""),
            "page": row.get("page_start"), "page_end": row.get("page_end"),
            "text": text, "purpose": "fact_agent_context",
        })
    return by_paper


def citation_entries(project: Path) -> list[dict[str, Any]]:
    payload = read_json(project / "04_first_draft" / "citations.json", {})
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    return entries if isinstance(entries, list) else []


def paragraph_citation_binding(project, paragraph):
    callouts = expand_callouts(str(paragraph.get('text') or ''))
    return {str(e['callout']): str(e['paper_id']) for e in citation_entries(project)
            if str(e.get('callout') or '').isdigit() and int(e['callout']) in callouts and e.get('paper_id')}


def matrix_rows(project: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(project / "01_matrix_outline" / "literature_matrix.json", {})
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return {
        str(row.get("paper_id")): row
        for row in rows or []
        if isinstance(row, dict) and row.get("paper_id")
    }


def metadata_record(review_root: Path, paper_id: str) -> dict[str, Any]:
    path = review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    return read_json(path, {}) if path.is_file() else {}


def resolve_source_path(review_root: Path, raw: Any) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else review_root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(review_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _source_blocks_from_content_list(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path, [])
    if not isinstance(payload, list):
        return []
    raw_blocks: list[tuple[int | None, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text") or item.get("content") or "")
        if not text:
            continue
        raw_page = item.get("page_idx")
        page = int(raw_page) + 1 if isinstance(raw_page, int) and raw_page >= 0 else None
        raw_blocks.append((page, text))

    blocks: list[dict[str, Any]] = []
    current_page: int | None = None
    current_parts: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current_parts, current_chars
        if current_parts:
            blocks.append(
                {
                    "page": current_page,
                    "text": clean_text(" ".join(current_parts)),
                }
            )
        current_parts = []
        current_chars = 0

    for page, text in raw_blocks:
        if current_parts and (
            page != current_page or current_chars + len(text) > MAX_SOURCE_PASSAGE_CHARS
        ):
            flush()
        current_page = page
        current_parts.append(text)
        current_chars += len(text) + 1
    flush()
    return blocks


def _source_blocks_from_markdown(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    parts = [clean_text(value) for value in re.split(r"\n\s*\n", text)]
    parts = [value for value in parts if value]
    blocks: list[dict[str, Any]] = []
    for part in parts:
        for offset in range(0, len(part), MAX_SOURCE_PASSAGE_CHARS):
            chunk = clean_text(part[offset : offset + MAX_SOURCE_PASSAGE_CHARS])
            if chunk:
                blocks.append({"page": None, "text": chunk})
    return blocks


def _source_blocks_from_pdf(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return []
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [
            {"page": index, "text": clean_text(page.extract_text() or "")}
            for index, page in enumerate(reader.pages, start=1)
            if clean_text(page.extract_text() or "")
        ]
    except Exception:
        return []


def load_original_source(
    review_root: Path,
    paper_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    source_paths = metadata.get("source_paths") if isinstance(metadata, dict) else {}
    source_paths = source_paths if isinstance(source_paths, dict) else {}
    candidates = [
        ("mineru_content_list", resolve_source_path(review_root, source_paths.get("content_list"))),
        ("mineru_markdown", resolve_source_path(review_root, source_paths.get("markdown"))),
        ("pdf_text", resolve_source_path(review_root, source_paths.get("pdf"))),
    ]
    for source_kind, path in candidates:
        if path is None or not path.is_file():
            continue
        if source_kind == "mineru_content_list":
            blocks = _source_blocks_from_content_list(path)
        elif source_kind == "mineru_markdown":
            blocks = _source_blocks_from_markdown(path)
        else:
            blocks = _source_blocks_from_pdf(path)
        if blocks:
            return {
                "paper_id": paper_id,
                "source_kind": source_kind,
                "source_path": str(path),
                "blocks": blocks,
                "text_chars": sum(len(str(item.get("text") or "")) for item in blocks),
            }
    return {
        "paper_id": paper_id,
        "source_kind": "unavailable",
        "source_path": "",
        "blocks": [],
        "text_chars": 0,
    }


def source_query_terms(text: str) -> Counter[str]:
    terms = [
        match.group(0).casefold()
        for match in SOURCE_TOKEN_RE.finditer(text or "")
        if match.group(0).casefold() not in SOURCE_STOPWORDS
        and not match.group(0).isdigit()
    ]
    for run in SOURCE_CJK_RE.findall(text or ""):
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return Counter(terms)


def cross_language_query_phrases(text: str) -> set[str]:
    """Expand common chemistry concepts without depending on an online translator."""
    folded = clean_text(text).casefold()
    phrases: set[str] = set()
    for english, translations in CROSS_LANGUAGE_CHEMISTRY_TERMS:
        if english in folded:
            phrases.update(translations)
    return phrases


def source_phrase_present(phrase: str, folded_block: str) -> bool:
    folded_phrase = phrase.casefold()
    if re.fullmatch(r"[a-z]{1,3}", folded_phrase):
        return bool(
            re.search(
                rf"(?<![a-z]){re.escape(folded_phrase)}(?![a-z])",
                folded_block,
            )
        )
    return folded_phrase in folded_block


def source_query_variants(text: str) -> list[str]:
    whole = clean_text(text)
    claims = [
        clean_text(value)
        for sentence in prose_sentences(whole) for value in re.split(r"[;；]\s*", sentence)
        if clean_text(value)
    ]
    useful = [
        value
        for value in claims
        if len(source_query_terms(value)) >= 2 or cross_language_query_phrases(value)
    ]
    return list(dict.fromkeys(useful + ([whole] if whole else [])))


def score_source_block(
    query_text: str,
    block_text: str,
    protected_terms: set[str],
) -> float:
    query_terms = source_query_terms(query_text)
    block_terms = source_query_terms(block_text)
    folded = block_text.casefold()
    overlap = sum(min(count, block_terms.get(term, 0)) for term, count in query_terms.items())
    protected_hits = sum(1 for term in protected_terms if term and term in folded)
    coverage = overlap / max(1, sum(query_terms.values()))
    bilingual_phrases = cross_language_query_phrases(query_text)
    bilingual_hits = sum(
        min(3.0, 1.0 + len(phrase) / 4.0)
        for phrase in bilingual_phrases
        if source_phrase_present(phrase, folded)
    )
    return overlap + protected_hits * 4.0 + coverage * 10.0 + bilingual_hits * 3.0


def retrieve_original_passages(
    paper_id: str,
    paragraph_text: str,
    document: dict[str, Any],
    *, queries: list[str] | None = None,
) -> list[dict[str, Any]]:
    protected = protected_signature(' '.join(queries) if queries else paragraph_text)
    protected_terms = set(
        protected["chemical_identities"]
        + protected["soft_chemical_terms"]
        + protected["numbers"]
        + protected["stereo"]
        + protected["soft_stereo_terms"]
    )
    blocks = document.get("blocks") or []
    passage_limit = MAX_SOURCE_PASSAGES_PER_PAPER if queries else (
        MAX_SOURCE_PASSAGES_PER_PAPER
        if cross_language_query_phrases(paragraph_text)
        and any(SOURCE_CJK_RE.search(str(block.get("text") or "")) for block in blocks)
        else 2
    )
    variants = list(dict.fromkeys(queries or source_query_variants(paragraph_text)))[:4]
    best_by_block: dict[int, tuple[float, int, dict[str, Any]]] = {}
    claim_leaders: list[tuple[float, int, dict[str, Any]]] = []
    for variant in variants:
        variant_ranked: list[tuple[float, int, dict[str, Any]]] = []
        for index, block in enumerate(blocks):
            block_text = clean_text(block.get("text") or "")
            score = score_source_block(variant, block_text, protected_terms)
            if score <= 0:
                continue
            candidate = (score, index, block)
            variant_ranked.append(candidate)
            previous = best_by_block.get(index)
            if previous is None or score > previous[0]:
                best_by_block[index] = candidate
        if variant_ranked:
            variant_ranked.sort(key=lambda item: (-item[0], item[1]))
            claim_leaders.append(variant_ranked[0])
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    leader_indexes: set[int] = set()
    for candidate in claim_leaders:
        if candidate[1] not in leader_indexes:
            ranked.append(candidate)
            leader_indexes.add(candidate[1])
    ranked.extend(
        candidate
        for index, candidate in sorted(
            best_by_block.items(), key=lambda item: (-item[1][0], item[0])
        )
        if index not in leader_indexes
    )
    passages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, index, block in ranked:
        text = clean_text(block.get("text") or "")
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        page = block.get("page")
        page_label = f"p{page}" if page else "page-unknown"
        passages.append(
            {
                "ref": f"{paper_id}:{page_label}:b{index + 1}",
                "page": page,
                "retrieval_score": round(score, 3),
                "text": text,
            }
        )
        if len(passages) >= passage_limit:
            break
    return passages


def paragraph_paper_hint(
    review_root: Path,
    paragraph: dict[str, Any],
    rows: dict[str, dict[str, Any]],
) -> str:
    """Resolve figure/caption paragraphs before interpreting bracketed source labels."""
    if "<!-- inserted_figure:" not in str(paragraph.get("text") or ""):
        return ""
    match = PAPER_PARAGRAPH_ID_RE.match(str(paragraph.get("paragraph_id") or ""))
    if not match:
        return ""
    paper_id = match.group(1).upper()
    metadata_path = (
        review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    )
    return paper_id if paper_id in rows or metadata_path.is_file() else ""


def source_evidence(
    review_root: Path,
    project: Path,
    paragraph: dict[str, Any],
    structured: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    source_cache: dict[str, dict[str, Any]] | None = None,
    academic_contract: dict[str, Any] | None = None,
    *, queries: list[str] | None = None,
) -> dict[str, Any]:
    contract = academic_contract or {}
    navigation = (contract.get("section_navigation") or {}).get(str(paragraph.get("paragraph_id") or ""), {})
    planned_ids = (contract.get("paragraph_claim_ids") or {}).get(str(paragraph.get("paragraph_id") or "")) or []
    argument_plan = [{**argument_projection(claim), "required_for_section": claim.get("required_for_section", True),
                      **({"draft_revision": claim["draft_revision"]} if claim.get("draft_revision") else {})}
                     for claim_id in planned_ids if (claim := (contract.get("claims") or {}).get(claim_id, {})).get("argument_basis")]
    exact = claim_bound_evidence(
        str(paragraph.get("paragraph_id") or ""),
        structured,
        academic_contract or {},
    )
    # Source checks live in the existing Quality, not in a second evidence store.
    # Validate text and current source bytes before reusing a saved recovery.
    if not queries:
        saved = read_json(project / '04_first_draft' / 'original_source_check.json', {}) or read_json(
            project / '04_first_draft' / 'prior_quality_context.json', {}).get('source_check') or {}
        entry = next((e for e in saved.get('entries') or []
                      if e.get('paragraph_id') == paragraph['paragraph_id']), {})
        if (entry.get('paragraph_text_hash') == hashlib.sha256(str(paragraph.get('text') or '').encode()).hexdigest()
                and entry.get('targeted_source_recheck')):
            restored = evidence_from_source_check_report({'entries': [entry]}).get(paragraph['paragraph_id'])
            current_binding = paragraph_citation_binding(project, paragraph)
            valid = bool(restored and restored.get('evidence') and current_binding
                         and entry.get('citation_binding') == current_binding)
            for paper in (restored or {}).get('evidence') or []:
                pid = paper['paper_id']
                if pid not in current_binding.values():
                    valid = False
                    break
                doc = load_original_source(review_root, pid, metadata_record(review_root, pid))
                digest = hashlib.sha256(json.dumps(doc.get('blocks') or [], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                valid = valid and bool(doc.get('blocks')) and digest == paper.get('source_content_hash')
                # Job staging paths change even when the immutable source does not.
                paper['source_path'] = str(doc.get('source_path') or '')
                paper['local_source_available'] = bool(doc.get('blocks'))
            if valid:
                restored['local_source_available'] = True
                restored['argument_plan'] = argument_plan
                restored['section_context'] = navigation
                restored['paragraph_text_hash'] = entry['paragraph_text_hash']
                restored['targeted_source_recheck'] = entry['targeted_source_recheck']
                return restored
    if exact and not queries:
        paper_ids = list(exact)
        return {
            "paragraph_id": paragraph["paragraph_id"],
            "heading": paragraph.get("heading", ""),
            "paper_ids": paper_ids,
            "local_source_available": True,
            "original_source_ready": True,
            "evidence_scope": "claim_bound_indexed_evidence",
            "argument_plan": argument_plan,
            "section_context": navigation,
            "evidence": [
                {
                    "paper_id": paper_id,
                    "title": str((rows.get(paper_id) or {}).get("title") or ""),
                    "abstract": "",
                    "main_content": "",
                    "local_source_available": True,
                    "original_text_available": True,
                    "source_kind": "section_evidence_package",
                    "source_path": "02_section_drafting/section_evidence.json",
                    "source_text_chars": sum(
                        len(str(item.get("text") or ""))
                        for item in exact[paper_id]
                    ),
                    "original_passages": exact[paper_id],
                }
                for paper_id in paper_ids
            ],
        }
    paper_ids = [
        str(value)
        for value in (structured.get("cited_paper_ids") or [structured.get("paper_id")])
        if value
    ]
    if queries:
        cited = list(paragraph_citation_binding(project, paragraph).values())
        # Direct bindings first; fallback only to this paragraph's actual citations.
        paper_ids = list(dict.fromkeys([*(pid for pid in exact if pid in cited), *cited]))
    if not paper_ids and not queries:
        paper_hint = paragraph_paper_hint(review_root, paragraph, rows)
        if paper_hint:
            paper_ids = [paper_hint]
        else:
            by_callout = {
                int(entry.get("callout")): str(entry.get("paper_id") or "")
                for entry in citation_entries(project)
                if str(entry.get("callout") or "").isdigit() and entry.get("paper_id")
            }
            paper_ids = [
                by_callout[number]
                for number in sorted(expand_callouts(str(paragraph.get("text") or "")))
                if number in by_callout
            ]
    paper_ids = list(dict.fromkeys(paper_ids))
    evidence: list[dict[str, Any]] = []
    local_source_available = True
    original_source_ready = True
    cache = source_cache if source_cache is not None else {}
    for paper_id in paper_ids:
        row = rows.get(paper_id, {})
        metadata = metadata_record(review_root, paper_id)
        source_paths = metadata.get("source_paths") if isinstance(metadata, dict) else {}
        registered_paths = [
            resolve_source_path(review_root, value)
            for value in (source_paths or {}).values()
            if str(value or "").strip()
        ]
        registered_available = any(
            path is not None and (path.is_file() or path.is_dir())
            for path in registered_paths
        )
        document = cache.get(paper_id)
        if document is None:
            document = load_original_source(review_root, paper_id, metadata)
            cache[paper_id] = document
        passages = retrieve_original_passages(
            paper_id,
            str(paragraph.get("text") or ""),
            document,
            queries=queries,
        )
        available = bool(document.get("blocks"))
        local_source_available = local_source_available and registered_available
        original_source_ready = original_source_ready and bool(passages)
        evidence.append(
            {
                "paper_id": paper_id,
                "title": str(row.get("title") or metadata_value(metadata.get("title")) or ""),
                "abstract": clean_text(
                    row.get("abstract") or metadata_value(metadata.get("abstract"))
                )[:1200],
                "main_content": clean_text(row.get("main_content"))[:1600],
                "local_source_available": registered_available,
                "original_text_available": available,
                "source_kind": document.get("source_kind"),
                "source_path": document.get("source_path"),
                "source_text_chars": document.get("text_chars"),
                "source_content_hash": hashlib.sha256(json.dumps(document.get("blocks") or [], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "original_passages": passages,
            }
        )
    return {
        "paragraph_id": paragraph["paragraph_id"],
        "heading": paragraph.get("heading", ""),
        "paper_ids": paper_ids,
        "local_source_available": local_source_available if paper_ids else False,
        "original_source_ready": original_source_ready if paper_ids else False,
        "evidence_scope": (
            "retrieved_original_full_text"
            if paper_ids and original_source_ready
            else "original_full_text_without_relevant_passage"
            if paper_ids and local_source_available
            else "metadata_only"
        ),
        "evidence": evidence,
        "argument_plan": argument_plan,
        "section_context": navigation,
    }


def expand_callouts(text: str) -> set[int]:
    values: set[int] = set()
    for match in CALLOUT_RE.finditer(text or ""):
        for part in re.split(r"\s*,\s*", match.group(1)):
            if "-" in part:
                left, right = [item.strip() for item in part.split("-", 1)]
                if left.isdigit() and right.isdigit():
                    values.update(range(int(left), int(right) + 1))
            elif part.strip().isdigit():
                values.add(int(part.strip()))
    return values


def deterministic_preflight(
    review_root: Path,
    project_id: str,
    *,
    min_words: int,
    max_words: int,
) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_path = project / "04_first_draft" / "first_draft.md"
    if not draft_path.is_file():
        raise FileNotFoundError(draft_path)
    markdown = make_xml_compatible(draft_path.read_text(encoding="utf-8", errors="replace"))[0]
    paragraphs = parse_marked_paragraphs(markdown)
    _covered_markdown, marker_report = ensure_prose_paragraph_markers(markdown)
    structured = paragraph_metadata(project)
    rows = matrix_rows(project)
    findings: list[dict[str, Any]] = []
    paragraph_checks: list[dict[str, Any]] = []
    source_cache: dict[str, dict[str, Any]] = {}
    academic_contract = claim_evidence_contract(project)
    section_boundary_sentences: dict[str, set[str]] = {}
    for paragraph in paragraphs:
        paragraph_id = str(paragraph["paragraph_id"])
        text = clean_text(paragraph["text"])
        words = len(text.split())
        structured_paragraph = structured.get(paragraph_id, {})
        argument_role = paragraph_argument_role(structured_paragraph)
        word_range_applicable = argument_role == "anchor_case"
        evidence = source_evidence(
            review_root,
            project,
            paragraph,
            structured_paragraph,
            rows,
            source_cache,
            academic_contract,
        )
        issues: list[str] = []
        if word_range_applicable and (words < min_words or words > max_words):
            issues.append("P01")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P01",
                    "severity": "minor",
                    "diagnosis": f"Paragraph has {words} words; configured range is {min_words}-{max_words}.",
                    "route": "final_polish",
                }
            )
        if LABEL_SCAFFOLD_RE.search(text) or SCAFFOLD_RE.search(text):
            issues.append("P08")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P08",
                    "severity": "major",
                    "diagnosis": "Label-style or extraction-field scaffolding remains in the prose.",
                    "route": "section_rewrite",
                }
            )
        sentences = prose_sentences(text)
        normalized = [prose_comparison_key(value) for value in sentences]
        if any(value and value in normalized[:index] for index, value in enumerate(normalized)):
            issues.append("P03")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P03",
                    "severity": "major",
                    "diagnosis": "An identical sentence is repeated inside the paragraph.",
                    "route": "section_rewrite",
                }
            )
        section_id = paragraph_id.rsplit("-p", 1)[0]
        seen_boundary = section_boundary_sentences.setdefault(section_id, set())
        repeated_boundary = False
        for sentence, sentence_key in zip(sentences, normalized, strict=True):
            if not sentence_key or not EVIDENCE_BOUNDARY_RE.search(sentence):
                continue
            if sentence_key in seen_boundary:
                repeated_boundary = True
            seen_boundary.add(sentence_key)
        if repeated_boundary:
            issues.append("P09")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P09",
                    "severity": "major",
                    "diagnosis": (
                        "This section repeats the same evidence-boundary disclaimer; "
                        "retain at most one concise boundary statement."
                    ),
                    "route": "section_rewrite",
                }
            )
        voice_issues = publication_voice_issues(text)
        if voice_issues:
            issues.append("M05")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "M05",
                    "severity": "minor",
                    "diagnosis": (
                        "Internal workflow language remains in publication prose: "
                        + ", ".join(
                            dict.fromkeys(
                                str(item.get("phrase") or "")
                                for item in voice_issues
                            )
                        )[:300]
                    ),
                    "route": "final_polish",
                }
            )
        if evidence["paper_ids"] and not evidence["local_source_available"]:
            issues.append("C01")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "C01",
                    "severity": "major",
                    "diagnosis": "No readable local source is registered for at least one cited paper.",
                    "route": "local_source_recheck",
                }
            )
        paragraph_checks.append(
            {
                "paragraph_id": paragraph_id,
                "word_count": words,
                "paragraph_role": argument_role,
                "word_range_applicable": word_range_applicable,
                "issues": issues,
                "paper_ids": evidence["paper_ids"],
                "local_source_available": evidence["local_source_available"],
                "original_source_ready": evidence["original_source_ready"],
                "evidence_scope": evidence["evidence_scope"],
            }
        )

    body, references = split_body_references(markdown)
    cited = expand_callouts(body)
    listed = {int(value) for value in re.findall(r"(?m)^\s*\[(\d+)\]\s*\.?\s*\S", references)}
    mapped = {
        int(entry.get("callout"))
        for entry in citation_entries(project)
        if str(entry.get("callout") or "").isdigit()
    }
    image_paths = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)
    broken_images = [
        raw
        for raw in image_paths
        if not ((project / "04_first_draft" / raw).resolve()).is_file()
        and not re.match(r"^[a-z]+://", raw, re.I)
    ]
    hard: list[str] = []
    marker_coverage_complete = bool(
        not marker_report.get("changed")
        and paragraphs
        and len(paragraphs) == int(marker_report.get("prose_paragraph_count") or 0)
    )
    if not marker_coverage_complete:
        hard.append("paragraph_marker_coverage_mismatch")
    if not references.strip():
        hard.append("missing_references_section")
    if cited != listed or not cited.issubset(mapped):
        hard.append("citation_reference_map_mismatch")
    if broken_images:
        hard.append("broken_image_paths")
    for finding in findings:
        finding["hard_gate"] = paragraph_finding_is_blocking(finding)
    if any(finding["hard_gate"] for finding in findings):
        hard.append("paragraph_readability_or_source_failures")
    report = {
        "project_id": project_id,
        "draft_path": str(draft_path.resolve()),
        "draft_sha256": sha256_file(draft_path),
        "case_word_range": [min_words, max_words],
        "checks": {
            "paragraph_count": len(paragraphs),
            "citation_callouts": sorted(cited),
            "listed_references": sorted(listed),
            "citation_records": sorted(mapped),
            "image_count": len(image_paths),
            "broken_images": broken_images,
            "prose_paragraph_count": int(marker_report.get("prose_paragraph_count") or 0),
            "marker_count": int(marker_report.get("marker_count") or 0),
            "marker_coverage_complete": marker_coverage_complete,
        },
        "paragraph_checks": paragraph_checks,
        "paragraph_findings": findings,
        "hard_regressions": sorted(set(hard)),
        "hash_manifest_created": False,
    }
    write_json(project / "04_first_draft" / "first_draft_preflight.json", report)
    return report


def provider_config() -> dict[str, str]:
    return {
        "api_key": str(os.environ.get("REVIEW_WRITING_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip(),
        "base_url": str(os.environ.get("REVIEW_WRITING_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).strip().rstrip("/"),
        "model": str(os.environ.get("REVIEW_WRITING_MODEL") or DEFAULT_TEXT_MODEL).strip(),
        "wire_api": str(os.environ.get("REVIEW_WRITING_WIRE_API") or DEFAULT_TEXT_WIRE_API).strip().casefold().replace("_", "-"),
    }


def provider_endpoint(base_url: str, wire_api: str) -> str:
    wire = str(wire_api or "").casefold()
    route = "chat/completions" if wire in {"chat", "chat-completion", "chat-completions"} else "responses"
    return openai_endpoint(base_url, route)


def extract_json_object(text: str) -> dict[str, Any]:
    return parse_json_object_text(text, context="Feedback model")


def call_json_model(prompt: str, *, label: str) -> dict[str, Any]:
    gateway_url = str(os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "").strip()
    task_token = str(os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    if gateway_url or task_token:
        if not gateway_url or not task_token:
            raise RuntimeError("Feedback loop received an incomplete internal gateway configuration.")
        try:
            return gateway_call_json_model(prompt, label=label)
        except GatewayRequestError as exc:
            details = json.dumps(exc.details, ensure_ascii=False)
            if exc.details.get("category") == "context_limit" or request_body_budget_exhausted(details):
                raise ProviderRequestBodyBudgetExceeded(
                    f"{label} exceeded the provider relay request-body budget"
                ) from exc
            if exc.status_code in {504, 524}:
                raise ProviderDeadlineExceeded(
                    f"{label} exceeded the provider deadline (HTTP {exc.status_code})"
                ) from exc
            raise

    config = provider_config()
    if not config["api_key"]:
        raise RuntimeError("Feedback loop requires the server text provider to be configured.")
    wire = config["wire_api"]
    if wire in {"chat", "chat-completion", "chat-completions"}:
        endpoint = provider_endpoint(config["base_url"], wire)
        payload = {
            "model": config["model"],
            "messages": [{"role": "user", "content": prompt + "\nReturn only one valid JSON object."}],
        }
    else:
        endpoint = provider_endpoint(config["base_url"], wire)
        payload = {"model": config["model"], "input": [{"role": "user", "content": prompt}]}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
        },
    )
    request_attempts = provider_request_attempts()
    attempt = 0
    while attempt < request_attempts:
        attempt += 1
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=300) as response:
                raw = response.read()
            data = json.loads(raw.decode("utf-8"))
            if wire in {"chat", "chat-completion", "chat-completions"}:
                choices = data.get("choices") if isinstance(data, dict) else []
                message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
                text = message.get("content") if isinstance(message, dict) else ""
                if isinstance(text, list):
                    text = "\n".join(
                        str(item.get("text") or "") for item in text if isinstance(item, dict)
                    )
            else:
                text = data.get("output_text") if isinstance(data, dict) else ""
                if not text and isinstance(data, dict):
                    text = "\n".join(
                        str(content.get("text") or "")
                        for output in data.get("output") or []
                        if isinstance(output, dict)
                        for content in output.get("content") or []
                        if isinstance(content, dict)
                    )
            return extract_json_object(str(text or ""))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:600].replace("\n", " ")
            failure = normalize_provider_error(exc.code, body)
            if failure["category"] in {"quota_exhausted", "authentication"}:
                raise GatewayRequestError(provider_error_message(failure), status_code=exc.code,
                                          code=failure["code"], details=failure) from exc
            if failure["category"] == "context_limit":
                raise ProviderRequestBodyBudgetExceeded(
                    f"{label} exceeded the provider relay request-body budget"
                ) from exc
            if exc.code == 524:
                raise ProviderDeadlineExceeded(
                    f"{label} exceeded the provider proxy deadline (HTTP 524)"
                ) from exc
            concurrent = provider_concurrency_exhausted(body)
            if concurrent:
                request_attempts = MAX_PROVIDER_REQUEST_ATTEMPTS
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= request_attempts:
                raise RuntimeError(
                    f"{label} failed with HTTP {exc.code} after "
                    f"{attempt} provider attempts: {body or exc.reason}"
                ) from exc
            time.sleep(
                (
                    provider_concurrency_retry_delay
                    if concurrent
                    else provider_retry_delay
                )(
                    attempt,
                    str(exc.headers.get("Retry-After") or "") if exc.headers else "",
                )
            )
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt >= request_attempts:
                raise RuntimeError(
                    f"{label} transport/JSON failure after "
                    f"{attempt} provider attempts: {exc}"
                ) from exc
            time.sleep(provider_retry_delay(attempt))
    raise RuntimeError(
        f"{label} failed after {request_attempts} provider attempts"
    )


def rubric_dimensions(rubric: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions = rubric.get("dimensions") or []
    if not isinstance(dimensions, list) or not dimensions:
        raise RuntimeError("Unified rubric has no dimensions")
    if abs(sum(float(item.get("weight", 0)) for item in dimensions) - 100.0) > 0.001:
        raise RuntimeError("Unified rubric weights must total 100")
    return dimensions


def rubric_dimension_scope(definition: dict[str, Any]) -> str:
    """Return the evaluation scope for one rubric dimension."""

    explicit = clean_text(definition.get("scope")).lower()
    if explicit in {"global", "paragraph"}:
        return explicit
    dimension_id = str(definition.get("id") or "").upper()
    if dimension_id.startswith(("M", "S")) or dimension_id in {"G01", "G10", "G12"}:
        return "global"
    return "paragraph"


def scoped_rubric(rubric: dict[str, Any], scope: str) -> dict[str, Any]:
    return {
        **rubric,
        "dimensions": [
            item
            for item in rubric_dimensions(rubric)
            if rubric_dimension_scope(item) == scope
        ],
    }


def global_evaluation_prompt(
    rubric: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    preflight: dict[str, Any],
    goal: float,
    *,
    compact: bool = False,
    evidence: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Build one bounded whole-manuscript evaluation request."""

    total_budget = 24_000 if compact else 48_000
    per_paragraph = max(
        180,
        min(700 if not compact else 360, total_budget // max(1, len(paragraphs))),
    )
    evidence = evidence or {}
    overview = []
    for item in paragraphs:
        paragraph_id = str(item.get("paragraph_id") or "")
        bound = evidence.get(paragraph_id) or {}
        overview.append(
            {
                "paragraph_id": paragraph_id,
                "heading": clean_text(item.get("heading")),
                "paper_ids": list(bound.get("paper_ids") or []),
                "evidence_scope": str(bound.get("evidence_scope") or ""),
                "text_preview": clean_text(item.get("text"))[:per_paragraph],
            }
        )
    preflight_summary = {
        "checks": preflight.get("checks") or {},
        "hard_regressions": preflight.get("hard_regressions") or [],
    }
    return (
        "Act as a whole-manuscript scientific review evaluator. Do not rewrite text. "
        "Score only the supplied global rubric dimensions at levels 0-4. Judge "
        "organization, coverage, progression, synthesis, and workflow-state accuracy "
        "from the complete ordered draft overview. Do not infer paragraph-level fact "
        "errors from a truncated preview; those are checked separately against exact "
        "evidence. Preserve deterministic preflight findings; only hard_gate=true findings are immutable "
        "integrity failures. Quality findings inform scoring, not an automatic score cap. Return JSON with "
        "dimension_scores only. It must include every supplied rubric id exactly once, "
        "with id, level, and evidence. Keep evidence under 40 words.\n\n"
        f"Overall goal: {goal}.\n"
        f"Global rubric: {json.dumps(rubric, ensure_ascii=False)}\n"
        f"Deterministic preflight summary: {json.dumps(preflight_summary, ensure_ascii=False)}\n"
        f"Complete ordered draft overview: {json.dumps(overview, ensure_ascii=False)}"
    )


def compact_evidence_for_prompt(
    raw: dict[str, Any], *, minimal: bool = False,
) -> dict[str, Any]:
    """Project complete evidence for scoring and rewriting, deduplicating text.

    Source identity remains on each passage. Shared text is only a storage
    optimization, never permission to cite another paper or truncate a proof.
    """

    compact_papers: list[dict[str, Any]] = []
    passage_texts: dict[str, str] = {}
    text_ids: dict[str, str] = {}
    for paper in raw.get("evidence") or []:
        if not isinstance(paper, dict):
            continue
        passages: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for passage in paper.get("original_passages") or []:
            if not isinstance(passage, dict):
                continue
            text = clean_text(passage.get("text"))
            if not text:
                continue
            text_id = text_ids.setdefault(text, f"T{len(text_ids) + 1}")
            identity = (str(passage.get("ref") or ""),
                        str(passage.get("claim_id") or ""), text_id)
            if identity in seen:
                continue
            seen.add(identity)
            passage_texts[text_id] = text
            passages.append({
                **{key: passage[key] for key in (
                    "ref", "evidence_key", "chunk_id", "page", "page_end", "claim_id", "purpose",
                ) if passage.get(key) is not None},
                "text_ref": text_id,
            })
        compact_paper = {
            "paper_id": paper.get("paper_id"),
            "local_source_available": bool(paper.get("local_source_available")),
            "original_text_available": bool(paper.get("original_text_available")),
            "original_passages": passages,
        }
        if not minimal:
            compact_paper["title"] = clean_text(paper.get("title"))[:160]
        if not passages:
            compact_paper["metadata_fallback"] = clean_text(
                paper.get("abstract") or paper.get("main_content")
            )[:260 if minimal else 700]
        compact_papers.append(compact_paper)
    return {
        "paragraph_id": raw.get("paragraph_id"),
        "paper_ids": raw.get("paper_ids") or [],
        "local_source_available": bool(raw.get("local_source_available")),
        "original_source_ready": bool(raw.get("original_source_ready")),
        "evidence_scope": raw.get("evidence_scope"),
        "evidence": compact_papers,
        "passage_texts": passage_texts,
        "argument_plan": raw.get("argument_plan") or [],
        "section_context": raw.get("section_context") or {},
    }


def compact_rewrite_evidence_for_prompt(
    raw: dict[str, Any],
    *,
    minimal: bool = False,
) -> dict[str, Any]:
    """Reuse the scoring projection; minimal mode omits metadata, not evidence."""
    return compact_evidence_for_prompt(raw, minimal=minimal)


def compact_preflight_for_prompt(
    preflight: dict[str, Any],
    paragraph_ids: set[str],
) -> dict[str, Any]:
    """Send only global checks and preflight rows relevant to the current batch."""

    return {
        "case_word_range": preflight.get("case_word_range"),
        "checks": preflight.get("checks") or {},
        "hard_regressions": preflight.get("hard_regressions") or [],
        "paragraph_checks": [
            item
            for item in preflight.get("paragraph_checks") or []
            if isinstance(item, dict) and str(item.get("paragraph_id") or "") in paragraph_ids
        ],
        "paragraph_findings": [
            item
            for item in preflight.get("paragraph_findings") or []
            if isinstance(item, dict) and str(item.get("paragraph_id") or "") in paragraph_ids
        ],
    }


def evaluation_prompt(
    rubric: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    preflight: dict[str, Any],
    goal: float,
    paragraph_goal: float,
    *,
    batch_index: int = 1,
    batch_total: int = 1,
    draft_structure: list[dict[str, str]] | None = None,
    prior_quality_context: dict[str, Any] | None = None,
) -> str:
    paragraph_ids = {str(item["paragraph_id"]) for item in paragraphs}
    preflight_roles = {
        str(item.get("paragraph_id") or ""): str(
            item.get("paragraph_role") or "supporting"
        )
        for item in preflight.get("paragraph_checks") or []
        if isinstance(item, dict)
    }
    compact_paragraphs = [
        {
            "paragraph_id": item["paragraph_id"],
            "heading": item.get("heading", ""),
            "paragraph_role": preflight_roles.get(
                str(item["paragraph_id"]), "supporting"
            ),
            "text": clean_text(item["text"]),
            "source_evidence": compact_evidence_for_prompt(
                evidence.get(str(item["paragraph_id"]), {})
            ),
        }
        for item in paragraphs
    ]
    prior_quality_context = prior_quality_context or {}
    # Only completed edits are closure history. An unresolved earlier diagnosis
    # is not evidence and must not be presented to the model as an accepted fact.
    relevant_dispositions = [
        dict(item)
        for item in (prior_quality_context.get("claim_dispositions") or {}).values()
        if isinstance(item, dict)
        and str(item.get("paragraph_id") or "") in paragraph_ids
        and item.get("outcome") in {"narrowed", "removed"}
    ]
    relevant_rescue_outcomes = {
        str(paragraph_id): dict(value)
        for paragraph_id, value in (
            prior_quality_context.get("evidence_rescue_outcomes") or {}
        ).items()
        if str(paragraph_id) in paragraph_ids and isinstance(value, dict)
    }
    return (
        "Act as a detect-first scientific review evaluator. Do not rewrite text. "
        "Every finding must identify an observable defect in the current paragraph, its failed rubric dimension, "
        "and the exact current assertion or registered Claim ID when evidence is at issue. Vague wishes for more "
        "evidence or stronger discussion without an identifiable defect are optional advice. Partial support alone "
        "is not a failure. Do not decide automatic repair permissions or invent source-absence conclusions. "
        f"This is paragraph scoring batch {batch_index} of {batch_total}. Score the supplied paragraph-level rubric at levels 0-4 "
        "against the supplied batch and score every supplied marked paragraph on a 0-100 scale. "
        "Use the draft structure index to preserve whole-draft order and section context. Batch results will be "
        "combined deterministically, so do not refer to paragraphs that are absent from this batch. "
        "Preserve deterministic preflight findings, but distinguish hard_gate=true source-integrity failures from "
        "quality recommendations. Do not invent a hard failure or cap a score merely for target-length deviation. "
        "Do not penalize a paragraph merely for passive voice. "
        "Basic terminology definitions and logical distinctions need not be stated verbatim by each paper. "
        "This exception never covers experiment-specific numbers, catalyst roles, mechanisms or superiority claims. "
        "Review synthesis is valid when its attributed premises support the inference; different substrates alone "
        "do not prohibit a bounded comparison. Avoid demanding repeated uncertainty disclaimers. Length is an "
        "authoring target: recommend substantive expansion only when warranted, never padding or a blocking failure. "
        "Execute the supplied argument_plan: distinguish reported facts, author interpretation and review synthesis. "
        "When draft_revision is present, assess its proposed argument against the actual passages and original "
        "research purpose. Do not reimpose the superseded wording, but do flag evasion, fabricated support or "
        "a changed scientific question; an edited requirement is not itself evidence of correctness. "
        "A previously audited synthesis need not be quoted verbatim from one paper; check whether the current prose "
        "realizes the same supported inference without changing its premises or comparison basis. Report omitted "
        "required core arguments in missing_core_claim_ids, using only the supplied required Claim IDs. "
        "An omitted core argument is a substantive writing problem, even when the remaining sentences are accurate. "
        f"{REVIEW_COMPARISON_POLICY} "
        "A protected-fact conflict must route to local_source_recheck or human_confirmation, never automatic invention. "
        f"{SOURCE_ATTRIBUTION_POLICY} "
        f"{CONTRIBUTION_WRITING_POLICY} {SECTION_THREAD_POLICY} "
        "section_context contains the organizing thread and adjacent CURRENT draft paragraphs, not verified evidence. "
        "Check local continuity without treating neighbors as source support or scoring paragraphs outside this batch. "
        "Original-source checking is part of this evaluation. For each paragraph, compare its factual claims with the "
        f"retrieved original_passages. {REVIEW_EVIDENCE_POLICY} Return source_check_status "
        "(verified|partially_supported|unsupported|needs_human_review|contradicted|not_found_in_checked_scope|not_applicable), source_evidence_refs using only the "
        "provided passage refs, and unsupported_claims. Treat absence from retrieved excerpts as needs_human_review, not "
        "as contradiction. Use local_source_recheck only when original text is unavailable or the retrieved passages are "
        "insufficient; otherwise route wording corrections to section_rewrite or final_polish. "
        "Figure, Scheme, or Table placement/callout prose such as 'Figure 2 summarizes ...' is not a paper-level scientific "
        "claim and must not be listed in unsupported_claims; evaluate any separate scientific interpretation of that visual "
        "normally. A retrieval miss never proves that a publication did not report or establish something. Unless the supplied "
        "passage explicitly makes the negative statement, identify an over-strong negative as a wording problem that can be "
        "narrowed to the checked-source boundary, not as a contradiction or a mandatory human review. "
        "The configured case-paragraph word range applies only where deterministic preflight marks "
        "word_range_applicable=true. Supporting, transition, caption-adjacent, introduction, and synthesis prose must not "
        "fail P01 solely because it is shorter than a case paragraph. "
        f"Respect paragraph_role from the Writing Plan: {', '.join(REVIEW_SYNTHESIS_ROLES)} "
        "are synthesis roles, not single-study case paragraphs. "
        "Prior claim dispositions are accepted closure records. Do not reopen a narrowed or removed unsupported claim "
        "when that claim is absent from the current paragraph. If a different problem remains, identify its exact current "
        "claim instead of repeating the closed diagnosis. "
        "Return JSON with dimension_scores and paragraph_scores. dimension_scores must include every supplied rubric id exactly once "
        "with id, level, evidence. paragraph_scores must include every paragraph exactly once with paragraph_id, score, "
        "failed_dimensions, severity (none|minor|major|critical), diagnosis, route "
        "(pass|section_rewrite|local_source_recheck|final_polish|human_confirmation), source_check_status, "
        "source_evidence_refs, unsupported_claims, missing_core_claim_ids, and finding_category. "
        f"Use finding_category from {sorted(FINDING_CATEGORIES)} based on the defect itself; diagnosis language must not control routing. "
        "Keep each dimension evidence under 30 words, each diagnosis under "
        "60 words, and unsupported_claims to at most four concise items. Keep source_evidence_refs for actual claims; "
        "do not extract or promote per-paper fact cards. "
        "For an unambiguous factual typo with a unique correction directly stated in a supplied passage, "
        "also return source_corrections on that paragraph: [{before,after,source_ref,source_quote,unambiguous:true}]. "
        "before is an exact short span in current prose; source_quote is verbatim from the passage and contains after. "
        "Do not propose these for ambiguous parsing, missing subscripts without corroborating context, inference, "
        "conflicting experiments, citation changes or disputed interpretations. Corrections are reviewable candidates only. "
        "For wrong study/method attribution (prior work confused with this study), also return source_attribution_repair: "
        "{kind:'wrong_study_attribution',claim_span,correction,context:[{source_ref,quote},{source_ref,quote}]}. "
        "claim_span must occur exactly in the current paragraph. Quote verbatim local context identifying the original "
        "owner AND the transition or current study results; retain enough surrounding text to resolve attribution. "
        "correction is a concise instruction, not a new fact. Do not use this for genuine conflicting experiments, "
        "uncertain source identity or absent evidence. Keep the current erroneous assertion marked unsupported or "
        "contradicted until the rewritten candidate is checked. Use the same repair for each affected paragraph. "
        f"Overall goal: {goal}; paragraph goal: {paragraph_goal}.\n"
        f"Draft structure index: {json.dumps(draft_structure or [], ensure_ascii=False)}\n"
        f"Rubric: {json.dumps(rubric, ensure_ascii=False)}\n"
        f"Deterministic preflight: {json.dumps(compact_preflight_for_prompt(preflight, paragraph_ids), ensure_ascii=False)}\n"
        f"Prior accepted claim dispositions: {json.dumps(relevant_dispositions, ensure_ascii=False)}\n"
        "Bounded local evidence-rescue outcomes are trusted search-scope records. "
        "not_found_in_checked_scope means the requested relation was not located in the registered project sources; "
        "report that same status for the affected unchanged claim. It does not prove a universal negative. "
        "Only narrow or remove a detail already listed as unsupported. "
        f"Evidence-rescue outcomes: {json.dumps(relevant_rescue_outcomes, ensure_ascii=False)}\n"
        f"Paragraphs and evidence: {json.dumps(compact_paragraphs, ensure_ascii=False)}"
    )


def evaluation_batch_size() -> int:
    raw = str(os.environ.get("REVIEW_FEEDBACK_BATCH_SIZE") or "").strip()
    try:
        configured = int(raw) if raw else DEFAULT_EVALUATION_BATCH_SIZE
    except ValueError:
        configured = DEFAULT_EVALUATION_BATCH_SIZE
    return max(2, min(configured, 12))


def paragraph_batches(
    paragraphs: list[dict[str, Any]],
    *,
    batch_size: int | None = None,
) -> list[list[dict[str, Any]]]:
    size = batch_size or evaluation_batch_size()
    return [paragraphs[index : index + size] for index in range(0, len(paragraphs), size)]


def merge_batched_evaluations(
    rubric: dict[str, Any],
    batches: list[tuple[list[dict[str, Any]], dict[str, Any]]],
    *,
    global_dimension_scores: list[dict[str, Any]] | None = None,
    scoped: bool = False,
) -> dict[str, Any]:
    """Merge independently bounded model calls into one normalization input."""

    # A paragraph-only response uses the original dimension weights, whose
    # subtotal is intentionally below 100; the final full rubric stays strict.
    all_dimensions = list(rubric.get('dimensions') or []) if scoped else rubric_dimensions(rubric)
    expected_dimensions = [
        str(item["id"])
        for item in all_dimensions
        if global_dimension_scores is None or rubric_dimension_scope(item) == "paragraph"
    ]
    dimension_levels: dict[str, list[tuple[float, int]]] = {
        dimension_id: [] for dimension_id in expected_dimensions
    }
    dimension_evidence: dict[str, list[str]] = {
        dimension_id: [] for dimension_id in expected_dimensions
    }
    paragraph_scores: list[dict[str, Any]] = []
    seen_paragraphs: set[str] = set()
    for batch_number, (batch, raw) in enumerate(batches, 1):
        raw_dimensions = raw.get("dimension_scores") or []
        if not isinstance(raw_dimensions, list):
            raise RuntimeError(f"Feedback scoring batch {batch_number} has no dimension_scores list")
        dimension_ids = [
            str(item.get("id") or "")
            for item in raw_dimensions
            if isinstance(item, dict)
        ]
        if (
            len(dimension_ids) != len(expected_dimensions)
            or len(set(dimension_ids)) != len(dimension_ids)
            or set(dimension_ids) != set(expected_dimensions)
        ):
            raise RuntimeError(
                f"Feedback scoring batch {batch_number} must score every rubric dimension exactly once"
            )
        batch_weight = max(1, len(batch))
        for item in raw_dimensions:
            dimension_id = str(item.get("id") or "")
            level = max(0.0, min(4.0, float(item.get("level", 0))))
            dimension_levels[dimension_id].append((level, batch_weight))
            evidence_text = clean_text(item.get("evidence"))
            if evidence_text and evidence_text not in dimension_evidence[dimension_id]:
                dimension_evidence[dimension_id].append(evidence_text)

        expected_paragraphs = [str(item["paragraph_id"]) for item in batch]
        raw_scores = raw.get("paragraph_scores") or []
        if not isinstance(raw_scores, list):
            raise RuntimeError(f"Feedback scoring batch {batch_number} has no paragraph_scores list")
        raw_ids = [
            str(item.get("paragraph_id") or "")
            for item in raw_scores
            if isinstance(item, dict)
        ]
        if (
            len(raw_ids) != len(expected_paragraphs)
            or len(set(raw_ids)) != len(raw_ids)
            or set(raw_ids) != set(expected_paragraphs)
        ):
            raise RuntimeError(
                f"Feedback scoring batch {batch_number} must score every supplied paragraph exactly once"
            )
        overlap = seen_paragraphs.intersection(raw_ids)
        if overlap:
            raise RuntimeError(f"Feedback scoring batches duplicated paragraphs: {sorted(overlap)}")
        seen_paragraphs.update(raw_ids)
        paragraph_scores.extend(item for item in raw_scores if isinstance(item, dict))

    merged_by_id: dict[str, dict[str, Any]] = {}
    for dimension_id in expected_dimensions:
        levels = dimension_levels[dimension_id]
        if not levels:
            raise RuntimeError(f"Feedback scoring did not assess rubric dimension {dimension_id}")
        weighted_level = sum(level * weight for level, weight in levels) / sum(
            weight for _level, weight in levels
        )
        evidence_text = " | ".join(dimension_evidence[dimension_id][:3])[:900]
        merged_by_id[dimension_id] = {
            "id": dimension_id,
            "level": round(weighted_level, 3),
            "evidence": evidence_text,
        }
    if global_dimension_scores is not None:
        expected_global = {
            str(item["id"])
            for item in all_dimensions
            if rubric_dimension_scope(item) == "global"
        }
        supplied_global = [
            str(item.get("id") or "")
            for item in global_dimension_scores
            if isinstance(item, dict)
        ]
        if (
            len(supplied_global) != len(expected_global)
            or len(set(supplied_global)) != len(supplied_global)
            or set(supplied_global) != expected_global
        ):
            raise RuntimeError(
                "Whole-draft feedback must score every global rubric dimension exactly once"
            )
        for item in global_dimension_scores:
            dimension_id = str(item.get("id") or "")
            merged_by_id[dimension_id] = {
                "id": dimension_id,
                "level": max(0.0, min(4.0, float(item.get("level", 0)))),
                "evidence": clean_text(item.get("evidence")),
            }
    merged_dimensions = [
        merged_by_id[str(item["id"])]
        for item in all_dimensions
        if str(item["id"]) in merged_by_id
    ]
    return {
        "dimension_scores": merged_dimensions,
        "paragraph_scores": paragraph_scores,
        "dimension_score_basis": {
            "aggregation": "rubric_weights_and_batch_paragraph_counts",
            "paragraph_batches": [{"paragraph_ids": [p['paragraph_id'] for p in batch],
                                   "basis": raw.get('dimension_basis', 'assessed_batch')}
                                  for batch, raw in batches],
            "global": "current_draft" if global_dimension_scores is not None else "batch_assessed",
        },
    }


def normalize_evaluation(
    raw: dict[str, Any],
    rubric: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    preflight: dict[str, Any],
    goal: float,
    paragraph_goal: float,
    evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence = evidence or {}
    dimensions = rubric_dimensions(rubric)
    expected_ids = [str(item["id"]) for item in dimensions]
    raw_dimensions = raw.get("dimension_scores") or []
    if not isinstance(raw_dimensions, list):
        raise RuntimeError("Feedback rubric dimension_scores must be a list")
    raw_dimension_ids = [
        str(item.get("id"))
        for item in raw_dimensions
        if isinstance(item, dict) and item.get("id")
    ]
    by_id = {
        str(item.get("id")): item
        for item in raw_dimensions
        if isinstance(item, dict) and item.get("id")
    }
    if len(raw_dimension_ids) != len(expected_ids) or len(set(raw_dimension_ids)) != len(raw_dimension_ids) or set(by_id) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(by_id))
        extra = sorted(set(by_id) - set(expected_ids))
        duplicates = sorted({value for value in raw_dimension_ids if raw_dimension_ids.count(value) > 1})
        raise RuntimeError(
            "Feedback rubric response must score every dimension exactly once; "
            f"missing={missing}, extra={extra}, duplicates={duplicates}"
        )
    normalized_dimensions: list[dict[str, Any]] = []
    total = 0.0
    for definition in dimensions:
        item = by_id[str(definition["id"])]
        level = max(0.0, min(4.0, float(item.get("level", 0))))
        weight = float(definition.get("weight", 0))
        weighted = weight * level / 4.0
        total += weighted
        normalized_dimensions.append(
            {
                "id": definition["id"],
                "weight": weight,
                "level": level,
                "weighted": round(weighted, 3),
                "evidence": clean_text(item.get("evidence")),
            }
        )
    paragraph_ids = [str(item["paragraph_id"]) for item in paragraphs]
    if not paragraph_ids:
        raise RuntimeError("Feedback evaluation cannot release a draft with no marked prose paragraphs")
    if len(set(paragraph_ids)) != len(paragraph_ids):
        raise RuntimeError("The draft contains duplicate paragraph_id markers")
    raw_scores = raw.get("paragraph_scores") or []
    if not isinstance(raw_scores, list):
        raise RuntimeError("Feedback rubric paragraph_scores must be a list")
    raw_paragraph_ids = [
        str(item.get("paragraph_id"))
        for item in raw_scores
        if isinstance(item, dict) and item.get("paragraph_id")
    ]
    score_by_id = {
        str(item.get("paragraph_id")): item
        for item in raw_scores
        if isinstance(item, dict) and item.get("paragraph_id")
    }
    missing_paragraphs = sorted(set(paragraph_ids) - set(score_by_id))
    extra_paragraphs = sorted(set(score_by_id) - set(paragraph_ids))
    duplicate_paragraphs = sorted(
        {value for value in raw_paragraph_ids if raw_paragraph_ids.count(value) > 1}
    )
    if (
        missing_paragraphs
        or extra_paragraphs
        or duplicate_paragraphs
        or len(raw_paragraph_ids) != len(paragraph_ids)
    ):
        raise RuntimeError(
            "Feedback response must score every paragraph exactly once; "
            f"missing={missing_paragraphs}, extra={extra_paragraphs}, duplicates={duplicate_paragraphs}"
        )
    preflight_by_id: dict[str, list[dict[str, Any]]] = {}
    for finding in preflight.get("paragraph_findings") or []:
        preflight_by_id.setdefault(str(finding.get("paragraph_id") or ""), []).append(finding)
    paragraph_scores: list[dict[str, Any]] = []
    paragraph_failures: list[dict[str, Any]] = []
    paragraph_by_id = {
        str(paragraph.get("paragraph_id") or ""): paragraph
        for paragraph in paragraphs
        if isinstance(paragraph, dict)
    }
    paragraph_roles = {
        str(check.get("paragraph_id") or ""): str(check.get("paragraph_role") or "")
        for check in preflight.get("paragraph_checks") or [] if isinstance(check, dict)
    }
    for paragraph_id in paragraph_ids:
        item = score_by_id[paragraph_id]
        score = max(0.0, min(100.0, float(item.get("score", 0))))
        binding = preflight_by_id.get(paragraph_id, [])
        hard_binding = [finding for finding in binding if paragraph_finding_is_blocking(finding)]
        if hard_binding:
            score = min(score, 79.0)
        severity_order = {"none": 0, "minor": 1, "major": 2, "critical": 3}
        binding_severity = max(
            (str(finding.get("severity") or "none").casefold() for finding in binding),
            key=lambda value: severity_order.get(value, 0), default="none",
        )
        severity = str(item.get("severity") or binding_severity).casefold()
        if severity not in {"none", "minor", "major", "critical"}:
            severity = "major" if binding or score < paragraph_goal else "none"
        if severity_order.get(binding_severity, 0) > severity_order[severity]:
            severity = binding_severity
        route = str(item.get("route") or (binding[0].get("route") if binding else "pass"))
        allowed_routes = {
            "pass",
            "section_rewrite",
            "local_source_recheck",
            "final_polish",
            "human_confirmation",
        }
        if route not in allowed_routes:
            route = "section_rewrite" if score < paragraph_goal else "pass"
        if binding and route == "pass" and severity == "minor":
            route = "final_polish"
        if score < paragraph_goal and route in {"pass", "final_polish"}:
            route = "section_rewrite"
            if severity == "none":
                severity = "major"
        if route == "final_polish" and severity == "critical":
            route = "human_confirmation"
        elif route == "final_polish" and severity == "major":
            route = "section_rewrite"
        failed = [str(value) for value in item.get("failed_dimensions") or []]
        for finding in binding:
            for value in str(finding.get("rule") or "").split("/"):
                if value and value not in failed:
                    failed.append(value)
        if failed and route == "pass":
            route = "section_rewrite"
        source_check_status = str(item.get("source_check_status") or "not_assessed").casefold()
        if source_check_status not in {
            "verified",
            "partially_supported",
            "unsupported",
            "needs_human_review",
            "contradicted",
            "not_found_in_checked_scope",
            "not_applicable",
            "not_assessed",
        }:
            source_check_status = "not_assessed"
        source_evidence_refs = [
            clean_text(value)
            for value in item.get("source_evidence_refs") or []
            if clean_text(value)
        ][:12]
        raw_unsupported_claims = [
            clean_text(value)
            for value in item.get("unsupported_claims") or []
            if clean_text(value)
        ][:12]
        excluded_figure_callouts = [
            value for value in raw_unsupported_claims if is_figure_callout_only(value)
        ]
        unsupported_claims = [
            value for value in raw_unsupported_claims if not is_figure_callout_only(value)
        ]
        paragraph_evidence = evidence.get(paragraph_id, {})
        paper_ids = [str(value) for value in paragraph_evidence.get("paper_ids") or [] if value]
        valid_source_refs = {
            str(passage.get("ref") or "")
            for paper in paragraph_evidence.get("evidence") or []
            if isinstance(paper, dict)
            for passage in paper.get("original_passages") or []
            if isinstance(passage, dict) and passage.get("ref")
        }
        source_evidence_refs = [value for value in source_evidence_refs if value in valid_source_refs]
        source_ready = bool(paragraph_evidence.get("original_source_ready"))
        evidence_texts = [
            clean_text(passage.get("text"))
            for paper in paragraph_evidence.get("evidence") or []
            if isinstance(paper, dict)
            for passage in paper.get("original_passages") or []
            if isinstance(passage, dict) and clean_text(passage.get("text"))
        ]
        negative_policies = {
            claim: negative_claim_policy(claim, evidence_texts=evidence_texts)
            for claim in unsupported_claims
            if is_strong_negative_claim(claim)
        }
        scope_limited_negatives = [
            claim
            for claim, policy in negative_policies.items()
            if policy == "scope_limited_rewrite"
        ]
        figure_only_source_issue = bool(raw_unsupported_claims) and not unsupported_claims
        negative_scope_only = bool(unsupported_claims) and len(scope_limited_negatives) == len(
            unsupported_claims
        )
        if not paper_ids:
            source_check_status = "not_applicable"
        elif figure_only_source_issue and source_check_status in {
            "partially_supported",
            "unsupported",
            "needs_human_review",
            "not_assessed",
        }:
            # Figure placement/callout integrity is checked by the Figures
            # stage. Do not manufacture a paper-source failure for that prose.
            source_check_status = "not_applicable"
            if route in {"local_source_recheck", "human_confirmation"}:
                route = "section_rewrite" if score < paragraph_goal else "pass"
            if route == "pass":
                severity = "none"
        elif negative_scope_only:
            # A no-hit negative is repairable by narrowing the wording to the
            # actually checked source boundary. It is not a contradiction and
            # does not require the user to prove an absolute absence.
            source_check_status = "partially_supported"
            route = "section_rewrite"
            severity = "major"
            score = min(score, 79.0)
        elif source_ready and (
            source_check_status in {"not_assessed", "verified", "partially_supported", "unsupported"}
            and not source_evidence_refs
        ):
            source_check_status = "needs_human_review"
        if source_check_status == "needs_human_review":
            route = "local_source_recheck"
            severity = "major"
            score = min(score, 79.0)
        elif source_check_status == "contradicted":
            route = "human_confirmation"
            severity = "major"
            score = min(score, 79.0)
        elif source_check_status == "not_found_in_checked_scope":
            if unsupported_claims:
                route = "section_rewrite"
            else:
                route = "local_source_recheck"
            severity = "major"
            score = min(score, 79.0)
        elif source_check_status == "unsupported":
            route = "section_rewrite"
            severity = "major"
            score = min(score, 79.0)
        elif source_check_status == "partially_supported" and route == "pass" and (
            unsupported_claims or binding
        ):
            route = "section_rewrite"
            severity = "major"
            score = min(score, 79.0)
        problem_type = evidence_problem_type(
            unsupported_claims=unsupported_claims,
            source_check_status=source_check_status,
            source_evidence_refs=source_evidence_refs,
            source_ready=source_ready,
            evidence_texts=evidence_texts,
        )
        if (route == "pass" and severity == "none" and not unsupported_claims and not failed
                and source_check_status == "partially_supported"
                and source_ready and source_evidence_refs and not binding):
            problem_type = "none"
        claim_fact_bindings = validated_claim_fact_bindings(
            item.get("claim_fact_bindings") or [],
            paragraph_text=str(
                paragraph_by_id.get(paragraph_id, {}).get("text") or ""
            ),
            paragraph_evidence=paragraph_evidence,
        )
        required_ids = {claim.get("claim_id") for claim in paragraph_evidence.get("argument_plan") or []
                        if claim.get("required_for_section")}
        missing_core = [value for value in item.get("missing_core_claim_ids") or [] if isinstance(value, str) and value in required_ids]
        if missing_core:
            severity = "major"
            if route in {"pass", "final_polish"}:
                route = "section_rewrite"
        record = {
            "source_attribution_repair": validated_attribution_repair(
                str(paragraph_by_id.get(paragraph_id, {}).get('text') or ''),
                item.get('source_attribution_repair'), paragraph_evidence),
            "source_corrections": verified_corrections(str(paragraph_by_id.get(paragraph_id, {}).get('text') or ''),
                                                      item.get('source_corrections'), paragraph_evidence),
            "paragraph_id": paragraph_id,
            "score": round(score, 2),
            "missing_core_claim_ids": list(dict.fromkeys(missing_core)),
            "failed_dimensions": failed,
            "severity": severity,
            "diagnosis": clean_text(item.get("diagnosis") or "; ".join(str(f.get("diagnosis")) for f in binding)),
            "route": route,
            "source_check_status": source_check_status,
            "source_evidence_refs": source_evidence_refs,
            "unsupported_claims": unsupported_claims,
            "excluded_figure_callouts": excluded_figure_callouts,
            "negative_claim_policies": negative_policies,
            "evidence_problem_type": problem_type,
            "claim_fact_bindings": claim_fact_bindings,
            "failed_coverage_fields": [
                clean_text(value)
                for value in item.get("failed_coverage_fields") or []
                if clean_text(value)
            ],
        }
        record["finding_category"] = finding_category({**record, "finding_category": item.get("finding_category")})
        record.update(paragraph_repair_contract(record, paragraph_evidence))
        paragraph_scores.append(record)
        if route != "pass" or severity in {"critical", "major"}:
            paragraph_failures.append(record)
    hard = sorted(set(preflight.get("hard_regressions") or []))
    if any(paragraph_finding_is_blocking(item) for item in preflight.get("paragraph_findings") or []):
        hard = sorted(set(hard) | {"paragraph_readability_or_source_failures"})
    blocking_paragraph_failures = [
        item
        for item in paragraph_scores
        if paragraph_finding_is_blocking(item)
    ]
    decision = (
        "PASS"
        if not hard and not blocking_paragraph_failures
        else "REGENERATE_SECTIONS"
    )
    return {
        "rubric_model": str(rubric.get("name") or "readability_first_unified_review_rubric"),
        "pass_threshold": goal,
        "paragraph_pass_threshold": paragraph_goal,
        "total_score": round(total, 2),
        "decision": decision,
        "dimension_scores": normalized_dimensions,
        "dimension_score_basis": raw.get('dimension_score_basis') or {},
        "hard_gate_failures": hard,
        "paragraph_scores": paragraph_scores,
        "paragraph_failures": paragraph_failures,
        "blocking_paragraph_failures": blocking_paragraph_failures,
    }


def chemical_identity_tokens(text: str) -> list[str]:
    """Extract explicit identities/formulas that a wording edit must retain."""

    protected: list[str] = []
    for match in CHEMICAL_WORD_RE.finditer(text or ""):
        token = match.group(0).strip(".,;:")
        if repeated_run_junk_token(token):
            continue
        folded = token.casefold()
        uppercase_count = sum(1 for character in token if character.isupper())
        formula_like = bool(
            (any(character.isdigit() for character in token) and any(character.isalpha() for character in token))
            or uppercase_count >= 2
            or re.search(r"[A-Z][a-z]?\([IVX]+\)", token)
            or token in EXPLICIT_CHEMICAL_SYMBOLS
        )
        named_chemical = folded in CHEMICAL_ELEMENTS_AND_METALS
        if formula_like or named_chemical:
            protected.append(folded)
    return sorted(set(protected))


def soft_chemical_terms(text: str) -> list[str]:
    """Normalize generic chemistry classes whose grammar may safely change."""

    terms: set[str] = set()
    for match in CHEMICAL_WORD_RE.finditer(text or ""):
        folded = match.group(0).strip(".,;:").casefold()
        for suffix in CHEMICAL_SUFFIXES:
            if folded.endswith(suffix + "s"):
                terms.add(folded[:-1])
                break
            if folded.endswith(suffix):
                terms.add(folded)
                break
    return sorted(terms)


def protection_prose(text: str) -> str:
    """Exclude already hard-protected figure structures from prose signatures."""

    without_metadata = INSERTED_FIGURE_RE.sub(" ", text or "")
    return MARKDOWN_IMAGE_RE.sub(" ", without_metadata)


def protected_signature(text: str, *, exclude_citation_numbers: bool = False) -> dict[str, list[str]]:
    prose = protection_prose(text)
    numeric_prose = CALLOUT_RE.sub("", prose) if exclude_citation_numbers else prose
    return {
        # Citation order is binding; [1] and [2] may not trade places.
        "callouts": [match.group(0) for match in CALLOUT_RE.finditer(prose)],
        "numbers": [match.group(0).casefold() for match in PROTECTED_NUMBER_RE.finditer(numeric_prose)],
        "stereo": sorted(set(match.group(0).casefold() for match in STEREO_RE.finditer(prose))),
        "chemical_identities": chemical_identity_tokens(prose),
        "required_labels": sorted(
            set(
                clean_text(match.group(0)).casefold()
                for match in REQUIRED_LABEL_RE.finditer(prose)
            )
        ),
        "soft_chemical_terms": soft_chemical_terms(prose),
        "soft_stereo_terms": sorted(
            set(match.group(0).casefold() for match in SOFT_STEREO_RE.finditer(prose))
        ),
        # Figures are manuscript structure, not prose. A text rewrite may not
        # remove, replace, reorder, or repoint either the image or its anchor.
        "images": [match.group(0) for match in MARKDOWN_IMAGE_RE.finditer(text or "")],
        "figure_metadata": [
            clean_text(match.group(0))
            for match in INSERTED_FIGURE_RE.finditer(text or "")
        ],
    }


def rewrite_prompt(
    paragraph: dict[str, Any],
    score: dict[str, Any],
    evidence: dict[str, Any],
    min_words: int,
    max_words: int,
    *,
    word_range_applicable: bool = True,
    rewrite_mode: str = "section_rewrite",
    minimal_evidence: bool = False,
) -> str:
    length_instruction = (
        f"Target word range for this case paragraph: {min_words}-{max_words}. "
        "Prefer complete, supported prose; do not pad with disclaimers or invent details to reach the target. "
        "Length deviation is a quality recommendation, not an integrity failure."
        if word_range_applicable
        else (
            f"This is supporting prose, not a case paragraph. Keep it concise and no longer than {max_words} words; "
            "do not pad it to the case-paragraph minimum."
        )
    )
    mode_instruction = {
        "negative_claim_scope_narrowing": (
            "The listed unsupported claim is an over-strong paper-level negative. Do not state that the publication did "
            "not report, establish, define, or discuss the point unless the supplied passage explicitly says so. Recast it "
            "as a bounded statement such as 'the checked source passages do not directly establish ...' or omit it when it "
            "does not advance the argument. Preserve all supported positive facts and citations."
        ),
        "source_recheck_cleanup": (
            "This paragraph is in original-source recheck. Retain claims supported by the supplied passages and remove "
            "or explicitly qualify only the listed unsupported_claims. Absence from a passage is not evidence that a "
            "claim is false. Do not describe the paragraph as source-verified. Narrow the scientific statement itself; "
            "do not add a generic evidence disclaimer or repeat stock phrases such as 'does not establish'."
        ),
        "review_synthesis_cleanup": (
            "This is uncited review-synthesis prose. Remove unsupported specifics or recast them explicitly as a bounded "
            "review-level comparison. Do not invent a citation or imply that an unlinked primary source was checked."
        ),
        "human_review_style_only": (
            "This issue still requires manual source or figure-identity confirmation. Do not resolve, conceal, downgrade, "
            "or claim to have verified that issue. Preserve every scientific proposition, source relationship, citation, "
            "number, condition, chemical identity, and figure reference. Improve only grammar, sentence structure, and "
            "transitions that can be changed without altering the factual meaning. The returned candidate remains subject "
            "to manual confirmation."
        ),
        "final_polish": (
            "Improve grammar, concision, and transitions only. Preserve the complete scientific meaning and all evidence "
            "boundaries; do not add, remove, or upgrade a scientific claim."
        ),
    }.get(rewrite_mode, "Apply the requested section-level readability correction.")
    return (
        "Rewrite exactly one scientific-review paragraph for readability and argument flow. Preserve every citation callout, "
        "keeping each citation immediately next to the condition, result, mechanism, or conclusion it supports. "
        "Do not move source-specific citations to the paragraph end or combine different systems under one citation group. "
        "Sort numbers inside each citation group in ascending order. Preserve every "
        "number, condition, metric type, explicit chemical identity/formula, catalyst/reagent role, stereochemical value, "
        "and evidence "
        "boundary. Preserve every Markdown image and inserted_figure metadata comment exactly, including its path and "
        "order. Do not add facts, citations, mechanisms, yields, selectivities, or compounds. Use only the original text "
        "and supplied local evidence. Generic chemistry class terms may change number or phrasing for grammar, but must "
        "not introduce a new scientific claim. Remove obvious leading or trailing OCR/test junk such as long repeated-letter "
        "tokens when the diagnosis identifies junk, garbled, or noisy text; such junk is not a chemical identity. "
        "Return JSON {\"text\": \"...\"}.\n\n"
        f"{length_instruction}\n"
        f"Rewrite mode: {rewrite_mode}. {mode_instruction}\n"
        f"{attribution_repair_instruction(score, evidence, rewrite_mode)}\n"
        f"{REVIEW_COMPARISON_POLICY} {REVIEW_EVIDENCE_POLICY}\n"
        f"{CONTRIBUTION_WRITING_POLICY} {SECTION_THREAD_POLICY}\n"
        "Use section_context to preserve the local thread and adjacency, not to add unverified facts. "
        "Change only this paragraph; never copy a neighbor or change paragraph IDs, image bindings or citations.\n"
        f"Paragraph id: {paragraph['paragraph_id']}\n"
        f"Diagnosis: {json.dumps(score, ensure_ascii=False)}\n"
        "Local evidence: "
        f"{json.dumps(compact_rewrite_evidence_for_prompt(evidence, minimal=minimal_evidence), ensure_ascii=False)}\n"
        f"Original paragraph: {paragraph['text']}"
    )


def rewrite_repair_prompt(
    original: str,
    rejected_candidate: str,
    validation_errors: list[str],
    min_words: int,
    max_words: int,
    *,
    word_range_applicable: bool,
    allowed_unsupported_claims: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    score: dict[str, Any] | None = None,
    rewrite_mode: str = "section_rewrite",
    repair_attempt: int = 2,
) -> str:
    """Repair a candidate with the same evidence and strict integrity checks."""
    protected = protected_signature(original)
    hard_protected = {
        key: value for key, value in protected.items() if key in HARD_PROTECTED_FIELDS
    }
    soft_protected = {
        key: value for key, value in protected.items() if key in SOFT_PROTECTED_FIELDS
    }
    candidate_word_count = len(clean_text(rejected_candidate).split())
    length_instruction = (
        f"Target {min_words}-{max_words} words; the rejected candidate has {candidate_word_count} words. "
        "Prefer a shorter supported paragraph to padding, repeated disclaimers or invented detail."
        if word_range_applicable
        else f"Keep the corrected supporting paragraph concise and at or below {max_words} words."
    )
    allowed_removals = protected_signature(" ".join(allowed_unsupported_claims or []))
    protection_instruction = (
        "Hard-protected values may only be deleted when the same value occurs in the listed unsupported claims; values "
        "must never be added, replaced, or reassigned. Citation callouts, numerical facts, images, and figure metadata "
        "must remain exactly unchanged."
        if allowed_unsupported_claims
        else (
            "The hard-protected signature must retain the same facts. Citation callouts, numerical facts, images, and "
            "figure metadata must keep their exact multiplicity and order."
        )
    )
    factual_repair = attribution_repair_instruction(score or {}, evidence or {}, rewrite_mode)
    if factual_repair:
        protection_instruction = (
            "Keep the hard-protected signature except for existing permitted removals of listed unsupported "
            "values. Correct only the diagnosed relationships using the supplied source context. "
            "Citation callouts, images and figure metadata must retain their multiplicity and order."
        )
    return (
        "Repair one rejected scientific-review rewrite. Return JSON {\"text\": \"...\"} only. "
        f"{REVIEW_EVIDENCE_POLICY} "
        "Do not add facts or use chemical identities from supplied evidence unless they already occur in the original. "
        f"{protection_instruction} {factual_repair} "
        f"{length_instruction}\n"
        f"Generation attempt: {repair_attempt}. Rewrite mode: {rewrite_mode}.\n"
        f"Validation errors to fix: {json.dumps(validation_errors, ensure_ascii=False)}\n"
        f"Diagnosis: {json.dumps(score or {}, ensure_ascii=False)}\n"
        "Local evidence (use it only to ground or clarify statements already permitted by the original paragraph): "
        f"{json.dumps(compact_rewrite_evidence_for_prompt(evidence or {}, minimal=True), ensure_ascii=False)}\n"
        f"Hard-protected signature: {json.dumps(hard_protected, ensure_ascii=False)}\n"
        f"Soft terminology (may be grammatically rephrased, never used to add a claim): "
        f"{json.dumps(soft_protected, ensure_ascii=False)}\n"
        f"Allowed protected-value removals: {json.dumps(allowed_removals, ensure_ascii=False)}\n"
        f"Unsupported claims: {json.dumps(allowed_unsupported_claims or [], ensure_ascii=False)}\n"
        f"Original paragraph: {original}\n"
        f"Rejected candidate: {rejected_candidate}"
    )


def protected_change_allows_only_listed_removals(
    before: list[str],
    after: list[str],
    allowed_removals: list[str],
) -> bool:
    after_index = 0
    removed: list[str] = []
    for value in before:
        if after_index < len(after) and value == after[after_index]:
            after_index += 1
        else:
            removed.append(value)
    return after_index == len(after) and not (Counter(removed) - Counter(allowed_removals))


def protected_set_change_allows_only_listed_removals(
    before: list[str],
    after: list[str],
    allowed_removals: list[str],
) -> bool:
    before_set, after_set = set(before), set(after)
    added = after_set - before_set
    removed = before_set - after_set
    return not added and removed.issubset(set(allowed_removals))


def validate_rewrite_report(
    original: str,
    candidate: str,
    min_words: int,
    max_words: int,
    *,
    allowed_unsupported_claims: list[str] | None = None,
    source_corrections: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """Return blocking integrity errors and non-blocking terminology warnings."""

    errors: list[str] = []
    warnings: list[str] = []
    cleaned = clean_text(candidate)
    if not cleaned:
        return ["empty_rewrite"], warnings
    prose_blocks = [
        value.strip()
        for value in re.split(r"\n\s*\n", str(candidate or "").strip())
        if clean_text(value)
    ]
    if len(prose_blocks) != 1:
        errors.append("multiple_prose_blocks")
    if PARAGRAPH_MARKER_RE.search(str(candidate or "")):
        errors.append("paragraph_marker_in_rewrite")
    words = len(cleaned.split())
    if words < min_words or words > max_words:
        warnings.append(f"word_count_{words}_outside_{min_words}_{max_words}")
    before, after = protected_signature(corrected_baseline(original, source_corrections)), protected_signature(candidate)
    allowed = protected_signature(" ".join(allowed_unsupported_claims or []))
    exact_sequence_fields = {"callouts", "numbers", "images", "figure_metadata"}
    set_fields = {"stereo", "chemical_identities", "required_labels"}
    never_removable = {"callouts", "images", "figure_metadata"}
    for key in HARD_PROTECTED_FIELDS:
        unchanged = before[key] == after[key]
        removal_allowed = False
        if allowed_unsupported_claims and key not in never_removable:
            if key in exact_sequence_fields:
                removal_allowed = protected_change_allows_only_listed_removals(
                    before[key], after[key], allowed[key]
                )
            elif key in set_fields:
                removal_allowed = protected_set_change_allows_only_listed_removals(
                    before[key], after[key], allowed[key]
                )
        if not unchanged and not removal_allowed:
            errors.append(f"protected_{key}_changed")
    for key in SOFT_PROTECTED_FIELDS:
        if set(before[key]) != set(after[key]):
            warnings.append(f"{key}_changed")
    if LABEL_SCAFFOLD_RE.search(cleaned) or SCAFFOLD_RE.search(cleaned):
        errors.append("scaffolding_remains")
    if edge_junk_tokens(cleaned):
        errors.append("edge_junk_text_remains")
    return errors, warnings


def validate_rewrite(
    original: str,
    candidate: str,
    min_words: int,
    max_words: int,
    *,
    allowed_unsupported_claims: list[str] | None = None,
) -> list[str]:
    errors, _warnings = validate_rewrite_report(
        original,
        candidate,
        min_words,
        max_words,
        allowed_unsupported_claims=allowed_unsupported_claims,
    )
    return errors


def replace_paragraph_in_markdown(markdown: str, paragraph_id: str, replacement: str) -> str:
    body, references = split_body_references(markdown)
    paragraph = next(
        (item for item in parse_marked_paragraphs(markdown) if item["paragraph_id"] == paragraph_id),
        None,
    )
    if not paragraph:
        raise RuntimeError(f"Paragraph marker disappeared: {paragraph_id}")
    updated = body[: paragraph["start"]] + replacement.strip() + body[paragraph["end"] :]
    return updated + references


def _paragraph_score_map(evaluation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("paragraph_id") or ""): dict(item)
        for item in evaluation.get("paragraph_scores") or []
        if isinstance(item, dict) and str(item.get("paragraph_id") or "")
    }


def _paragraph_preflight(
    preflight: dict[str, Any], paragraph_id: str
) -> dict[str, Any]:
    return {
        "case_word_range": preflight.get("case_word_range"),
        "checks": preflight.get("checks") or {},
        "hard_regressions": [],
        "paragraph_checks": [
            dict(item)
            for item in preflight.get("paragraph_checks") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ],
        "paragraph_findings": [
            dict(item)
            for item in preflight.get("paragraph_findings") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ],
    }


def update_best_paragraph_candidates(
    best_candidates: dict[str, dict[str, Any]],
    *,
    source_markdown: str,
    candidate_markdown: str,
    source_evaluation: dict[str, Any],
    candidate_evaluation: dict[str, Any],
    source_preflight: dict[str, Any],
    candidate_preflight: dict[str, Any],
    candidate_evidence: dict[str, dict[str, Any]],
    min_words: int,
    max_words: int,
    iteration: int,
    blocked_paragraph_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep individually safe candidates that improve score or evidence accuracy."""

    source_rows = {
        str(item.get("paragraph_id") or ""): item
        for item in parse_marked_paragraphs(source_markdown)
    }
    candidate_rows = {
        str(item.get("paragraph_id") or ""): item
        for item in parse_marked_paragraphs(candidate_markdown)
    }
    source_scores = _paragraph_score_map(source_evaluation)
    candidate_scores = _paragraph_score_map(candidate_evaluation)
    paragraph_count = max(1, len(source_scores))
    source_checks = {
        str(item.get("paragraph_id") or ""): item
        for item in source_preflight.get("paragraph_checks") or []
        if isinstance(item, dict)
    }
    source_check_entries = {entry['paragraph_id']: entry
                            for entry in source_check_rows(candidate_evaluation, candidate_evidence)}
    excluded: list[dict[str, Any]] = []
    blocked_paragraph_ids = blocked_paragraph_ids or set()
    for paragraph_id, source_row in source_rows.items():
        candidate_row = candidate_rows.get(paragraph_id)
        source_score = source_scores.get(paragraph_id)
        candidate_score = candidate_scores.get(paragraph_id)
        if candidate_row is None or source_score is None or candidate_score is None:
            continue
        original_text = str(source_row.get("text") or "").strip()
        candidate_text = str(candidate_row.get("text") or "").strip()
        if clean_text(original_text) == clean_text(candidate_text):
            continue
        if paragraph_id in blocked_paragraph_ids:
            excluded.append(
                {
                    "paragraph_id": paragraph_id,
                    "reasons": ["user_modified_paragraph"],
                    "iteration": iteration,
                }
            )
            continue
        word_range_applicable = bool(
            source_checks.get(paragraph_id, {}).get("word_range_applicable", True)
        )
        errors, warnings = validate_rewrite_report(
            original_text,
            candidate_text,
            min_words if word_range_applicable else 1,
            max_words,
            source_corrections=source_score.get('source_corrections'),
            allowed_unsupported_claims=[
                str(value)
                for value in source_score.get("unsupported_claims") or []
                if str(value).strip()
            ],
        )
        old_score = float(source_score.get("score") or 0)
        new_score = float(candidate_score.get("score") or 0)
        source_status = str(source_score.get("source_check_status") or "not_assessed")
        candidate_status = str(
            candidate_score.get("source_check_status") or "not_assessed"
        )
        evidence_rank = {
            "not_assessed": 0,
            "needs_human_review": 1,
            "contradicted": 1,
            "unsupported": 1,
            "not_found_in_checked_scope": 2,
            "partially_supported": 2,
            "not_applicable": 3,
            "verified": 4,
        }
        source_unsupported = {
            clean_text(value)
            for value in source_score.get("unsupported_claims") or []
            if clean_text(value)
        }
        candidate_unsupported = {
            clean_text(value)
            for value in candidate_score.get("unsupported_claims") or []
            if clean_text(value)
        }
        accuracy_improved = bool(
            evidence_rank.get(candidate_status, 0)
            > evidence_rank.get(source_status, 0)
            or candidate_unsupported < source_unsupported
        )
        source_failed_dimensions = {
            str(value)
            for value in source_score.get("failed_dimensions") or []
            if str(value).strip()
        }
        candidate_failed_dimensions = {
            str(value)
            for value in candidate_score.get("failed_dimensions") or []
            if str(value).strip()
        }
        introduced_issue_ids = sorted(
            {
                *(f"dimension:{value}" for value in candidate_failed_dimensions - source_failed_dimensions),
                *(f"unsupported:{value}" for value in candidate_unsupported - source_unsupported),
            }
        )
        route_rank = {
            "human_confirmation": 0,
            "local_source_recheck": 0,
            "section_rewrite": 1,
            "final_polish": 2,
            "pass": 3,
        }
        source_route = str(source_score.get("route") or "section_rewrite")
        candidate_route = str(candidate_score.get("route") or "section_rewrite")
        target_issue_resolved = bool(
            accuracy_improved
            or route_rank.get(candidate_route, 0) > route_rank.get(source_route, 0)
            or (
                source_failed_dimensions
                and not (source_failed_dimensions & candidate_failed_dimensions)
            )
        )
        if source_unsupported or finding_category(source_score) == "evidence":
            # A better score or a softer route cannot clear a factual defect.
            target_issue_resolved = bool(
                candidate_status in {"verified", "partially_supported"}
                and not candidate_unsupported
                and not (source_failed_dimensions & candidate_failed_dimensions)
                and not candidate_score.get("missing_core_claim_ids")
            )
        rejection_reasons = list(errors)
        if new_score < old_score - DEFAULT_SCORE_TOLERANCE:
            rejection_reasons.append("paragraph_score_regression")
        if candidate_route in {"human_confirmation", "local_source_recheck"}:
            rejection_reasons.append("scientific_ambiguity_requires_confirmation")
        if introduced_issue_ids:
            rejection_reasons.append("candidate_introduced_new_issues")
        if not target_issue_resolved:
            rejection_reasons.append("target_issue_not_resolved")
        if rejection_reasons:
            excluded.append(
                {
                    "paragraph_id": paragraph_id,
                    "source_paragraph_score": round(old_score, 2),
                    "candidate_paragraph_score": round(new_score, 2),
                    "reasons": list(dict.fromkeys(rejection_reasons)),
                    "introduced_issue_ids": introduced_issue_ids,
                    "target_issue_resolved": target_issue_resolved,
                    "iteration": iteration,
                }
            )
            continue
        previous = best_candidates.get(paragraph_id)
        if previous:
            previous_accuracy = bool(previous.get("accuracy_improved"))
            if previous_accuracy and not accuracy_improved:
                continue
            if (
                previous_accuracy == accuracy_improved
                and float(previous.get("candidate_paragraph_score") or 0) >= new_score
            ):
                continue
        best_candidates[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "original_text": original_text,
            "candidate_text": candidate_text,
            "source_paragraph_score": round(old_score, 2),
            "candidate_paragraph_score": round(new_score, 2),
            "score_delta": round(new_score - old_score, 2),
            "overall_score_delta": round(
                (new_score - old_score) / paragraph_count, 4
            ),
            "accuracy_improved": accuracy_improved,
            "target_issue_resolved": target_issue_resolved,
            "introduced_issue_ids": introduced_issue_ids,
            "score_tolerance": DEFAULT_SCORE_TOLERANCE,
            "source_check_status_before": source_status,
            "source_check_status_after": candidate_status,
            "unsupported_claims_before": sorted(source_unsupported),
            "unsupported_claims_after": sorted(candidate_unsupported),
            "iteration": iteration,
            "validation_warnings": warnings,
            "candidate_evaluation": paragraph_candidate_evaluation(paragraph_id, candidate_score,
                candidate_preflight, source_check_entries.get(paragraph_id) or {}, warnings=warnings),
        }
    return excluded


def evaluation_with_best_candidates(
    source_evaluation: dict[str, Any],
    best_candidates: dict[str, dict[str, Any]],
    *,
    paragraph_goal: float,
) -> dict[str, Any]:
    """Overlay targeted paragraph scores without claiming a new full score."""

    result = json.loads(json.dumps(source_evaluation, ensure_ascii=False))
    source_rows = _paragraph_score_map(source_evaluation)
    rows = dict(source_rows)
    for paragraph_id, candidate in best_candidates.items():
        evaluation = dict(candidate.get("candidate_evaluation") or {})
        score = dict(evaluation.get("paragraph_score") or {})
        if score:
            rows[paragraph_id] = {**score, "paragraph_id": paragraph_id}
    ordered_ids = [
        str(item.get("paragraph_id") or "")
        for item in source_evaluation.get("paragraph_scores") or []
        if isinstance(item, dict) and str(item.get("paragraph_id") or "")
    ]
    paragraph_scores = [rows[paragraph_id] for paragraph_id in ordered_ids]
    result["paragraph_scores"] = paragraph_scores
    result["paragraph_failures"] = [
        row
        for paragraph_id, row in rows.items()
        if paragraph_id in best_candidates
        and (
            str(row.get("route") or "") != "pass"
            or str(row.get("severity") or "") in {"critical", "major"}
        )
    ]
    result["blocking_paragraph_failures"] = [
        row
        for row in paragraph_scores
        if paragraph_finding_is_blocking(row)
    ]
    score = float(source_evaluation.get("total_score") or 0) + sum(
        float(candidate.get("overall_score_delta") or 0)
        for candidate in best_candidates.values()
    )
    result["total_score"] = round(max(0.0, min(score, 100.0)), 2)
    result["quality_scope"] = "changed_paragraphs_provisional"
    result["requires_full_draft_refresh"] = True
    result["decision"] = "REGENERATE_SECTIONS"
    return result


def write_batch_review_candidates(
    path: Path,
    *,
    project_id: str,
    source_markdown: str,
    source_evaluation: dict[str, Any],
    best_candidates: dict[str, dict[str, Any]],
    excluded: list[dict[str, Any]],
    evaluated_markdown: str = "",
    full_draft_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_markdown = source_markdown
    source_order = [
        str(item.get("paragraph_id") or "")
        for item in parse_marked_paragraphs(source_markdown)
    ]
    changes = [
        best_candidates[paragraph_id]
        for paragraph_id in source_order
        if paragraph_id in best_candidates
    ]
    for change in changes:
        candidate_markdown = replace_paragraph_in_markdown(
            candidate_markdown,
            str(change["paragraph_id"]),
            str(change["candidate_text"]),
        )
    source_score = float(source_evaluation.get("total_score") or 0)
    candidate_score = source_score + sum(
        float(change.get("overall_score_delta") or 0)
        for change in changes
    )
    payload = {
        "schema_version": 1,
        "project_id": project_id,
        "source_score": round(source_score, 2),
        "candidate_score": round(max(0.0, min(candidate_score, 100.0)), 2),
        "candidate_draft_text": candidate_markdown.rstrip() + "\n",
        "changes": changes,
        "excluded": excluded,
        "source_evaluation": source_evaluation,
        "created_at": utc_now(),
    }
    exact_evaluated_markdown = str(evaluated_markdown or "").rstrip() + "\n"
    if (
        changes
        and full_draft_evaluation
        and exact_evaluated_markdown == payload["candidate_draft_text"]
    ):
        payload.update(
            {
                "candidate_score": round(
                    float(full_draft_evaluation.get("total_score") or 0), 2
                ),
                "full_draft_evaluated": True,
                "full_draft_evaluation": full_draft_evaluation,
                "full_draft_evaluated_at": utc_now(),
            }
        )
    write_json(path, payload)
    return payload


def paragraph_candidate_evaluation(paragraph_id, score, preflight, source_entry, *, warnings=None):
    """One serialized score contract for ordinary and joint candidates."""
    return {"schema_version": 1, "evaluation_scope": "single_paragraph",
            "evaluation_mode": "batch_candidate", "paragraph_id": paragraph_id,
            "paragraph_score": dict(score), "local_hard_gate_failures": [],
            "local_preflight": _paragraph_preflight(preflight, paragraph_id),
            "source_check_entry": dict(source_entry), "validation_warnings": warnings or [],
            "evaluated_at": utc_now()}


def record_rewrite_overlay(
    project: Path,
    paragraph_id: str,
    old_text: str,
    new_text: str,
) -> None:
    """Persist a replayable Stage-8 overlay without mutating Stage-5 source outputs."""
    path = project / "04_first_draft" / "feedback_loop_rewrites.json"
    payload = read_json(path, {}) or {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        entries = {}
    previous = entries.get(paragraph_id)
    original_source_hash = (
        str(previous.get("source_text_sha256") or "")
        if isinstance(previous, dict)
        else ""
    )
    entries[paragraph_id] = {
        "paragraph_id": paragraph_id,
        # Keep the hash of the original deterministic Stage-8 paragraph across
        # multiple loop iterations.  Otherwise a later rewrite would replace
        # it with the hash of an earlier rewrite and could no longer be safely
        # replayed after the draft is rebuilt.
        "source_text_sha256": original_source_hash
        or hashlib.sha256(clean_text(old_text).encode("utf-8")).hexdigest(),
        "rewritten_text": new_text.strip(),
        "updated_at": utc_now(),
    }
    write_json(
        path,
        {
            **payload,
            "schema_version": 1,
            "project_id": project.name,
            "policy": "Apply only when paragraph_id and source_text_sha256 still match.",
            "entries": entries,
        },
    )


def record_paragraph_history(
    project: Path,
    paragraph_id: str,
    old_text: str,
    operation: str,
) -> None:
    """Expose accepted batch rewrites through the normal Stage-8 history UI."""
    path = project / "04_first_draft" / "paragraph_history.json"
    payload = read_json(path, {}) or {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        entries = []
    entries.append(
        {
            "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
            "paragraph_id": paragraph_id,
            "operation": operation,
            "old_text": old_text,
            "snapshot_file": "",
        }
    )
    write_json(path, {"entries": entries})


def apply_rewrite_overlays(project: Path) -> dict[str, Any]:
    """Replay safe feedback rewrites after a future deterministic draft rebuild."""
    draft_path = project / "04_first_draft" / "first_draft.md"
    overlay_path = project / "04_first_draft" / "feedback_loop_rewrites.json"
    payload = read_json(overlay_path, {}) or {}
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    if not draft_path.is_file() or not isinstance(entries, dict) or not entries:
        return {"applied": [], "conflicts": []}
    markdown = draft_path.read_text(encoding="utf-8", errors="replace")
    applied: list[str] = []
    conflicts: list[str] = []
    for paragraph_id, entry in entries.items():
        current = next(
            (item for item in parse_marked_paragraphs(markdown) if item["paragraph_id"] == paragraph_id),
            None,
        )
        if not current or not isinstance(entry, dict):
            conflicts.append(str(paragraph_id))
            continue
        current_sha = hashlib.sha256(clean_text(current["text"]).encode("utf-8")).hexdigest()
        if current_sha != str(entry.get("source_text_sha256") or ""):
            conflicts.append(str(paragraph_id))
            continue
        rewritten = str(entry.get("rewritten_text") or "").strip()
        if not rewritten:
            conflicts.append(str(paragraph_id))
            continue
        markdown = replace_paragraph_in_markdown(markdown, str(paragraph_id), rewritten)
        applied.append(str(paragraph_id))
    if applied:
        temporary = draft_path.with_suffix(".md.feedback-replay.tmp")
        temporary.write_text(markdown, encoding="utf-8")
        temporary.replace(draft_path)
    report = {"applied": applied, "conflicts": conflicts, "applied_at": utc_now()}
    write_json(project / "04_first_draft" / "feedback_loop_replay.json", report)
    return report


def status_path(project: Path) -> Path:
    return project / "04_first_draft" / "feedback_loop_status.json"


def stop_path(project: Path) -> Path:
    return project / "04_first_draft" / "feedback_loop.stop"


def update_status(project: Path, **updates: Any) -> dict[str, Any]:
    path = status_path(project)
    current = read_json(path, {}) or {}
    current.update(updates)
    current["updated_at"] = utc_now()
    write_json(path, current)
    return current


def reviewer_findings(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            **item,
            "id": f"PAR-{index:03d}",
            "reviewer": "rubric_evaluator",
            "severity": item.get("severity", "minor"),
            "paragraph_id": item["paragraph_id"],
            "location": item["paragraph_id"],
            "fragment": "",
            "diagnosis": item.get("diagnosis", ""),
            "recommended_direction": item.get("recommended_action") or "Review this finding at its recorded repair target.",
            "confidence": "high",
            "route": item["route"],
        }
        for index, item in enumerate(evaluation.get("paragraph_failures") or [], 1)
    ]


def source_check_rows(
    evaluation: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    score_by_id = {
        str(item.get("paragraph_id") or ""): item
        for item in evaluation.get("paragraph_scores") or []
        if isinstance(item, dict)
    }
    entries: list[dict[str, Any]] = []
    for paragraph_id, paragraph_evidence in evidence.items():
        score = score_by_id.get(paragraph_id, {})
        papers: list[dict[str, Any]] = []
        for paper in paragraph_evidence.get("evidence") or []:
            if not isinstance(paper, dict):
                continue
            papers.append(
                {
                    "paper_id": paper.get("paper_id"),
                    "title": paper.get("title"),
                    "source_kind": paper.get("source_kind"),
                    "source_path": paper.get("source_path"),
                    "source_content_hash": paper.get("source_content_hash"),
                    "passages": paper.get("original_passages") or [],
                }
            )
        entries.append(
            {
                "paragraph_id": paragraph_id,
                "paragraph_text_hash": paragraph_evidence.get("paragraph_text_hash"),
                "targeted_source_recheck": paragraph_evidence.get("targeted_source_recheck"),
                "citation_binding": paragraph_evidence.get("citation_binding"),
                "paper_ids": paragraph_evidence.get("paper_ids") or [],
                "evidence_scope": paragraph_evidence.get("evidence_scope"),
                "argument_plan": paragraph_evidence.get("argument_plan") or [],
                "source_check_status": score.get("source_check_status", "not_assessed"),
                "source_evidence_refs": score.get("source_evidence_refs") or [],
                "unsupported_claims": score.get("unsupported_claims") or [],
                "missing_core_claim_ids": score.get("missing_core_claim_ids") or [],
                "claim_fact_bindings": score.get("claim_fact_bindings") or [],
                "route": score.get("route"),
                "papers": papers,
            }
        )
    return entries


def original_source_check_report(project, evaluation, evidence):
    entries = source_check_rows(evaluation, evidence)
    counts = Counter(str(item.get("source_check_status") or "not_assessed") for item in entries)
    return {
        "schema_version": 1,
        "project_id": project.name,
        "generated_at": utc_now(),
        "draft_sha256": sha256_file(project / "04_first_draft" / "first_draft.md"),
        "counts": dict(sorted(counts.items())),
        "entries": entries,
    }


def evidence_from_source_check_report(
    report: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Restore the bounded evidence view stored with a reusable Quality."""

    restored: dict[str, dict[str, Any]] = {}
    for raw_entry in report.get("entries") or []:
        if not isinstance(raw_entry, dict):
            continue
        paragraph_id = str(raw_entry.get("paragraph_id") or "")
        if not paragraph_id:
            continue
        papers = []
        for raw_paper in raw_entry.get("papers") or []:
            if not isinstance(raw_paper, dict):
                continue
            passages = [
                dict(passage)
                for passage in raw_paper.get("passages") or []
                if isinstance(passage, dict)
            ]
            papers.append(
                {
                    "paper_id": str(raw_paper.get("paper_id") or ""),
                    "title": str(raw_paper.get("title") or ""),
                    "source_kind": str(raw_paper.get("source_kind") or ""),
                    "source_path": str(raw_paper.get("source_path") or ""),
                    "source_content_hash": raw_paper.get("source_content_hash"),
                    "local_source_available": bool(raw_paper.get("source_path")),
                    "original_text_available": bool(passages),
                    "original_passages": passages,
                }
            )
        paper_ids = [
            str(value)
            for value in raw_entry.get("paper_ids") or []
            if str(value).strip()
        ]
        restored[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "paragraph_text_hash": raw_entry.get('paragraph_text_hash'),
            "citation_binding": raw_entry.get('citation_binding'),
            "targeted_source_recheck": raw_entry.get('targeted_source_recheck'),
            "paper_ids": paper_ids,
            "local_source_available": bool(paper_ids) and bool(papers) and all(
                paper.get("local_source_available") for paper in papers
            ),
            "original_source_ready": bool(paper_ids) and bool(papers) and all(
                paper.get("original_passages") for paper in papers
            ),
            "evidence_scope": str(raw_entry.get("evidence_scope") or ""),
            "argument_plan": raw_entry.get("argument_plan") or [],
            "evidence": papers,
        }
    return restored


def apply_evidence_rescue_outcomes(
    evaluation: dict[str, Any],
    prior_quality_context: dict[str, Any],
    *,
    paragraph_goal: float,
    evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply trusted bounded-search outcomes to a reusable baseline."""

    result = json.loads(json.dumps(evaluation, ensure_ascii=False))
    outcomes = {
        str(paragraph_id): dict(value)
        for paragraph_id, value in (
            prior_quality_context.get("evidence_rescue_outcomes") or {}
        ).items()
        if isinstance(value, dict)
    }
    scores = [
        dict(row)
        for row in result.get("paragraph_scores") or []
        if isinstance(row, dict)
    ]
    for row in scores:
        if row.get("source_check_status") == "contradicted" or row.get("evidence_problem_type") == "conflict":
            continue
        outcome = outcomes.get(str(row.get("paragraph_id") or ""), {})
        status = str(outcome.get("status") or "")
        if status == "provider_deferred":
            row["evidence_rescue_status"] = status
            continue
        if status != "not_found_in_checked_scope":
            continue
        if row.get("source_check_status") == "verified" and not row.get("unsupported_claims") and not row.get("missing_core_claim_ids"):
            continue
        row["source_check_status"] = status
        row["evidence_rescue_status"] = status
        row["evidence_problem_type"] = "checked_scope_no_match"
        if row.get("unsupported_claims"):
            row["route"] = "section_rewrite"
            row["severity"] = "major"
            row["score"] = min(float(row.get("score") or 0), 79.0)
    if evidence is not None:
        for row in scores:
            row.update(paragraph_repair_contract(row, evidence.get(str(row.get("paragraph_id") or ""), {})))
    result["paragraph_scores"] = scores
    result["paragraph_failures"] = [
        row
        for row in scores
        if str(row.get("route") or "") != "pass"
        or str(row.get("severity") or "") in {"critical", "major"}
    ]
    result["blocking_paragraph_failures"] = [
        row
        for row in scores
        if paragraph_finding_is_blocking(row)
    ]
    if result["blocking_paragraph_failures"]:
        result["decision"] = "REGENERATE_SECTIONS"
    elif not result.get("hard_gate_failures"):
        result["decision"] = "PASS"
    return result


class BaselineNotReusable(RuntimeError):
    """A stale cache requires fresh evaluation, not a failed optimization job."""


def reusable_baseline_evaluation(
    project: Path,
    *,
    artifact_dir: Path,
    status_iteration: int,
    args=None,
    rubric=None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Materialize an API-approved full-draft baseline without model calls."""

    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    evaluation = read_json(first / "baseline_quality.json", {}) or {}
    paragraphs = parse_marked_paragraphs(
        draft_path.read_text(encoding="utf-8", errors="replace")
    )
    coverage = [str(row.get("paragraph_id") or "") for row in paragraphs]
    if (
        str(evaluation.get("quality_scope") or "") != FULL_DRAFT_QUALITY_SCOPE
        or str(evaluation.get("evaluation_rule_version") or "")
        != DRAFT_QUALITY_RULE_VERSION
        or str(evaluation.get("evaluation_input_sha256") or "")
        != sha256_file(draft_path)
        or list(evaluation.get("paragraph_coverage") or []) != coverage
    ):
        raise BaselineNotReusable("The reusable Draft quality baseline no longer matches the exact manuscript.")
    prior_quality_context = read_json(
        first / "prior_quality_context.json", {}
    ) or {}
    source_report = dict(evaluation.get("source_check") or {})
    evidence = evidence_from_source_check_report(source_report)
    evaluation = apply_evidence_rescue_outcomes(
        evaluation,
        prior_quality_context,
        paragraph_goal=float(
            evaluation.get("paragraph_pass_threshold")
            or PARAGRAPH_PASS_THRESHOLD
        ),
        evidence=evidence,
    )
    preflight = dict(evaluation.get("preflight") or {})
    if args is not None and rubric is not None:
        paragraph_rubric = scoped_rubric(rubric, 'paragraph')
        paragraph_dimensions = {str(d['id']) for d in paragraph_rubric['dimensions']}
        baseline_raw = {'paragraph_scores': evaluation.get('paragraph_scores') or [],
                        'dimension_scores': [d for d in evaluation.get('dimension_scores') or []
                                             if str(d['id']) in paragraph_dimensions],
                        'dimension_basis': 'retained_baseline'}
        rescored = []
        def score_one(paragraph, expanded):
            checked = score_source_recheck(paragraph, expanded, paragraph_rubric, preflight,
                float(args.goal), float(args.paragraph_goal), [], prior_quality_context)
            rescored.append(paragraph['paragraph_id'])
            return checked
        groups = targeted_source_recheck(project, paragraphs, baseline_raw, evidence, score_one)
        if rescored:
            global_dimensions = [d for d in evaluation.get('dimension_scores') or []
                                 if str(d['id']) not in paragraph_dimensions]
            raw = merge_batched_evaluations(rubric, groups, global_dimension_scores=global_dimensions)
            updated = normalize_evaluation(raw, rubric, paragraphs, preflight, float(args.goal),
                                           float(args.paragraph_goal), evidence=evidence)
            updated['dimension_score_basis']['global'] = 'retained_baseline'
            evaluation.update(updated)
            evaluation['score'] = evaluation['total_score']
        source_report = original_source_check_report(project, evaluation, evidence)
        evaluation['source_check'] = source_report
    artifact_dir.mkdir(parents=True, exist_ok=True)
    write_json(first / "rubric_evaluation.json", evaluation)
    write_json(first / "reviewer_findings.json", reviewer_findings(evaluation))
    write_json(first / "first_draft_preflight.json", preflight)
    write_json(first / "original_source_check.json", source_report)
    write_json(artifact_dir / "rubric_evaluation.json", evaluation)
    write_json(artifact_dir / "original_source_check.json", source_report)
    gate = queue_artifacts(project, evaluation, preflight)
    update_status(
        project,
        phase="baseline_reused",
        iteration=status_iteration,
        quality_reused=True,
        score=float(evaluation.get("total_score") or evaluation.get("score") or 0),
        paragraph_total=len(paragraphs),
        paragraph_completed=len(paragraphs),
        gate_decision=gate["gate_decision"],
    )
    return preflight, evaluation, gate, paragraphs, evidence


def queue_artifacts(project: Path, evaluation: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    first = project / "04_first_draft"
    rewrite = []
    polish = []
    blocking_ids = {
        str(item.get("paragraph_id") or "")
        for item in evaluation.get("blocking_paragraph_failures") or []
    }
    for item in evaluation.get("paragraph_failures") or []:
        target = (
            polish
            if not paragraph_finding_is_blocking(item)
            and str(item.get("paragraph_id") or "") not in blocking_ids
            else rewrite
        )
        target.append({"origin": "rubric", **item})
    score = float(evaluation.get("total_score", 0))
    goal = float(evaluation.get("pass_threshold", DRAFT_PASS_THRESHOLD))
    hard = sorted(set(evaluation.get("hard_gate_failures") or []) | set(preflight.get("hard_regressions") or []))
    released = evaluation.get("decision") == "PASS" and not hard and not rewrite
    decision = "GATE_RELEASE" if released else "GATE_HOLD_REWRITE_REQUIRED"
    write_json(first / "first_draft_rewrite_queue.json", {"project_id": project.name, "items": rewrite})
    write_json(first / "first_draft_final_polish_queue.json", {"project_id": project.name, "items": polish})
    gate = {
        "project_id": project.name,
        "status": "RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH" if released else "REWRITE_REQUIRED",
        "gate_decision": decision,
        "unified_rubric_score": score,
        "hard_gate_failures": hard,
        "rewrite_queue_path": "04_first_draft/first_draft_rewrite_queue.json",
        "final_polish_queue_path": "04_first_draft/first_draft_final_polish_queue.json",
        "next_action": "Generate final outputs." if released else "Continue targeted paragraph improvement or review blocked facts.",
    }
    write_json(first / "first_draft_gate_status.json", gate)
    return gate


def targeted_source_recheck(project, batch, raw, evidence, score_one):
    """One bounded local recovery per failed paragraph; reuse the existing scorer.

    The returned groups feed the same weighted rubric aggregation as ordinary
    batches. Unaffected batch dimensions retain their original evaluation basis.
    """
    if not any(s.get('source_check_status') in {'partially_supported', 'needs_human_review', 'unsupported', 'contradicted'}
               for s in raw.get('paragraph_scores') or []):
        return [(batch, raw)]
    root = project.parent.parent
    structured, rows = paragraph_metadata(project), matrix_rows(project)
    contract, cache = claim_evidence_contract(project), {}
    saved = read_json(project / '04_first_draft' / 'original_source_check.json', {}) or read_json(
        project / '04_first_draft' / 'prior_quality_context.json', {}).get('source_check') or {}
    history = {e.get('paragraph_id'): e for e in saved.get('entries') or []}
    scores = {s.get('paragraph_id'): s for s in raw.get('paragraph_scores') or []}
    recovered, retained = [], []
    for paragraph in batch:
        pid = paragraph['paragraph_id']
        finding = scores.get(pid, {})
        if finding.get('source_check_status') not in {'partially_supported', 'needs_human_review', 'unsupported', 'contradicted'}:
            retained.append(paragraph)
            continue
        queries = [clean_text(q) for q in finding.get('unsupported_claims') or [] if clean_text(q)][:4]
        queries = queries or source_query_variants(paragraph['text'])[:4]
        old = evidence.get(pid, {})
        try:
            expanded = source_evidence(root, project, paragraph, structured.get(pid, {}), rows,
                                       cache, contract, queries=queries)
            # Keep direct bindings as well as recovered context, never erase a contradiction.
            by_paper = {p['paper_id']: p for p in expanded.get('evidence') or []}
            for paper in old.get('evidence') or []:
                if paper['paper_id'] in by_paper:
                    target = by_paper[paper['paper_id']]
                    seen = {p['ref'] for p in target['original_passages']}
                    target['original_passages'].extend(p for p in paper.get('original_passages') or [] if p['ref'] not in seen)
            text_hash = hashlib.sha256(str(paragraph['text']).encode()).hexdigest()
            citation_binding = paragraph_citation_binding(project, paragraph)
            scope_hash = hashlib.sha256(json.dumps([
                DRAFT_QUALITY_RULE_VERSION, 'targeted-source-check/3', text_hash,
                citation_binding,
                sorted(clean_text(q).casefold() for q in queries),
                sorted((p['paper_id'], str(p.get('source_content_hash') or ''),
                        sorted((str(span.get('ref') or ''), clean_text(span.get('text')))
                               for span in p.get('original_passages') or []))
                       for p in expanded.get('evidence') or [])
            ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            previous = (history.get(pid) or {}).get('targeted_source_recheck') or {}
            record = {'input_fingerprint': scope_hash, 'queries': queries, 'status': 'checked_no_new_support'}
            old_texts = {(paper['paper_id'], p.get('ref'), clean_text(p.get('text'))) for paper in old.get('evidence') or [] for p in paper.get('original_passages') or []}
            new_texts = {(paper['paper_id'], p.get('ref'), clean_text(p.get('text'))) for paper in expanded.get('evidence') or [] for p in paper.get('original_passages') or []}
            # Retain recovered passages even when verification is negative,
            # cached or temporarily unavailable; passages are not verdicts.
            expanded.update(paragraph_text_hash=text_hash, citation_binding=citation_binding, targeted_source_recheck=record)
            evidence[pid] = expanded
            old = expanded
            needs_attribution_check = bool(finding.get('unsupported_claims')
                and finding.get('source_check_status') in {'contradicted', 'partially_supported'}
                and not validated_attribution_repair(paragraph['text'], finding.get('source_attribution_repair'), expanded))
            if (not new_texts or (not new_texts - old_texts and not previous and not needs_attribution_check) or (previous.get('input_fingerprint') == scope_hash
                    and previous.get('status') == 'checked_no_new_support')):
                retained.append(paragraph)
                continue
            update_status(project, phase='source_checking', current_paragraph_id=pid)
            checked = score_one(paragraph, expanded)
            if [s.get('paragraph_id') for s in checked.get('paragraph_scores') or []] != [pid]:
                raise ValueError('Source recheck returned a different paragraph identity')
            checked_score = checked['paragraph_scores'][0]
            valid_refs = {p['ref'] for paper in expanded.get('evidence') or [] for p in paper.get('original_passages') or []}
            if (checked_score.get('source_check_status') == 'verified'
                    and not checked_score.get('unsupported_claims')
                    and not checked_score.get('missing_core_claim_ids')
                    and checked_score.get('source_evidence_refs')
                    and set(checked_score.get('source_evidence_refs') or []) <= valid_refs):
                record['status'] = 'supported_without_prose_change'
            evidence[pid] = expanded
            recovered.append(([paragraph], checked))
        except Exception as exc:
            if isinstance(exc, ValueError):
                old['targeted_source_recheck'] = {'status': 'response_invalid'}
                retained.append(paragraph)
                continue
            if not recoverable_paragraph_provider_failure(exc):
                raise
            old['targeted_source_recheck'] = {'status': 'provider_deferred'}
            retained.append(paragraph)
    if retained:
        ids = {p['paragraph_id'] for p in retained}
        recovered.insert(0, (retained, {**raw,
            'dimension_basis': 'retained_original_batch' if recovered else raw.get('dimension_basis', 'assessed_batch'),
            'paragraph_scores': [s for s in raw.get('paragraph_scores') or [] if s.get('paragraph_id') in ids]}))
    return recovered


def score_source_recheck(paragraph, expanded, rubric, preflight, goal, paragraph_goal,
                         draft_structure, prior_quality_context):
    checked = call_json_model(evaluation_prompt(
        rubric, [paragraph], {paragraph['paragraph_id']: expanded}, preflight, goal, paragraph_goal,
        draft_structure=draft_structure, prior_quality_context=prior_quality_context,
    ), label=f"Targeted source recheck {paragraph['paragraph_id']}")
    try:
        merge_batched_evaluations(rubric, [([paragraph], checked)], scoped=True)
    except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('Invalid targeted source response') from exc
    return checked


def request_paragraph_score_batches(
    project: Path,
    *,
    rubric: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    preflight: dict[str, Any],
    goal: float,
    paragraph_goal: float,
    draft_structure: list[dict[str, str]],
    prior_quality_context: dict[str, Any],
    label_prefix: str,
) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
    """Score bounded paragraph batches with shared split and progress logic."""

    batches = paragraph_batches(paragraphs)
    raw_batches: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    completed = 0
    batch_index = 0
    timeout_splits = 0
    update_status(
        project,
        scoring_batch_total=len(batches),
        scoring_batch_completed=0,
        paragraph_total=len(paragraphs),
        paragraph_completed=0,
    )
    while batch_index < len(batches):
        batch = batches[batch_index]
        display_index = batch_index + 1
        try:
            raw_batch = call_json_model(
                evaluation_prompt(
                    rubric,
                    batch,
                    evidence,
                    preflight,
                    goal,
                    paragraph_goal,
                    batch_index=display_index,
                    batch_total=len(batches),
                    draft_structure=draft_structure,
                    prior_quality_context=prior_quality_context,
                ),
                label=f"{label_prefix} {display_index}/{len(batches)}",
            )
        except (ProviderDeadlineExceeded, ProviderRequestBodyBudgetExceeded) as exc:
            if len(batch) <= 1:
                if isinstance(exc, ProviderRequestBodyBudgetExceeded):
                    raise ProviderRequestBodyBudgetExceeded(
                        "The complete evidence for one paragraph exceeds the provider request budget. "
                        "Use a provider with a larger request budget; evidence was not truncated."
                    ) from exc
                raise RuntimeError(
                    "Scientific provider timed out while scoring one paragraph. "
                    "Use a faster text model or a provider without a 120-second proxy deadline."
                ) from exc
            midpoint = (len(batch) + 1) // 2
            batches[batch_index : batch_index + 1] = [
                batch[:midpoint],
                batch[midpoint:],
            ]
            timeout_splits += 1
            update_status(
                project,
                scoring_batch_total=len(batches),
                scoring_batch_completed=batch_index,
                scoring_timeout_splits=timeout_splits,
            )
            continue
        def score_one(paragraph, expanded):
            return score_source_recheck(paragraph, expanded, rubric, preflight, goal, paragraph_goal,
                                         draft_structure, prior_quality_context)
        raw_batches.extend(targeted_source_recheck(project, batch, raw_batch, evidence, score_one))
        completed += len(batch)
        batch_index += 1
        update_status(
            project,
            paragraph_completed=completed,
            scoring_batch_completed=batch_index,
            scoring_batch_total=len(batches),
        )
    return raw_batches


def evaluate_current_draft(
    review_root: Path,
    project: Path,
    args: argparse.Namespace,
    rubric: dict[str, Any],
    artifact_dir: Path,
    *,
    status_iteration: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Evaluate exactly the draft bytes that are currently on disk."""

    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    update_status(project, phase="preflight", iteration=status_iteration)
    preflight = deterministic_preflight(
        review_root,
        args.project_id,
        min_words=args.min_case_words,
        max_words=args.max_case_words,
    )
    write_json(artifact_dir / "first_draft_preflight.json", preflight)
    markdown = draft_path.read_text(encoding="utf-8", errors="replace")
    paragraphs = parse_marked_paragraphs(markdown)
    structured = paragraph_metadata(project)
    rows = matrix_rows(project)
    source_cache: dict[str, dict[str, Any]] = {}
    academic_contract = claim_evidence_contract(project)
    prior_quality_context = read_json(
        first / "prior_quality_context.json", {}
    )
    update_status(project, phase="source_checking")
    evidence = {
        str(paragraph["paragraph_id"]): source_evidence(
            review_root,
            project,
            paragraph,
            structured.get(str(paragraph["paragraph_id"]), {}),
            rows,
            source_cache,
            academic_contract,
        )
        for paragraph in paragraphs
    }
    update_status(
        project,
        phase="scoring",
        paragraph_total=len(paragraphs),
        paragraph_completed=0,
    )
    draft_structure = [
        {
            "paragraph_id": str(item["paragraph_id"]),
            "heading": clean_text(item.get("heading")),
        }
        for item in paragraphs
    ]
    global_rubric = scoped_rubric(rubric, "global")
    paragraph_rubric = scoped_rubric(rubric, "paragraph")
    try:
        raw_global = call_json_model(
            global_evaluation_prompt(
                global_rubric,
                paragraphs,
                preflight,
                float(args.goal),
                evidence=evidence,
            ),
            label="Whole-draft rubric evaluation",
        )
    except (ProviderRequestBodyBudgetExceeded, ProviderDeadlineExceeded):
        raw_global = call_json_model(
            global_evaluation_prompt(
                global_rubric,
                paragraphs,
                preflight,
                float(args.goal),
                compact=True,
                evidence=evidence,
            ),
            label="Whole-draft rubric evaluation compact retry",
        )
    global_dimension_scores = raw_global.get("dimension_scores") or []
    raw_batches = request_paragraph_score_batches(
        project,
        rubric=paragraph_rubric,
        paragraphs=paragraphs,
        evidence=evidence,
        preflight=preflight,
        goal=float(args.goal),
        paragraph_goal=float(args.paragraph_goal),
        draft_structure=draft_structure,
        prior_quality_context=prior_quality_context,
        label_prefix="First-draft rubric evaluation batch",
    )
    raw = merge_batched_evaluations(
        rubric,
        raw_batches,
        global_dimension_scores=global_dimension_scores,
    )
    evaluation = normalize_evaluation(
        raw,
        rubric,
        paragraphs,
        preflight,
        float(args.goal),
        float(args.paragraph_goal),
        evidence=evidence,
    )
    evaluation = apply_evidence_rescue_outcomes(evaluation, prior_quality_context,
        paragraph_goal=float(args.paragraph_goal), evidence=evidence)
    write_json(first / "rubric_evaluation.json", evaluation)
    write_json(first / "reviewer_findings.json", reviewer_findings(evaluation))
    source_report = original_source_check_report(project, evaluation, evidence)
    write_json(first / "original_source_check.json", source_report)
    write_json(artifact_dir / "rubric_evaluation.json", evaluation)
    write_json(artifact_dir / "original_source_check.json", source_report)
    gate = queue_artifacts(project, evaluation, preflight)
    paragraph_scores = evaluation.get("paragraph_scores") or []
    update_status(
        project,
        phase="evaluated",
        score=evaluation["total_score"],
        score_updated_at=utc_now(),
        gate_decision=gate["gate_decision"],
        paragraph_scores=paragraph_scores,
        paragraph_completed=len(paragraphs),
    )
    return preflight, evaluation, gate, paragraphs, evidence


def evaluate_changed_paragraphs(
    review_root: Path,
    project: Path,
    args: argparse.Namespace,
    rubric: dict[str, Any],
    paragraph_ids: set[str],
    artifact_dir: Path,
    *,
    global_dimension_scores: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Score only changed paragraphs; whole-draft dimensions stay provisional."""

    first = project / "04_first_draft"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    preflight = deterministic_preflight(
        review_root,
        args.project_id,
        min_words=args.min_case_words,
        max_words=args.max_case_words,
    )
    markdown = (first / "first_draft.md").read_text(
        encoding="utf-8", errors="replace"
    )
    all_paragraphs = parse_marked_paragraphs(markdown)
    paragraphs = [
        row
        for row in all_paragraphs
        if str(row.get("paragraph_id") or "") in paragraph_ids
    ]
    found_ids = {str(row.get("paragraph_id") or "") for row in paragraphs}
    if found_ids != paragraph_ids:
        raise RuntimeError(
            "Changed paragraph markers disappeared before incremental evaluation: "
            + ", ".join(sorted(paragraph_ids - found_ids))
        )
    structured = paragraph_metadata(project)
    rows = matrix_rows(project)
    source_cache: dict[str, dict[str, Any]] = {}
    academic_contract = claim_evidence_contract(project)
    evidence = {
        str(paragraph["paragraph_id"]): source_evidence(
            review_root,
            project,
            paragraph,
            structured.get(str(paragraph["paragraph_id"]), {}),
            rows,
            source_cache,
            academic_contract,
        )
        for paragraph in paragraphs
    }
    update_status(
        project,
        phase="scoring_changed_paragraphs",
        changed_paragraph_count=len(paragraphs),
    )
    paragraph_rubric = scoped_rubric(rubric, "paragraph")
    prior_quality_context = read_json(
        first / "prior_quality_context.json", {}
    ) or {}
    draft_structure = [
        {
            "paragraph_id": str(item["paragraph_id"]),
            "heading": clean_text(item.get("heading")),
        }
        for item in all_paragraphs
    ]
    raw_batches = request_paragraph_score_batches(
        project,
        rubric=paragraph_rubric,
        paragraphs=paragraphs,
        evidence=evidence,
        preflight=preflight,
        goal=float(args.goal),
        paragraph_goal=float(args.paragraph_goal),
        draft_structure=draft_structure,
        prior_quality_context=prior_quality_context,
        label_prefix="Changed-paragraph rubric evaluation batch",
    )
    raw = merge_batched_evaluations(
        rubric,
        raw_batches,
        global_dimension_scores=global_dimension_scores,
    )
    evaluation = normalize_evaluation(
        raw,
        rubric,
        paragraphs,
        preflight,
        float(args.goal),
        float(args.paragraph_goal),
        evidence=evidence,
    )
    evaluation['dimension_score_basis']['global'] = 'retained_baseline'
    write_json(artifact_dir / "changed_paragraph_evaluation.json", evaluation)
    write_json(
        artifact_dir / "changed_paragraph_source_check.json",
        original_source_check_report(project, evaluation, evidence),
    )
    return preflight, evaluation, evidence


def evaluation_is_released(
    evaluation: dict[str, Any],
    *,
    goal: float,
    paragraph_goal: float,
) -> bool:
    paragraph_scores = evaluation.get("paragraph_scores") or []
    return bool(
        paragraph_scores
        and not evaluation.get("hard_gate_failures")
        and all(
            not paragraph_finding_is_blocking(item)
            for item in paragraph_scores
        )
    )


def automatic_rewrite_mode(finding, paragraph_evidence, *, paragraph_goal):
    """Compatibility entry point; all selection lives in the shared router."""
    return select_rewrite_mode(finding, paragraph_evidence)


def interactive_rewrite_mode(finding, paragraph_evidence, *, paragraph_goal):
    """Use the same permissions for explicitly requested paragraph candidates."""
    return select_rewrite_mode(finding, paragraph_evidence, interactive=True)


def run_feedback_loop(args: argparse.Namespace) -> dict[str, Any]:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    project_config = read_json(project / "project_config.json", {})
    blueprint = read_json(
        project / "01_matrix_outline" / "section_blueprint.json", {}
    )
    taxonomy_profile = str(
        project_config.get("taxonomy_profile")
        or blueprint.get("taxonomy_profile")
        or "general_academic"
    )
    apply_verification_profile(
        load_taxonomy_verification_profile(
            review_root,
            profile=taxonomy_profile,
            topic_text=str(
                blueprint.get("review_topic")
                or project_config.get("topic")
                or ""
            ),
        )
    )
    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    if not draft_path.is_file():
        raise FileNotFoundError(draft_path)
    rubric_path = Path(__file__).resolve().parents[1] / "references" / "unified_rubric.json"
    rubric = read_json(rubric_path, {})
    rubric_definition = rubric_dimensions(rubric)
    global_dimension_ids = {
        str(item.get("id") or "")
        for item in rubric_definition
        if rubric_dimension_scope(item) == "global"
    }
    rubric_threshold = float(rubric.get("pass_threshold", DRAFT_PASS_THRESHOLD))
    if float(args.goal) < rubric_threshold:
        raise ValueError(
            f"Overall goal cannot be lower than the rubric pass threshold ({rubric_threshold:g})."
        )
    current_markdown = make_xml_compatible(
        draft_path.read_text(encoding="utf-8", errors="replace")
    )[0]
    marked_markdown, marker_report = ensure_prose_paragraph_markers(current_markdown)
    if int(marker_report.get("prose_paragraph_count") or 0) < 1:
        raise RuntimeError("The first draft contains no prose paragraphs to evaluate")
    if (
        marker_report.get("changed")
        or marked_markdown != current_markdown
    ):
        marker_tmp = draft_path.with_suffix(".md.markers.tmp")
        marker_tmp.write_text(marked_markdown, encoding="utf-8")
        marker_tmp.replace(draft_path)
    write_json(first / "paragraph_marker_report.json", marker_report)
    stopper = stop_path(project)
    stopper.unlink(missing_ok=True)
    if getattr(args, "local_revision", False) and not args.evaluate_only:
        import local_revision
        import sys
        return local_revision.run(args, rubric, sys.modules[__name__])
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    runs_dir = first / "feedback_loop" / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(draft_path, run_dir / "first_draft_before.md")
    source_markdown = draft_path.read_text(encoding="utf-8", errors="replace")
    source_draft_sha256 = sha256_file(draft_path)
    source_evaluation: dict[str, Any] = {}
    source_preflight: dict[str, Any] = {}
    best_paragraph_candidates: dict[str, dict[str, Any]] = {}
    excluded_paragraph_candidates: list[dict[str, Any]] = []
    batch_review_path = first / "batch_review_candidates.json"
    batch_review_path.unlink(missing_ok=True)
    overlay_path = first / "feedback_loop_rewrites.json"
    overlay_before = overlay_path.read_bytes() if overlay_path.is_file() else None
    last_valid_draft = draft_path.read_bytes()
    last_valid_overlay = overlay_before
    best_score = -1.0
    best_iteration = 0
    best_draft: bytes | None = None
    best_overlay: bytes | None = None
    best_evaluation: dict[str, Any] = {}
    best_preflight: dict[str, Any] = {}
    best_evidence: dict[str, dict[str, Any]] = {}
    working_evaluation: dict[str, Any] = {}
    working_preflight: dict[str, Any] = {}
    working_evidence: dict[str, dict[str, Any]] = {}
    active_rewrite_checkpoint = first / "feedback_loop_rewrite_checkpoint.json"
    prior_quality_context = read_json(first / "prior_quality_context.json", {}) or {}
    repair_history = dict(prior_quality_context.get("repair_history") or {})
    blocked_paragraph_ids = {
        str(value)
        for value in prior_quality_context.get("unverified_manual_paragraph_ids") or []
        if str(value).strip()
    }

    def record_repair(fingerprint, paragraph_id, outcome, attempts):
        previous = repair_history.get(fingerprint) or {}
        repair_history[fingerprint] = {"paragraph_id": paragraph_id, "outcome": outcome,
            "model_attempts": int(previous.get("model_attempts") or 0) + attempts,
            "source_checked": True}
        # Existing authorized feedback status/checkpoint, not a second database.
        while len(repair_history) > 500:
            repair_history.pop(next(iter(repair_history)))
        update_status(project, repair_history=repair_history)

    def checkpoint_rewrite_queue(
        iteration: int,
        rewrite_items: list[dict[str, Any]],
        *,
        accepted: int,
        rejected: int,
        deferred: int,
        state: str = "running",
    ) -> dict[str, Any]:
        payload = write_rewrite_queue_checkpoint(
            active_rewrite_checkpoint,
            project_id=args.project_id,
            run_id=run_id,
            iteration=iteration,
            source_draft_sha256=source_draft_sha256,
            current_draft_sha256=sha256_file(draft_path),
            rewrite_items=rewrite_items,
            accepted=accepted,
            rejected=rejected,
            deferred=deferred,
            state=state,
        )
        write_json(run_dir / "rewrite_queue_checkpoint.json", payload)
        return payload

    def restore_last_valid_state() -> None:
        draft_tmp = draft_path.with_suffix(".md.feedback-restore.tmp")
        draft_tmp.write_bytes(last_valid_draft)
        draft_tmp.replace(draft_path)
        if last_valid_overlay is None:
            overlay_path.unlink(missing_ok=True)
        else:
            overlay_tmp = overlay_path.with_suffix(overlay_path.suffix + ".restore.tmp")
            overlay_tmp.write_bytes(last_valid_overlay)
            overlay_tmp.replace(overlay_path)

    def remember_best_state(
        score: float,
        iteration: int,
        evaluation: dict[str, Any],
        preflight: dict[str, Any],
        evidence: dict[str, dict[str, Any]],
    ) -> None:
        nonlocal best_score, best_iteration, best_draft, best_overlay
        nonlocal best_evaluation, best_preflight, best_evidence
        if score <= best_score:
            return
        best_score = score
        best_iteration = iteration
        best_draft = draft_path.read_bytes()
        best_overlay = overlay_path.read_bytes() if overlay_path.is_file() else None
        best_evaluation = json.loads(json.dumps(evaluation, ensure_ascii=False))
        best_preflight = json.loads(json.dumps(preflight, ensure_ascii=False))
        best_evidence = json.loads(json.dumps(evidence, ensure_ascii=False))

    def restore_best_scored_state() -> bool:
        if best_draft is None or not best_evaluation:
            return False
        draft_tmp = draft_path.with_suffix(".md.feedback-best.tmp")
        draft_tmp.write_bytes(best_draft)
        draft_tmp.replace(draft_path)
        if best_overlay is None:
            overlay_path.unlink(missing_ok=True)
        else:
            overlay_tmp = overlay_path.with_suffix(overlay_path.suffix + ".best.tmp")
            overlay_tmp.write_bytes(best_overlay)
            overlay_tmp.replace(overlay_path)
        write_json(first / "first_draft_preflight.json", best_preflight)
        write_json(first / "rubric_evaluation.json", best_evaluation)
        write_json(first / "reviewer_findings.json", reviewer_findings(best_evaluation))
        write_json(
            first / "original_source_check.json",
            original_source_check_report(project, best_evaluation, best_evidence),
        )
        queue_artifacts(project, best_evaluation, best_preflight)
        return True
    update_status(
        project,
        project_id=args.project_id,
        run_id=run_id,
        status="running",
        phase="preflight",
        iteration=0,
        max_iterations=args.max_iterations,
        goal=float(args.goal),
        paragraph_goal=float(args.paragraph_goal),
        min_case_words=int(args.min_case_words),
        max_case_words=int(args.max_case_words),
        started_at=utc_now(),
        source_draft_sha256=sha256_file(draft_path),
        current_paragraph_id="",
        rewrite_items=[],
        rewrite_total=0,
        rewrite_completed=0,
        rewrite_accepted=0,
        rewrite_rejected=0,
        rewrite_deferred=0,
        deferred_paragraph_ids=[],
        baseline_full_evaluation_count=0,
        final_full_evaluation_count=0,
        changed_paragraph_count=0,
        error="",
    )
    final_evaluation: dict[str, Any] = {}
    final_preflight: dict[str, Any] = {}
    try:
        for iteration in range(1, int(args.max_iterations) + 1):
            if stopper.exists():
                restore_last_valid_state()
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="stopped",
                    phase="stopped",
                    iteration=iteration - 1,
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "stopped", "iteration": iteration - 1}
            iteration_dir = run_dir / f"iteration_{iteration:03d}"
            if iteration == 1:
                baseline_reused = False
                if bool(getattr(args, "reuse_baseline", False)):
                    try:
                        preflight, evaluation, gate, paragraphs, evidence = (
                            reusable_baseline_evaluation(
                                project,
                                artifact_dir=iteration_dir,
                                status_iteration=iteration,
                                args=args, rubric=rubric,
                            )
                        )
                        baseline_reused = True
                    except BaselineNotReusable as exc:
                        update_status(project, phase="baseline_refreshing", quality_reused=False,
                                      baseline_refresh_reason=str(exc), run_mode="baseline_expired")
                if not baseline_reused:
                    preflight, evaluation, gate, paragraphs, evidence = evaluate_current_draft(
                        review_root,
                        project,
                        args,
                        rubric,
                        iteration_dir,
                        status_iteration=iteration,
                    )
                    update_status(project, baseline_full_evaluation_count=1)
            else:
                evaluation = working_evaluation
                preflight = working_preflight
                evidence = working_evidence
                paragraphs = parse_marked_paragraphs(
                    draft_path.read_text(encoding="utf-8", errors="replace")
                )
                gate = queue_artifacts(project, evaluation, preflight)
                update_status(
                    project,
                    phase="evaluated_changed_paragraphs",
                    iteration=iteration,
                    score=evaluation.get("total_score"),
                    gate_decision=gate["gate_decision"],
                )
            final_evaluation, final_preflight = evaluation, preflight
            paragraph_scores = evaluation.get("paragraph_scores") or []
            if not source_evaluation:
                source_evaluation = json.loads(
                    json.dumps(evaluation, ensure_ascii=False)
                )
                source_preflight = json.loads(
                    json.dumps(preflight, ensure_ascii=False)
                )
                source_evaluation["preflight"] = json.loads(
                    json.dumps(source_preflight, ensure_ascii=False)
                )
                source_evaluation['source_check'] = original_source_check_report(project, evaluation, evidence)
                review_payload = write_batch_review_candidates(
                    batch_review_path,
                    project_id=args.project_id,
                    source_markdown=source_markdown,
                    source_evaluation=source_evaluation,
                    best_candidates=best_paragraph_candidates,
                    excluded=excluded_paragraph_candidates,
                    evaluated_markdown=draft_path.read_text(
                        encoding="utf-8", errors="replace"
                    ),
                    full_draft_evaluation=evaluation,
                )
            else:
                review_payload = write_batch_review_candidates(
                    batch_review_path,
                    project_id=args.project_id,
                    source_markdown=source_markdown,
                    source_evaluation=source_evaluation,
                    best_candidates=best_paragraph_candidates,
                    excluded=excluded_paragraph_candidates,
                    evaluated_markdown=draft_path.read_text(
                        encoding="utf-8", errors="replace"
                    ),
                    full_draft_evaluation=evaluation,
                )
            update_status(
                project,
                review_candidate_count=len(review_payload.get("changes") or []),
                review_candidate_score=review_payload.get("candidate_score"),
            )
            last_valid_draft = draft_path.read_bytes()
            last_valid_overlay = overlay_path.read_bytes() if overlay_path.is_file() else None
            score_value = float(evaluation["total_score"])
            if iteration == 1:
                remember_best_state(score_value, iteration, evaluation, preflight, evidence)
            if iteration == 1 and args.evaluate_only and evaluation_is_released(
                evaluation,
                goal=float(args.goal),
                paragraph_goal=float(args.paragraph_goal),
            ):
                gate["gate_decision"] = "GATE_RELEASE"
                gate["status"] = "RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH"
                write_json(first / "first_draft_gate_status.json", gate)
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="completed",
                    phase="released",
                    gate_decision="GATE_RELEASE",
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "released", "score": evaluation["total_score"], "iteration": iteration}
            if args.evaluate_only:
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="completed",
                    phase="evaluated",
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "evaluated", "score": evaluation["total_score"], "iteration": iteration}
            failures: list[dict[str, Any]] = []
            for item in evaluation.get("paragraph_failures") or []:
                paragraph_id = str(item.get("paragraph_id") or "")
                rewrite_mode = automatic_rewrite_mode(
                    item,
                    evidence.get(paragraph_id, {}),
                    paragraph_goal=float(args.paragraph_goal),
                )
                if rewrite_mode:
                    failures.append({**item, "automatic_rewrite_mode": rewrite_mode})
            preflight_checks = {
                str(item.get("paragraph_id") or ""): item
                for item in preflight.get("paragraph_checks") or []
                if isinstance(item, dict)
            }
            accepted = 0
            rejected = 0
            deferred = 0
            rewrite_items = [
                {
                    "paragraph_id": str(item.get("paragraph_id") or ""),
                    "status": "pending",
                    "route": str(item.get("route") or "section_rewrite"),
                    "score": item.get("score"),
                    "diagnosis": str(item.get("diagnosis") or ""),
                    "attempt": 0,
                }
                for item in failures
            ]
            update_status(
                project,
                phase="rewriting" if failures else "evaluated",
                current_paragraph_id="",
                rewrite_total=len(failures),
                rewrite_completed=0,
                rewrite_accepted=0,
                rewrite_rejected=0,
                rewrite_deferred=0,
                deferred_paragraph_ids=[],
                rewrite_items=rewrite_items,
            )
            checkpoint_rewrite_queue(
                iteration,
                rewrite_items,
                accepted=accepted,
                rejected=rejected,
                deferred=deferred,
            )
            for index, failure in enumerate(failures, 1):
                if stopper.exists():
                    restore_last_valid_state()
                    shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                    update_status(
                        project,
                        status="stopped",
                        phase="stopped",
                        current_paragraph_id="",
                        finished_at=utc_now(),
                        output_draft_sha256=sha256_file(draft_path),
                    )
                    return {"status": "stopped", "iteration": iteration}
                paragraph_id = str(failure["paragraph_id"])
                current_markdown = make_xml_compatible(
                    draft_path.read_text(encoding="utf-8", errors="replace")
                )[0]
                current_paragraph = next(
                    (item for item in parse_marked_paragraphs(current_markdown) if item["paragraph_id"] == paragraph_id),
                    None,
                )
                if not current_paragraph:
                    rewrite_items[index - 1]["status"] = "skipped"
                    rewrite_items[index - 1]["errors"] = ["paragraph_marker_missing"]
                    update_status(
                        project,
                        rewrite_completed=index,
                        rewrite_items=rewrite_items,
                    )
                    checkpoint_rewrite_queue(
                        iteration,
                        rewrite_items,
                        accepted=accepted,
                        rejected=rejected,
                        deferred=deferred,
                    )
                    continue
                fingerprint = repair_input_fingerprint(current_paragraph, failure, evidence.get(paragraph_id, {}),
                    constraints={"min_words": args.min_case_words, "max_words": args.max_case_words,
                                 "paragraph_goal": args.paragraph_goal, "model": provider_config().get("model")})
                previous_attempt = repair_history.get(fingerprint) or {}
                if previous_attempt.get("outcome") == "no_safe_change":
                    rewrite_items[index - 1].update(status="skipped", reason="unchanged_input_no_safe_improvement",
                                                  retryable=False, input_fingerprint=fingerprint)
                    update_status(project, rewrite_completed=index, rewrite_items=rewrite_items, repair_history=repair_history)
                    checkpoint_rewrite_queue(iteration, rewrite_items, accepted=accepted, rejected=rejected, deferred=deferred)
                    continue
                rewrite_items[index - 1]["status"] = "rewriting"
                update_status(
                    project,
                    phase="rewriting",
                    current_paragraph_id=paragraph_id,
                    rewrite_total=len(failures),
                    rewrite_completed=index - 1,
                    rewrite_attempt=0,
                    rewrite_attempts=MAX_REWRITE_ATTEMPTS,
                    rewrite_items=rewrite_items,
                )
                check = preflight_checks.get(paragraph_id, {})
                word_range_applicable = bool(check.get("word_range_applicable", True))
                effective_min_words = args.min_case_words if word_range_applicable else 1
                rewrite_mode = str(failure.get("automatic_rewrite_mode") or "section_rewrite")
                allowed_unsupported_claims = [
                    str(value)
                    for value in failure.get("unsupported_claims") or []
                    if str(value).strip()
                ]
                attempts: list[dict[str, Any]] = []
                candidate = ""
                validation_errors: list[str] = []
                validation_warnings: list[str] = []
                provider_error: BaseException | None = None
                for rewrite_attempt in range(1, MAX_REWRITE_ATTEMPTS + 1):
                    if stopper.exists():
                        break
                    rewrite_items[index - 1]["attempt"] = rewrite_attempt
                    update_status(
                        project,
                        rewrite_attempt=rewrite_attempt,
                        rewrite_items=rewrite_items,
                    )
                    prompt = (
                        rewrite_prompt(
                            current_paragraph,
                            failure,
                            evidence.get(paragraph_id, {}),
                            effective_min_words,
                            args.max_case_words,
                            word_range_applicable=word_range_applicable,
                            rewrite_mode=rewrite_mode,
                        )
                        if rewrite_attempt == 1
                        else rewrite_repair_prompt(
                            str(current_paragraph["text"]),
                            candidate,
                            validation_errors,
                            effective_min_words,
                            args.max_case_words,
                            word_range_applicable=word_range_applicable,
                            allowed_unsupported_claims=allowed_unsupported_claims,
                            evidence=evidence.get(paragraph_id, {}),
                            score=failure,
                            rewrite_mode=rewrite_mode,
                            repair_attempt=rewrite_attempt,
                        )
                    )
                    request_label = (
                        f"Paragraph rewrite {paragraph_id}"
                        if rewrite_attempt == 1
                        else f"Paragraph rewrite repair {paragraph_id}"
                    )
                    try:
                        try:
                            response = {'text': corrected_baseline(current_paragraph['text'], failure['source_corrections'])} if rewrite_mode == 'source_correction' else call_json_model(prompt, label=request_label)
                        except ProviderRequestBodyBudgetExceeded:
                            if rewrite_attempt != 1:
                                raise
                            rewrite_items[index - 1]["request_compacted"] = True
                            update_status(project, rewrite_items=rewrite_items)
                            response = call_json_model(
                                rewrite_prompt(
                                    current_paragraph,
                                    failure,
                                    evidence.get(paragraph_id, {}),
                                    effective_min_words,
                                    args.max_case_words,
                                    word_range_applicable=word_range_applicable,
                                    rewrite_mode=rewrite_mode,
                                    minimal_evidence=True,
                                ),
                                label=f"{request_label} compact retry",
                            )
                    except Exception as exc:
                        if not recoverable_paragraph_provider_failure(exc):
                            raise
                        provider_error = exc
                        attempts.append(
                            {
                                "attempt": rewrite_attempt,
                                "provider_error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        break
                    candidate = str(response.get("text") or "").strip()
                    validation_errors, validation_warnings = validate_rewrite_report(
                        str(current_paragraph["text"]),
                        candidate,
                        effective_min_words,
                        args.max_case_words,
                        source_corrections=failure.get('source_corrections'),
                        allowed_unsupported_claims=allowed_unsupported_claims,
                    )
                    attempts.append(
                        {
                            "attempt": rewrite_attempt,
                            "errors": validation_errors,
                            "warnings": validation_warnings,
                            "candidate": candidate,
                        }
                    )
                    if not validation_errors:
                        break
                if stopper.exists():
                    restore_last_valid_state()
                    shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                    checkpoint_rewrite_queue(
                        iteration,
                        rewrite_items,
                        accepted=accepted,
                        rejected=rejected,
                        deferred=deferred,
                        state="stopped",
                    )
                    update_status(
                        project,
                        status="stopped",
                        phase="stopped",
                        current_paragraph_id="",
                        finished_at=utc_now(),
                        output_draft_sha256=sha256_file(draft_path),
                    )
                    return {"status": "stopped", "iteration": iteration}
                if provider_error is not None:
                    deferred += 1
                    provider_message = f"{type(provider_error).__name__}: {provider_error}"
                    rewrite_items[index - 1]["status"] = "deferred"
                    rewrite_items[index - 1]["provider_error"] = provider_message
                    rewrite_items[index - 1]["retryable"] = True
                    write_json(
                        iteration_dir / f"{paragraph_id}_provider_deferred.json",
                        {
                            "paragraph_id": paragraph_id,
                            "status": "deferred",
                            "provider_error": provider_message,
                            "attempts": attempts,
                            "automatic_rewrite_mode": rewrite_mode,
                            "retryable": True,
                            "deferred_at": utc_now(),
                        },
                    )
                    deferred_ids = [
                        str(item.get("paragraph_id") or "")
                        for item in rewrite_items
                        if str(item.get("status") or "") == "deferred"
                    ]
                    update_status(
                        project,
                        rewrite_completed=index,
                        rewrite_accepted=accepted,
                        rewrite_rejected=rejected,
                        rewrite_deferred=deferred,
                        deferred_paragraph_ids=deferred_ids,
                        current_paragraph_id=paragraph_id,
                        rewrite_items=rewrite_items,
                    )
                    checkpoint_rewrite_queue(
                        iteration,
                        rewrite_items,
                        accepted=accepted,
                        rejected=rejected,
                        deferred=deferred,
                    )
                    continue
                if validation_errors:
                    record_repair(fingerprint, paragraph_id, "no_safe_change", len(attempts))
                    rejected += 1
                    rewrite_items[index - 1]["status"] = "rejected"
                    rewrite_items[index - 1]["errors"] = list(validation_errors)
                    write_json(
                        iteration_dir / f"{paragraph_id}_rejected.json",
                        {
                            "errors": validation_errors,
                            "warnings": validation_warnings,
                            "candidate": candidate,
                            "attempts": attempts,
                            "automatic_rewrite_mode": rewrite_mode,
                        },
                    )
                    update_status(
                        project,
                        rewrite_completed=index,
                        rewrite_rejected=rejected,
                        rewrite_deferred=deferred,
                        rewrite_items=rewrite_items,
                    )
                    checkpoint_rewrite_queue(
                        iteration,
                        rewrite_items,
                        accepted=accepted,
                        rejected=rejected,
                        deferred=deferred,
                    )
                    continue
                if clean_text(candidate) == clean_text(current_paragraph["text"]):
                    record_repair(fingerprint, paragraph_id, "no_safe_change", len(attempts))
                    rewrite_items[index - 1].update(status="skipped", reason="candidate_unchanged", input_fingerprint=fingerprint)
                    update_status(project, rewrite_completed=index, rewrite_items=rewrite_items)
                    checkpoint_rewrite_queue(iteration, rewrite_items, accepted=accepted, rejected=rejected, deferred=deferred)
                    continue
                record_repair(fingerprint, paragraph_id, "candidate_generated", len(attempts))
                rewrite_items[index - 1]["input_fingerprint"] = fingerprint
                snapshot = run_dir / f"before_{iteration:03d}_{paragraph_id}.md"
                shutil.copy2(draft_path, snapshot)
                updated = replace_paragraph_in_markdown(current_markdown, paragraph_id, candidate)
                temporary = draft_path.with_suffix(".md.feedback.tmp")
                temporary.write_text(make_xml_compatible(updated)[0], encoding="utf-8")
                temporary.replace(draft_path)
                try:
                    record_rewrite_overlay(
                        project,
                        paragraph_id,
                        str(current_paragraph["text"]),
                        candidate,
                    )
                except Exception:
                    shutil.copy2(snapshot, draft_path)
                    raise
                try:
                    record_paragraph_history(
                        project,
                        paragraph_id,
                        str(current_paragraph["text"]),
                        "update: accepted batch AI rewrite",
                    )
                except OSError:
                    # The immutable feedback run and overlay remain the source
                    # of truth even if the convenience history index is not writable.
                    pass
                accepted += 1
                rewrite_items[index - 1]["status"] = "completed"
                rewrite_items[index - 1]["attempt"] = rewrite_attempt
                rewrite_items[index - 1]["warnings"] = list(validation_warnings)
                rewrite_items[index - 1]["candidate_sha256"] = hashlib.sha256(
                    clean_text(candidate).encode("utf-8")
                ).hexdigest()
                write_json(
                    iteration_dir / f"{paragraph_id}_accepted.json",
                    {
                        "paragraph_id": paragraph_id,
                        "status": "completed",
                        "original_text": str(current_paragraph["text"]),
                        "candidate_text": candidate,
                        "candidate_sha256": rewrite_items[index - 1]["candidate_sha256"],
                        "warnings": validation_warnings,
                        "attempts": attempts,
                        "automatic_rewrite_mode": rewrite_mode,
                        "accepted_at": utc_now(),
                    },
                )
                update_status(
                    project,
                    rewrite_completed=index,
                    rewrite_accepted=accepted,
                    rewrite_rejected=rejected,
                    rewrite_deferred=deferred,
                    current_paragraph_id=paragraph_id,
                    rewrite_items=rewrite_items,
                )
                checkpoint_rewrite_queue(
                    iteration,
                    rewrite_items,
                    accepted=accepted,
                    rejected=rejected,
                    deferred=deferred,
                )
            deferred_ids = [
                str(item.get("paragraph_id") or "")
                for item in rewrite_items
                if str(item.get("status") or "") == "deferred"
            ]
            update_status(
                project,
                current_paragraph_id="",
                rewrite_attempt=0,
                rewrite_accepted=accepted,
                rewrite_rejected=rejected,
                rewrite_deferred=deferred,
                deferred_paragraph_ids=deferred_ids,
                rewrite_items=rewrite_items,
            )
            checkpoint_rewrite_queue(
                iteration,
                rewrite_items,
                accepted=accepted,
                rejected=rejected,
                deferred=deferred,
                state="iteration_completed",
            )
            if not accepted:
                if best_paragraph_candidates:
                    # Earlier safe candidates remain useful.  Stop widening
                    # the loop and let the one final full evaluation decide
                    # whether that subset can be published.
                    break
                restored_best = restore_best_scored_state()
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="repair_incomplete",
                    phase="provider_deferred" if deferred else "rewrite_blocked",
                    error=(
                        (
                            f"{deferred} paragraph rewrite(s) were deferred after transient provider failures. "
                            "Other paragraphs were processed; retry the deferred paragraphs when the provider recovers."
                        )
                        if deferred
                        else (
                            "No proposed rewrite passed the protected-fact and citation checks after "
                            f"up to {MAX_REWRITE_ATTEMPTS} attempts per paragraph."
                        )
                    ),
                    score=best_score if restored_best else score_value,
                    best_score=best_score,
                    best_iteration=best_iteration,
                    best_score_restored=restored_best,
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {
                    "status": "repair_incomplete",
                    "reason": "provider_deferred" if deferred else "no_safe_rewrite",
                    "score": best_score if restored_best else score_value,
                    "best_iteration": best_iteration,
                    "rewrite_deferred": deferred,
                    "deferred_paragraph_ids": deferred_ids,
                }

            changed_ids = {
                str(item.get("paragraph_id") or "")
                for item in rewrite_items
                if str(item.get("status") or "") == "completed"
            }
            candidate_preflight, candidate_evaluation, candidate_evidence = (
                evaluate_changed_paragraphs(
                    review_root,
                    project,
                    args,
                    rubric,
                    changed_ids,
                    iteration_dir,
                    global_dimension_scores=[
                        row
                        for row in source_evaluation.get("dimension_scores") or []
                        if isinstance(row, dict)
                        and str(row.get("id") or "") in global_dimension_ids
                    ],
                )
            )
            round_excluded = update_best_paragraph_candidates(
                best_paragraph_candidates,
                source_markdown=source_markdown,
                candidate_markdown=draft_path.read_text(
                    encoding="utf-8", errors="replace"
                ),
                source_evaluation=source_evaluation,
                candidate_evaluation=candidate_evaluation,
                source_preflight=source_preflight,
                candidate_preflight=candidate_preflight,
                candidate_evidence=candidate_evidence,
                min_words=int(args.min_case_words),
                max_words=int(args.max_case_words),
                iteration=iteration,
                blocked_paragraph_ids=blocked_paragraph_ids,
            )
            excluded_paragraph_candidates.extend(round_excluded)
            excluded_by_id = {
                str(item.get("paragraph_id") or ""): list(
                    item.get("reasons") or []
                )
                for item in round_excluded
                if str(item.get("paragraph_id") or "") in changed_ids
            }
            current_rows = {
                str(row.get("paragraph_id") or ""): row
                for row in parse_marked_paragraphs(
                    draft_path.read_text(encoding="utf-8", errors="replace")
                )
            }
            accepted = 0
            for item in rewrite_items:
                paragraph_id = str(item.get("paragraph_id") or "")
                if paragraph_id not in changed_ids:
                    continue
                selected = best_paragraph_candidates.get(paragraph_id)
                current_row = current_rows.get(paragraph_id, {})
                if selected and clean_text(selected.get("candidate_text")) == clean_text(
                    current_row.get("text")
                ):
                    accepted += 1
                    continue
                item["status"] = "rejected"
                item["errors"] = excluded_by_id.get(
                    paragraph_id, ["candidate_not_selected"]
                )
                fingerprint = str(item.get("input_fingerprint") or "")
                if fingerprint:
                    record_repair(
                        fingerprint,
                        paragraph_id,
                        "no_safe_change",
                        0,
                    )
            rejected += len(changed_ids) - accepted
            review_payload = write_batch_review_candidates(
                batch_review_path,
                project_id=args.project_id,
                source_markdown=source_markdown,
                source_evaluation=source_evaluation,
                best_candidates=best_paragraph_candidates,
                excluded=excluded_paragraph_candidates,
            )
            draft_path.write_text(
                str(review_payload["candidate_draft_text"]), encoding="utf-8"
            )
            working_evaluation = evaluation_with_best_candidates(
                source_evaluation,
                best_paragraph_candidates,
                paragraph_goal=float(args.paragraph_goal),
            )
            working_preflight = candidate_preflight
            working_evidence = {**evidence, **candidate_evidence}
            last_valid_draft = draft_path.read_bytes()
            last_valid_overlay = (
                overlay_path.read_bytes() if overlay_path.is_file() else None
            )
            update_status(
                project,
                phase="changed_paragraphs_evaluated",
                rewrite_accepted=accepted,
                rewrite_rejected=rejected,
                rewrite_deferred=deferred,
                rewrite_items=rewrite_items,
                changed_paragraph_count=len(changed_ids),
                review_candidate_count=len(review_payload.get("changes") or []),
                review_candidate_score=review_payload.get("candidate_score"),
            )
            checkpoint_rewrite_queue(
                iteration,
                rewrite_items,
                accepted=accepted,
                rejected=rejected,
                deferred=deferred,
                state="changed_paragraphs_evaluated",
            )
            if not accepted:
                break

        # Score the exact assembled safe subset once before API publication.
        update_status(
            project,
            phase="validating_full_draft",
            final_full_evaluation_count=1,
        )
        final_preflight, final_evaluation, final_gate, _paragraphs, final_evidence = evaluate_current_draft(
            review_root,
            project,
            args,
            rubric,
            run_dir / "final_evaluation",
            status_iteration=int(args.max_iterations),
        )
        last_valid_draft = draft_path.read_bytes()
        last_valid_overlay = overlay_path.read_bytes() if overlay_path.is_file() else None
        final_score = float(final_evaluation.get("total_score", 0))
        review_payload = write_batch_review_candidates(
            batch_review_path,
            project_id=args.project_id,
            source_markdown=source_markdown,
            source_evaluation=source_evaluation,
            best_candidates=best_paragraph_candidates,
            excluded=excluded_paragraph_candidates,
            evaluated_markdown=draft_path.read_text(
                encoding="utf-8", errors="replace"
            ),
            full_draft_evaluation=final_evaluation,
        )
        update_status(
            project,
            review_candidate_count=len(review_payload.get("changes") or []),
            review_candidate_score=review_payload.get("candidate_score"),
        )
        remember_best_state(
            final_score,
            int(args.max_iterations),
            final_evaluation,
            final_preflight,
            final_evidence,
        )
        if evaluation_is_released(
            final_evaluation,
            goal=float(args.goal),
            paragraph_goal=float(args.paragraph_goal),
        ):
            shutil.copy2(draft_path, run_dir / "first_draft_after.md")
            final_gate["gate_decision"] = "GATE_RELEASE"
            final_gate["status"] = "RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH"
            write_json(first / "first_draft_gate_status.json", final_gate)
            update_status(
                project,
                status="completed",
                phase="released",
                gate_decision="GATE_RELEASE",
                finished_at=utc_now(),
                output_draft_sha256=sha256_file(draft_path),
            )
            return {
                "status": "released",
                "score": final_evaluation["total_score"],
                "iteration": int(args.max_iterations),
            }
        shutil.copy2(draft_path, run_dir / "first_draft_after.md")
        update_status(
            project,
            status="repair_incomplete",
            phase="iteration_limit",
            error="The configured iteration limit was reached before the goal.",
            score=final_score,
            best_score=best_score,
            best_iteration=best_iteration,
            best_score_restored=False,
            finished_at=utc_now(),
            output_draft_sha256=sha256_file(draft_path),
        )
        return {
            "status": "repair_incomplete",
            "reason": "iteration_limit",
            "score": final_score,
            "best_iteration": best_iteration,
            "best_score_restored": False,
            "hard_gate_failures": final_preflight.get("hard_regressions", []),
        }
    except Exception as exc:
        # A transport or schema failure must not leave a partially rewritten
        # manuscript in the isolated workspace. Restore the most recent draft
        # that passed the preceding local or full validation checkpoint.
        restore_last_valid_state()
        shutil.copy2(draft_path, run_dir / "first_draft_after.md")
        update_status(
            project,
            status="failed",
            phase="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=utc_now(),
            output_draft_sha256=sha256_file(draft_path),
        )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--goal", type=float, default=DRAFT_PASS_THRESHOLD)
    parser.add_argument("--paragraph-goal", type=float, default=PARAGRAPH_PASS_THRESHOLD)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--min-improvement", type=float, default=1.0)
    parser.add_argument(
        "--min-case-words", type=int, default=CASE_PARAGRAPH_MIN_WORDS
    )
    parser.add_argument(
        "--max-case-words", type=int, default=CASE_PARAGRAPH_MAX_WORDS
    )
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument("--reuse-baseline", action="store_true")
    parser.add_argument("--local-revision", action="store_true",
                        help="Build joint argument/body proposals in the current Draft only.")
    args = parser.parse_args()
    if not 0 <= args.goal <= 100 or not 0 <= args.paragraph_goal <= 100:
        parser.error("Goals must be between 0 and 100.")
    if not 1 <= args.max_iterations <= 10:
        parser.error("max-iterations must be between 1 and 10.")
    if args.min_case_words < 1 or args.max_case_words < args.min_case_words:
        parser.error("Invalid case word range.")
    return args


def main() -> int:
    result = run_feedback_loop(parse_args())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
