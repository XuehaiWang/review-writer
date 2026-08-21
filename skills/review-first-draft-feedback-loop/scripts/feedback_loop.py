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

from review_writer_core.providers import (  # noqa: E402
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_TEXT_MODEL,
    DEFAULT_TEXT_WIRE_API,
    openai_endpoint,
)
from review_writer_core.paragraph_markers import (  # noqa: E402
    ensure_prose_paragraph_markers,
    split_body_and_references as shared_split_body_and_references,
)
from review_writer_core.text_safety import make_xml_compatible  # noqa: E402
from review_writer_core.publication_voice import publication_voice_issues  # noqa: E402


PARAGRAPH_MARKER_RE = re.compile(r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->")
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
INSERTED_FIGURE_RE = re.compile(r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.S)
REFERENCES_RE = re.compile(
    r"^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$",
    re.I | re.M,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
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
REWRITE_EVIDENCE_CHAR_BUDGET = 10_000
MINIMAL_REWRITE_EVIDENCE_CHAR_BUDGET = 4_000


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
CHEMICAL_SUFFIXES = (
    "acid",
    "alcohol",
    "aldehyde",
    "allene",
    "alkyne",
    "amine",
    "boronate",
    "bromide",
    "carbonate",
    "carbene",
    "chloride",
    "ester",
    "ether",
    "halide",
    "hydride",
    "ketone",
    "ligand",
    "oxide",
    "phosphine",
    "reagent",
    "sulfide",
)
CHEMICAL_ELEMENTS_AND_METALS = {
    "boron",
    "chromium",
    "cobalt",
    "copper",
    "fluorine",
    "indium",
    "iron",
    "manganese",
    "nickel",
    "palladium",
    "phosphorus",
    "sulfur",
    "titanium",
    "zinc",
}
EXPLICIT_CHEMICAL_SYMBOLS = {
    "Ag", "Al", "Au", "Co", "Cr", "Cu", "Fe", "Li", "Mg", "Mn",
    "Ni", "Pd", "Pt", "Sc", "Ti", "Zn",
}
SOFT_STEREO_RE = re.compile(
    r"\b(?:racemic|enantioselective|enantiospecific|diastereoselective|"
    r"stereoselective|stereospecific|axial chirality)\b",
    re.I,
)
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
CROSS_LANGUAGE_CHEMISTRY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gold", ("Au", "金催化", "金盐")),
    ("silver", ("Ag", "银催化", "银盐")),
    ("palladium", ("Pd", "钯催化", "零价钯", "二价钯")),
    ("pd(0)", ("Pd(0)", "零价钯")),
    ("pd(ii)", ("Pd(II)", "二价钯")),
    ("nickel", ("Ni", "镍催化", "镍盐")),
    ("copper", ("Cu", "铜催化", "铜盐")),
    ("water", ("水参与", "水合", "水")),
    ("dihydrofuran", ("二氢呋喃",)),
    ("epoxide", ("环氧", "环氧化合物")),
    ("allylic bromide", ("烯丙基溴", "烯丙基溴化物")),
    ("aminative", ("胺化", "氨基化")),
    ("amination", ("胺化", "氨基化")),
    ("three-component", ("三组分",)),
    ("three component", ("三组分",)),
    ("beta-hydrogen", ("β-H", "β-氢", "β-H消除")),
    ("β-hydrogen", ("β-H", "β-氢", "β-H消除")),
    ("elimination", ("消除反应", "消除")),
    ("dimerization", ("二聚反应", "二聚")),
    ("cyclization", ("环化反应", "环化")),
    ("carbonylation", ("羰基化",)),
    ("rearrangement", ("重排反应", "重排")),
    ("radical", ("自由基反应", "自由基")),
    ("chirality transfer", ("手性转移", "轴手性向中心手性转移")),
    ("axial-to-central", ("轴手性向中心手性转移", "轴手性", "中心手性")),
    ("allenol", ("联烯醇",)),
    ("allene", ("联烯",)),
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


def metadata_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def split_body_references(markdown: str) -> tuple[str, str]:
    return shared_split_body_and_references(markdown or "")


def parse_marked_paragraphs(markdown: str) -> list[dict[str, Any]]:
    """Read prose immediately before each terminating paragraph marker.

    Figures are inserted between the marker of their target paragraph and the
    prose of the following paragraph.  Treating the complete inter-marker span
    as one paragraph therefore assigns the preceding figure block to the next
    paragraph.  Besides distorting evaluation, that made a targeted rewrite
    send Markdown images and ``inserted_figure`` metadata through the model.

    Stage 8's API already defines a paragraph as the final blank-line-delimited
    prose block immediately before its marker.  Keep the skill parser aligned
    with that contract so generation, candidate scoring, and persistence all
    operate on exactly the same bytes.
    """
    body, _ = split_body_references(markdown)
    markers = list(PARAGRAPH_MARKER_RE.finditer(body))
    headings = list(HEADING_RE.finditer(body))
    paragraphs: list[dict[str, Any]] = []
    for marker in markers:
        prefix = body[: marker.start()].rstrip()
        end = len(prefix)
        start = prefix.rfind("\n\n") + 2
        text = body[start:end].strip()
        preceding = [heading for heading in headings if heading.end() <= start]
        heading = preceding[-1].group(2).strip() if preceding else ""
        if text and not text.lstrip().startswith(("#", "!", "|", "<!--")):
            paragraphs.append(
                {
                    "paragraph_id": marker.group(1),
                    "heading": heading,
                    "text": text,
                    "start": start,
                    "end": end,
                    "marker_end": marker.end(),
                }
            )
    return paragraphs


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
    return result


def citation_entries(project: Path) -> list[dict[str, Any]]:
    payload = read_json(project / "04_first_draft" / "citations.json", {})
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    return entries if isinstance(entries, list) else []


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
        for value in re.split(r"(?<=[.!?。！？])\s+|[;；]\s*", whole)
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
) -> list[dict[str, Any]]:
    protected = protected_signature(paragraph_text)
    protected_terms = set(
        protected["chemical_identities"]
        + protected["soft_chemical_terms"]
        + protected["numbers"]
        + protected["stereo"]
        + protected["soft_stereo_terms"]
    )
    blocks = document.get("blocks") or []
    passage_limit = (
        MAX_SOURCE_PASSAGES_PER_PAPER
        if cross_language_query_phrases(paragraph_text)
        and any(SOURCE_CJK_RE.search(str(block.get("text") or "")) for block in blocks)
        else 2
    )
    variants = source_query_variants(paragraph_text)
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
    for candidate in sorted(claim_leaders, key=lambda item: (-item[0], item[1])):
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
        text = clean_text(block.get("text") or "")[:MAX_SOURCE_PASSAGE_CHARS]
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
) -> dict[str, Any]:
    paper_ids = [
        str(value)
        for value in (structured.get("cited_paper_ids") or [structured.get("paper_id")])
        if value
    ]
    if not paper_ids:
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
    for paragraph in paragraphs:
        paragraph_id = str(paragraph["paragraph_id"])
        text = clean_text(paragraph["text"])
        words = len(text.split())
        structured_paragraph = structured.get(paragraph_id, {})
        word_range_applicable = bool(structured_paragraph)
        evidence = source_evidence(
            review_root,
            project,
            paragraph,
            structured_paragraph,
            rows,
            source_cache,
        )
        issues: list[str] = []
        if word_range_applicable and (words < min_words or words > max_words):
            issues.append("P01")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P01",
                    "severity": "major",
                    "diagnosis": f"Paragraph has {words} words; configured range is {min_words}-{max_words}.",
                    "route": "section_rewrite",
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
        sentences = [clean_text(value) for value in re.split(r"(?<=[.!?])\s+", text) if clean_text(value)]
        normalized = [re.sub(r"[^a-z0-9]", "", value.casefold()) for value in sentences]
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
                "paragraph_role": "case" if word_range_applicable else "supporting",
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
    if any(item["severity"] in {"critical", "major"} for item in findings):
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
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Feedback model returned no JSON object")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise RuntimeError("Feedback model JSON must be an object")
    return value


def call_json_model(prompt: str, *, label: str) -> dict[str, Any]:
    gateway_url = str(os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "").strip()
    task_token = str(os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    if gateway_url or task_token:
        if not gateway_url or not task_token:
            raise RuntimeError("Feedback loop received an incomplete internal gateway configuration.")
        request_key = f"{label[:32]}-{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:48]}"
        request = urllib.request.Request(
            gateway_url,
            data=json.dumps(
                {"request_key": request_key, "stage": label, "prompt": prompt},
                ensure_ascii=False,
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {task_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(
                    request, context=ssl.create_default_context(), timeout=330
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return extract_json_object(str(payload.get("output_text") or ""))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:800].replace("\n", " ")
                if request_body_budget_exhausted(body):
                    raise ProviderRequestBodyBudgetExceeded(
                        f"{label} exceeded the provider relay request-body budget"
                    ) from exc
                if exc.code in {504, 524}:
                    raise ProviderDeadlineExceeded(
                        f"{label} exceeded the provider deadline (HTTP {exc.code})"
                    ) from exc
                if exc.code not in TRANSIENT_HTTP_CODES or attempt >= 3:
                    raise RuntimeError(
                        f"{label} gateway failed with HTTP {exc.code} after {attempt} attempts: {body}"
                    ) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= 3:
                    raise RuntimeError(
                        f"{label} gateway transport/JSON failure after {attempt} attempts: {exc}"
                    ) from exc
            time.sleep(provider_retry_delay(attempt))
        raise RuntimeError(f"{label} gateway failed after 3 attempts")

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
            if request_body_budget_exhausted(body):
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


def compact_evidence_for_prompt(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep verifiable passages while omitting duplicated metadata prose."""

    compact_papers: list[dict[str, Any]] = []
    for paper in raw.get("evidence") or []:
        if not isinstance(paper, dict):
            continue
        passages = [
            {
                "ref": passage.get("ref"),
                "page": passage.get("page"),
                "text": clean_text(passage.get("text"))[:600],
            }
            for passage in (paper.get("original_passages") or [])[:3]
            if isinstance(passage, dict) and clean_text(passage.get("text"))
        ]
        compact_paper = {
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "local_source_available": bool(paper.get("local_source_available")),
            "original_text_available": bool(paper.get("original_text_available")),
            "original_passages": passages,
        }
        if not passages:
            compact_paper["metadata_fallback"] = clean_text(
                paper.get("abstract") or paper.get("main_content")
            )[:700]
        compact_papers.append(compact_paper)
    return {
        "paragraph_id": raw.get("paragraph_id"),
        "paper_ids": raw.get("paper_ids") or [],
        "local_source_available": bool(raw.get("local_source_available")),
        "original_source_ready": bool(raw.get("original_source_ready")),
        "evidence_scope": raw.get("evidence_scope"),
        "evidence": compact_papers,
    }


def compact_rewrite_evidence_for_prompt(
    raw: dict[str, Any],
    *,
    minimal: bool = False,
) -> dict[str, Any]:
    """Bound one rewrite request while retaining every cited paper identity.

    Introductory and synthesis paragraphs can cite many papers. Sending each
    paper's abstract, main-content summary, and several full passages can
    exceed a relay's request-body budget. Rewriting needs the paragraph plus
    concise evidence excerpts, not duplicate source records.
    """

    papers = [item for item in raw.get("evidence") or [] if isinstance(item, dict)]
    total_budget = (
        MINIMAL_REWRITE_EVIDENCE_CHAR_BUDGET
        if minimal
        else REWRITE_EVIDENCE_CHAR_BUDGET
    )
    per_paper_budget = max(
        120 if minimal else 180,
        min(260 if minimal else 520, total_budget // max(1, len(papers))),
    )
    compact_papers: list[dict[str, Any]] = []
    for paper in papers:
        remaining = per_paper_budget
        passages: list[dict[str, Any]] = []
        passage_limit = 1 if minimal else 2
        for passage in paper.get("original_passages") or []:
            if not isinstance(passage, dict) or len(passages) >= passage_limit:
                continue
            text = clean_text(passage.get("text"))
            if not text or remaining <= 0:
                continue
            excerpt = text[:remaining]
            remaining -= len(excerpt)
            passages.append(
                {
                    "ref": passage.get("ref"),
                    "page": passage.get("page"),
                    "text": excerpt,
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
                            dict.fromkeys(str(item.get("phrase") or "") for item in voice_issues)
                        )[:300]
                    ),
                    "route": "final_polish",
                }
            )
        compact_paper = {
            "paper_id": paper.get("paper_id"),
            "title": clean_text(paper.get("title"))[:80 if minimal else 160],
            "local_source_available": bool(paper.get("local_source_available")),
            "original_text_available": bool(paper.get("original_text_available")),
            "original_passages": passages,
        }
        if not passages:
            compact_paper["metadata_fallback"] = clean_text(
                paper.get("abstract") or paper.get("main_content")
            )[:per_paper_budget]
        compact_papers.append(compact_paper)
    return {
        "paragraph_id": raw.get("paragraph_id"),
        "paper_ids": raw.get("paper_ids") or [],
        "local_source_available": bool(raw.get("local_source_available")),
        "original_source_ready": bool(raw.get("original_source_ready")),
        "evidence_scope": raw.get("evidence_scope"),
        "evidence": compact_papers,
    }


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
) -> str:
    paragraph_ids = {str(item["paragraph_id"]) for item in paragraphs}
    compact_paragraphs = [
        {
            "paragraph_id": item["paragraph_id"],
            "heading": item.get("heading", ""),
            "text": clean_text(item["text"]),
            "source_evidence": compact_evidence_for_prompt(
                evidence.get(str(item["paragraph_id"]), {})
            ),
        }
        for item in paragraphs
    ]
    return (
        "Act as a detect-first scientific review evaluator. Do not rewrite text. "
        f"This is scoring batch {batch_index} of {batch_total}. Score the complete rubric at levels 0-4 "
        "against the supplied batch and score every supplied marked paragraph on a 0-100 scale. "
        "Use the draft structure index to preserve whole-draft order and section context. Batch results will be "
        "combined deterministically, so do not refer to paragraphs that are absent from this batch. "
        "Treat deterministic preflight findings as binding. Do not penalize a paragraph merely for passive voice. "
        "A protected-fact conflict must route to local_source_recheck or human_confirmation, never automatic invention. "
        "Original-source checking is part of this evaluation. For each paragraph, compare its factual claims with the "
        "retrieved original_passages. Return source_check_status "
        "(verified|partially_supported|unsupported|needs_human_review|not_applicable), source_evidence_refs using only the "
        "provided passage refs, and unsupported_claims. Treat absence from retrieved excerpts as needs_human_review, not "
        "as contradiction. Use local_source_recheck only when original text is unavailable or the retrieved passages are "
        "insufficient; otherwise route wording corrections to section_rewrite or final_polish. "
        "The configured case-paragraph word range applies only where deterministic preflight marks "
        "word_range_applicable=true. Supporting, transition, caption-adjacent, introduction, and synthesis prose must not "
        "fail P01 solely because it is shorter than a case paragraph. "
        "Return JSON with dimension_scores and paragraph_scores. dimension_scores must include every rubric id exactly once "
        "with id, level, evidence. paragraph_scores must include every paragraph exactly once with paragraph_id, score, "
        "failed_dimensions, severity (none|minor|major|critical), diagnosis, route "
        "(pass|section_rewrite|local_source_recheck|final_polish|human_confirmation), source_check_status, "
        "source_evidence_refs, and unsupported_claims. Keep each dimension evidence under 30 words, each diagnosis under "
        "60 words, and unsupported_claims to at most four concise items.\n\n"
        f"Overall goal: {goal}; paragraph goal: {paragraph_goal}.\n"
        f"Draft structure index: {json.dumps(draft_structure or [], ensure_ascii=False)}\n"
        f"Rubric: {json.dumps(rubric, ensure_ascii=False)}\n"
        f"Deterministic preflight: {json.dumps(compact_preflight_for_prompt(preflight, paragraph_ids), ensure_ascii=False)}\n"
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
) -> dict[str, Any]:
    """Merge independently bounded model calls into one normalization input."""

    expected_dimensions = [str(item["id"]) for item in rubric_dimensions(rubric)]
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

    merged_dimensions = []
    for dimension_id in expected_dimensions:
        levels = dimension_levels[dimension_id]
        if not levels:
            raise RuntimeError(f"Feedback scoring did not assess rubric dimension {dimension_id}")
        weighted_level = sum(level * weight for level, weight in levels) / sum(
            weight for _level, weight in levels
        )
        evidence_text = " | ".join(dimension_evidence[dimension_id][:3])[:900]
        merged_dimensions.append(
            {
                "id": dimension_id,
                "level": round(weighted_level, 3),
                "evidence": evidence_text,
            }
        )
    return {
        "dimension_scores": merged_dimensions,
        "paragraph_scores": paragraph_scores,
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
    for paragraph_id in paragraph_ids:
        item = score_by_id[paragraph_id]
        score = max(0.0, min(100.0, float(item.get("score", 0))))
        binding = preflight_by_id.get(paragraph_id, [])
        if binding:
            score = min(score, 79.0)
        severity = str(item.get("severity") or ("major" if binding else "none")).casefold()
        if severity not in {"none", "minor", "major", "critical"}:
            severity = "major" if binding or score < paragraph_goal else "none"
        if binding and severity in {"none", "minor"}:
            severity = "major"
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
        source_check_status = str(item.get("source_check_status") or "not_assessed").casefold()
        if source_check_status not in {
            "verified",
            "partially_supported",
            "unsupported",
            "needs_human_review",
            "not_applicable",
            "not_assessed",
        }:
            source_check_status = "not_assessed"
        source_evidence_refs = [
            clean_text(value)
            for value in item.get("source_evidence_refs") or []
            if clean_text(value)
        ][:12]
        unsupported_claims = [
            clean_text(value)
            for value in item.get("unsupported_claims") or []
            if clean_text(value)
        ][:12]
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
        if not paper_ids:
            source_check_status = "not_applicable"
        elif source_ready and (
            source_check_status in {"not_assessed", "verified", "partially_supported", "unsupported"}
            and not source_evidence_refs
        ):
            source_check_status = "needs_human_review"
        if source_check_status == "needs_human_review":
            route = "local_source_recheck"
            severity = "major"
            score = min(score, 79.0)
        elif source_check_status == "unsupported":
            route = "section_rewrite"
            severity = "major"
            score = min(score, 79.0)
        elif source_check_status == "partially_supported" and route == "pass":
            route = "section_rewrite"
            severity = "major"
            score = min(score, 79.0)
        record = {
            "paragraph_id": paragraph_id,
            "score": round(score, 2),
            "failed_dimensions": failed,
            "severity": severity,
            "diagnosis": clean_text(item.get("diagnosis") or "; ".join(str(f.get("diagnosis")) for f in binding)),
            "route": route,
            "source_check_status": source_check_status,
            "source_evidence_refs": source_evidence_refs,
            "unsupported_claims": unsupported_claims,
        }
        paragraph_scores.append(record)
        if route != "pass" or severity in {"critical", "major"}:
            paragraph_failures.append(record)
    hard = sorted(set(preflight.get("hard_regressions") or []))
    blocking_paragraph_failures = [
        item
        for item in paragraph_scores
        if float(item.get("score", 0)) < paragraph_goal
        or item.get("severity") in {"critical", "major"}
        or item.get("route") not in {"pass", "final_polish"}
    ]
    decision = (
        "PASS"
        if total >= goal and not hard and not blocking_paragraph_failures
        else "REGENERATE_SECTIONS"
    )
    return {
        "rubric_model": str(rubric.get("name") or "readability_first_unified_review_rubric"),
        "pass_threshold": goal,
        "paragraph_pass_threshold": paragraph_goal,
        "total_score": round(total, 2),
        "decision": decision,
        "dimension_scores": normalized_dimensions,
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


def protected_signature(text: str) -> dict[str, list[str]]:
    prose = protection_prose(text)
    return {
        # Citation order is binding; [1] and [2] may not trade places.
        "callouts": [match.group(0) for match in CALLOUT_RE.finditer(prose)],
        "numbers": [match.group(0).casefold() for match in PROTECTED_NUMBER_RE.finditer(prose)],
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
        f"Required word range for this case paragraph: {min_words}-{max_words}. Aim at least "
        f"{min(max_words, min_words + 20)} words so minor tokenization differences do not fail validation."
        if word_range_applicable
        else (
            f"This is supporting prose, not a case paragraph. Keep it concise and no longer than {max_words} words; "
            "do not pad it to the case-paragraph minimum."
        )
    )
    mode_instruction = {
        "source_recheck_cleanup": (
            "This paragraph is in original-source recheck. Retain claims supported by the supplied passages and remove "
            "or explicitly qualify only the listed unsupported_claims. Absence from a passage is not evidence that a "
            "claim is false. Do not describe the paragraph as source-verified."
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
) -> str:
    """Ask once for a mechanical repair without relaxing protected-fact checks."""
    protected = protected_signature(original)
    hard_protected = {
        key: value for key, value in protected.items() if key in HARD_PROTECTED_FIELDS
    }
    soft_protected = {
        key: value for key, value in protected.items() if key in SOFT_PROTECTED_FIELDS
    }
    length_instruction = (
        f"The corrected paragraph must contain {min_words}-{max_words} whitespace-delimited words; "
        f"aim for {min(max_words, min_words + 25)}-{min(max_words, min_words + 60)} words."
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
    return (
        "Repair one rejected scientific-review rewrite. Return JSON {\"text\": \"...\"} only. "
        "Do not add facts or use chemical identities from supplied evidence unless they already occur in the original. "
        f"{protection_instruction} "
        f"{length_instruction}\n"
        f"Validation errors to fix: {json.dumps(validation_errors, ensure_ascii=False)}\n"
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
        errors.append(f"word_count_{words}_outside_{min_words}_{max_words}")
    before, after = protected_signature(original), protected_signature(candidate)
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
) -> list[dict[str, Any]]:
    """Keep only individually safer, higher-scoring paragraph candidates."""

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
    source_check_entries: dict[str, dict[str, Any]] = {}
    for paragraph_id, paragraph_evidence in candidate_evidence.items():
        score = candidate_scores.get(paragraph_id, {})
        source_check_entries[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "paper_ids": paragraph_evidence.get("paper_ids") or [],
            "evidence_scope": paragraph_evidence.get("evidence_scope"),
            "source_check_status": score.get(
                "source_check_status", "not_assessed"
            ),
            "source_evidence_refs": score.get("source_evidence_refs") or [],
            "unsupported_claims": score.get("unsupported_claims") or [],
            "route": score.get("route"),
        }
    excluded: list[dict[str, Any]] = []
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
        word_range_applicable = bool(
            source_checks.get(paragraph_id, {}).get("word_range_applicable", True)
        )
        errors, warnings = validate_rewrite_report(
            original_text,
            candidate_text,
            min_words if word_range_applicable else 1,
            max_words,
            allowed_unsupported_claims=[
                str(value)
                for value in source_score.get("unsupported_claims") or []
                if str(value).strip()
            ],
        )
        old_score = float(source_score.get("score") or 0)
        new_score = float(candidate_score.get("score") or 0)
        if errors or new_score <= old_score:
            excluded.append(
                {
                    "paragraph_id": paragraph_id,
                    "source_paragraph_score": round(old_score, 2),
                    "candidate_paragraph_score": round(new_score, 2),
                    "reasons": errors or ["candidate_score_not_improved"],
                    "iteration": iteration,
                }
            )
            continue
        previous = best_candidates.get(paragraph_id)
        if previous and float(previous.get("candidate_paragraph_score") or 0) >= new_score:
            continue
        local_preflight = _paragraph_preflight(candidate_preflight, paragraph_id)
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
            "iteration": iteration,
            "validation_warnings": warnings,
            "candidate_evaluation": {
                "schema_version": 1,
                "evaluation_scope": "single_paragraph",
                "evaluation_mode": "batch_candidate",
                "paragraph_id": paragraph_id,
                "paragraph_score": dict(candidate_score),
                "local_hard_gate_failures": [],
                "local_preflight": local_preflight,
                "source_check_entry": dict(source_check_entries.get(paragraph_id) or {}),
                "validation_warnings": warnings,
                "evaluated_at": utc_now(),
            },
        }
    return excluded


def write_batch_review_candidates(
    path: Path,
    *,
    project_id: str,
    source_markdown: str,
    source_evaluation: dict[str, Any],
    best_candidates: dict[str, dict[str, Any]],
    excluded: list[dict[str, Any]],
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
        "created_at": utc_now(),
    }
    write_json(path, payload)
    return payload


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
            "id": f"PAR-{index:03d}",
            "reviewer": "rubric_evaluator",
            "severity": item["severity"],
            "paragraph_id": item["paragraph_id"],
            "location": item["paragraph_id"],
            "fragment": "",
            "diagnosis": item["diagnosis"],
            "recommended_direction": "Rewrite only this marked paragraph while preserving protected facts.",
            "confidence": "high",
            "route": item["route"],
        }
        for index, item in enumerate(evaluation.get("paragraph_failures") or [], 1)
    ]


def original_source_check_report(
    project: Path,
    evaluation: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
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
                    "passages": paper.get("original_passages") or [],
                }
            )
        entries.append(
            {
                "paragraph_id": paragraph_id,
                "paper_ids": paragraph_evidence.get("paper_ids") or [],
                "evidence_scope": paragraph_evidence.get("evidence_scope"),
                "source_check_status": score.get("source_check_status", "not_assessed"),
                "source_evidence_refs": score.get("source_evidence_refs") or [],
                "unsupported_claims": score.get("unsupported_claims") or [],
                "route": score.get("route"),
                "papers": papers,
            }
        )
    counts = Counter(str(item.get("source_check_status") or "not_assessed") for item in entries)
    return {
        "schema_version": 1,
        "project_id": project.name,
        "generated_at": utc_now(),
        "draft_sha256": sha256_file(project / "04_first_draft" / "first_draft.md"),
        "counts": dict(sorted(counts.items())),
        "entries": entries,
    }


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
            if item.get("route") == "final_polish"
            and str(item.get("paragraph_id") or "") not in blocking_ids
            else rewrite
        )
        target.append({"origin": "rubric", **item})
    score = float(evaluation.get("total_score", 0))
    goal = float(evaluation.get("pass_threshold", 90))
    hard = sorted(set(evaluation.get("hard_gate_failures") or []) | set(preflight.get("hard_regressions") or []))
    released = evaluation.get("decision") == "PASS" and score >= goal and not hard and not rewrite
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
    update_status(project, phase="source_checking")
    evidence = {
        str(paragraph["paragraph_id"]): source_evidence(
            review_root,
            project,
            paragraph,
            structured.get(str(paragraph["paragraph_id"]), {}),
            rows,
            source_cache,
        )
        for paragraph in paragraphs
    }
    update_status(
        project,
        phase="scoring",
        paragraph_total=len(paragraphs),
        paragraph_completed=0,
    )
    batches = paragraph_batches(paragraphs)
    draft_structure = [
        {
            "paragraph_id": str(item["paragraph_id"]),
            "heading": clean_text(item.get("heading")),
        }
        for item in paragraphs
    ]
    raw_batches: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    completed = 0
    update_status(
        project,
        scoring_batch_total=len(batches),
        scoring_batch_completed=0,
    )
    batch_index = 0
    timeout_splits = 0
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
                    float(args.goal),
                    float(args.paragraph_goal),
                    batch_index=display_index,
                    batch_total=len(batches),
                    draft_structure=draft_structure,
                ),
                label=f"First-draft rubric evaluation batch {display_index}/{len(batches)}",
            )
        except ProviderDeadlineExceeded as exc:
            if len(batch) <= 1:
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
                scoring_batch_completed=len(raw_batches),
                scoring_timeout_splits=timeout_splits,
            )
            continue
        raw_batches.append((batch, raw_batch))
        completed += len(batch)
        batch_index += 1
        update_status(
            project,
            paragraph_completed=completed,
            scoring_batch_completed=len(raw_batches),
            scoring_batch_total=len(batches),
        )
    raw = merge_batched_evaluations(rubric, raw_batches)
    evaluation = normalize_evaluation(
        raw,
        rubric,
        paragraphs,
        preflight,
        float(args.goal),
        float(args.paragraph_goal),
        evidence=evidence,
    )
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


def evaluation_is_released(
    evaluation: dict[str, Any],
    *,
    goal: float,
    paragraph_goal: float,
) -> bool:
    paragraph_scores = evaluation.get("paragraph_scores") or []
    return bool(
        paragraph_scores
        and float(evaluation.get("total_score", 0)) >= goal
        and not evaluation.get("hard_gate_failures")
        and all(
            float(item.get("score", 0)) >= paragraph_goal
            and item.get("route") in {"pass", "final_polish"}
            and item.get("severity") not in {"critical", "major"}
            for item in paragraph_scores
        )
    )


def automatic_rewrite_mode(
    finding: dict[str, Any],
    paragraph_evidence: dict[str, Any],
    *,
    paragraph_goal: float,
) -> str:
    """Select only rewrites that can be made without inventing missing evidence."""
    if float(finding.get("score", 0)) >= paragraph_goal:
        return ""
    route = str(finding.get("route") or "")
    if route == "section_rewrite":
        return "section_rewrite"
    if route != "local_source_recheck" or not (finding.get("unsupported_claims") or []):
        return ""
    source_status = str(finding.get("source_check_status") or "")
    paper_ids = paragraph_evidence.get("paper_ids") or []
    if source_status == "not_applicable" and not paper_ids:
        return "review_synthesis_cleanup"
    original_text_available = any(
        bool(item.get("original_text_available"))
        for item in paragraph_evidence.get("evidence") or []
        if isinstance(item, dict)
    )
    if source_status in {"partially_supported", "needs_human_review"} and original_text_available:
        return "source_recheck_cleanup"
    return ""


def interactive_rewrite_mode(
    finding: dict[str, Any],
    paragraph_evidence: dict[str, Any],
    *,
    paragraph_goal: float,
) -> str:
    """Select a safe mode for an explicitly requested paragraph candidate.

    Automatic batch rewriting must keep holding source/figure identity conflicts
    for a human. An explicit UI request may still produce a reviewable,
    style-only candidate, provided it does not claim to resolve that conflict.
    """

    mode = automatic_rewrite_mode(
        finding,
        paragraph_evidence,
        paragraph_goal=paragraph_goal,
    )
    if mode:
        return mode
    route = str(finding.get("route") or "")
    if route in {"human_confirmation", "local_source_recheck"}:
        return "human_review_style_only"
    if route == "section_rewrite":
        return "section_rewrite"
    if route == "final_polish":
        return "final_polish"
    return ""


def run_feedback_loop(args: argparse.Namespace) -> dict[str, Any]:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    if not draft_path.is_file():
        raise FileNotFoundError(draft_path)
    rubric_path = Path(__file__).resolve().parents[1] / "references" / "unified_rubric.json"
    rubric = read_json(rubric_path, {})
    rubric_dimensions(rubric)
    rubric_threshold = float(rubric.get("pass_threshold", 90))
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
    active_rewrite_checkpoint = first / "feedback_loop_rewrite_checkpoint.json"

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
        error="",
    )
    plateau_count = 0
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
            preflight, evaluation, gate, paragraphs, evidence = evaluate_current_draft(
                review_root,
                project,
                args,
                rubric,
                iteration_dir,
                status_iteration=iteration,
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
                review_payload = write_batch_review_candidates(
                    batch_review_path,
                    project_id=args.project_id,
                    source_markdown=source_markdown,
                    source_evaluation=source_evaluation,
                    best_candidates=best_paragraph_candidates,
                    excluded=excluded_paragraph_candidates,
                )
            else:
                excluded_paragraph_candidates.extend(
                    update_best_paragraph_candidates(
                        best_paragraph_candidates,
                        source_markdown=source_markdown,
                        candidate_markdown=draft_path.read_text(
                            encoding="utf-8", errors="replace"
                        ),
                        source_evaluation=source_evaluation,
                        candidate_evaluation=evaluation,
                        source_preflight=source_preflight,
                        candidate_preflight=preflight,
                        candidate_evidence=evidence,
                        min_words=int(args.min_case_words),
                        max_words=int(args.max_case_words),
                        iteration=iteration,
                    )
                )
                review_payload = write_batch_review_candidates(
                    batch_review_path,
                    project_id=args.project_id,
                    source_markdown=source_markdown,
                    source_evaluation=source_evaluation,
                    best_candidates=best_paragraph_candidates,
                    excluded=excluded_paragraph_candidates,
                )
            update_status(
                project,
                review_candidate_count=len(review_payload.get("changes") or []),
                review_candidate_score=review_payload.get("candidate_score"),
            )
            last_valid_draft = draft_path.read_bytes()
            last_valid_overlay = overlay_path.read_bytes() if overlay_path.is_file() else None
            score_value = float(evaluation["total_score"])
            previous_best_score = best_score
            remember_best_state(score_value, iteration, evaluation, preflight, evidence)
            if evaluation_is_released(
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
            if previous_best_score >= 0 and score_value - previous_best_score < float(args.min_improvement):
                plateau_count += 1
            else:
                plateau_count = 0
            if plateau_count >= 2:
                restored_best = restore_best_scored_state()
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="needs_human_review",
                    phase="plateau",
                    error="The score stopped improving across two iterations.",
                    score=best_score if restored_best else score_value,
                    best_score=best_score,
                    best_iteration=best_iteration,
                    best_score_restored=restored_best,
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {
                    "status": "needs_human_review",
                    "reason": "plateau",
                    "score": best_score if restored_best else score_value,
                    "best_iteration": best_iteration,
                }

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
                        )
                    )
                    request_label = (
                        f"Paragraph rewrite {paragraph_id}"
                        if rewrite_attempt == 1
                        else f"Paragraph rewrite repair {paragraph_id}"
                    )
                    try:
                        try:
                            response = call_json_model(prompt, label=request_label)
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
                restored_best = restore_best_scored_state()
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="needs_human_review",
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
                    "status": "needs_human_review",
                    "reason": "provider_deferred" if deferred else "no_safe_rewrite",
                    "score": best_score if restored_best else score_value,
                    "best_iteration": best_iteration,
                    "rewrite_deferred": deferred,
                    "deferred_paragraph_ids": deferred_ids,
                }
        # The final configured rewrite round changes the draft after its normal
        # evaluation. Score those exact output bytes once more before reporting
        # a goal result or an iteration-limit hold.
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
        excluded_paragraph_candidates.extend(
            update_best_paragraph_candidates(
                best_paragraph_candidates,
                source_markdown=source_markdown,
                candidate_markdown=draft_path.read_text(
                    encoding="utf-8", errors="replace"
                ),
                source_evaluation=source_evaluation,
                candidate_evaluation=final_evaluation,
                source_preflight=source_preflight,
                candidate_preflight=final_preflight,
                candidate_evidence=final_evidence,
                min_words=int(args.min_case_words),
                max_words=int(args.max_case_words),
                iteration=int(args.max_iterations) + 1,
            )
        )
        review_payload = write_batch_review_candidates(
            batch_review_path,
            project_id=args.project_id,
            source_markdown=source_markdown,
            source_evaluation=source_evaluation,
            best_candidates=best_paragraph_candidates,
            excluded=excluded_paragraph_candidates,
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
        restored_best = final_score < best_score and restore_best_scored_state()
        shutil.copy2(draft_path, run_dir / "first_draft_after.md")
        update_status(
            project,
            status="needs_human_review",
            phase="iteration_limit",
            error="The configured iteration limit was reached before the goal.",
            score=best_score if restored_best else final_score,
            best_score=best_score,
            best_iteration=best_iteration,
            best_score_restored=restored_best,
            finished_at=utc_now(),
            output_draft_sha256=sha256_file(draft_path),
        )
        return {
            "status": "needs_human_review",
            "reason": "iteration_limit",
            "score": best_score if restored_best else final_score,
            "best_iteration": best_iteration,
            "best_score_restored": restored_best,
            "hard_gate_failures": (
                best_preflight if restored_best else final_preflight
            ).get("hard_regressions", []),
        }
    except Exception as exc:
        # A transport or schema failure must not leave a partially rewritten
        # manuscript paired with an older score. Restore the most recent draft
        # that completed a full rubric evaluation, together with its overlay.
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
    parser.add_argument("--goal", type=float, default=90.0)
    parser.add_argument("--paragraph-goal", type=float, default=85.0)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--min-improvement", type=float, default=1.0)
    parser.add_argument("--min-case-words", type=int, default=140)
    parser.add_argument("--max-case-words", type=int, default=280)
    parser.add_argument("--evaluate-only", action="store_true")
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
