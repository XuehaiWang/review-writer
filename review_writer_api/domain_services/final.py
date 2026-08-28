"""PostgreSQL-native conclusion, overview, final build, validation, and export."""

from __future__ import annotations

import html
import json
import re
import shutil
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from typing import Any

from PIL import UnidentifiedImageError
from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import User, database_session, utc_now
from review_writer_api.domain_services.drafts import (
    DRAFT_APPROVAL,
    DRAFT_DOCUMENT,
    DRAFT_QUALITY,
    DraftsService,
    _clean_reference_doi,
    _clean_reference_field,
)
from review_writer_api.domain_services.discovery import discovery_search_record
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.figure_rules import image_size
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_api.workflow_repository import ArtifactRecord, WorkflowRepository
from review_writer_core.latex_renderer import SUPPORTED_PROFILES, TEMPLATE_VERSION
from review_writer_core.draft_bibliography import (
    citation_entries_from_draft,
    expand_callouts,
    ordered_callouts,
    reference_text,
)
from review_writer_core.manuscript_state import build_manuscript_state
from review_writer_core.markdown_images import (
    malformed_markdown_image_lines,
    parse_markdown_image,
)
from review_writer_core.publication_voice import publication_voice_issues
from review_writer_core.review_titles import (
    build_publication_overview_text,
    build_publication_review_title,
    generated_title_is_acceptable,
    generated_title_needs_rewrite,
    overview_text_needs_rewrite,
)
from review_writer_core.review_structure import sanitize_internal_section_title


FINAL_CONCLUSION = "final/conclusion.md"
FINAL_CONCLUSION_REPORT = "final/conclusion-report.json"
FINAL_OVERVIEW_IMAGE = "final/overview.png"
FINAL_OVERVIEW_TEXT = "final/overview-text.json"
FINAL_FRONT_MATTER = "final/front-matter.json"
FINAL_DRAFT = "final/manuscript.md"
FINAL_VALIDATION = "final/validation.json"
FINAL_RELEASE = "final/release.json"
FINAL_DOCX = "final/manuscript.docx"
FINAL_DOCX_QA = "final/docx-qa.json"
FINAL_MANUSCRIPT_STATE = "final/manuscript_state.json"
FINAL_RENDER_MANIFEST = "final/render_manifest.json"
FINAL_TEX = "final/manuscript.tex"
FINAL_PDF = "final/manuscript.pdf"
FINAL_PDF_QA = "final/pdf-qa.json"
FINAL_PDF_COMPILE_LOG = "final/pdf-compile.log"
DISCOVERY_LOGICAL_NAME = "discovery/review.json"
BLUEPRINT_LOGICAL_NAME = "blueprint/section_blueprint.json"
ARTIFACT_URL = re.compile(r"/api/v1/artifacts/([0-9a-fA-F-]{36})/content")
REFERENCES_HEADING = re.compile(
    r"(?im)^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$"
)
CITATION_CALLOUT = re.compile(r"\[([0-9][0-9,;\s-]*)\]")
REFERENCE_ITEM = re.compile(r"(?m)^\s*\[(\d+)\]\s*\.?\s+(.+?)\s*$")
MARKDOWN_HEADING = re.compile(r"(?m)^\s*(#{1,6})\s+(.+?)\s*$")
INTRODUCTION_TITLES = ("introduction", "background", "引言", "绪论", "研究背景")
REFERENCE_AFFILIATION_SUP = re.compile(
    r"<sup\b[^>]*>[\s,;:.·•*†‡#∥‖|\[\](){}\-]*</sup>",
    re.IGNORECASE,
)
HTML_COMMENT_BLOCK = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_DANGEROUS_BLOCK = re.compile(
    r"<(?:script|style|iframe|object)\b[^>]*>.*?</(?:script|style|iframe|object)\s*>",
    re.IGNORECASE | re.DOTALL,
)
HTML_SUP = re.compile(r"<sup\b[^>]*>(.*?)</sup\s*>", re.IGNORECASE | re.DOTALL)
HTML_SUB = re.compile(r"<sub\b[^>]*>(.*?)</sub\s*>", re.IGNORECASE | re.DOTALL)
HTML_BREAK = re.compile(r"<br\b[^>]*?/?>", re.IGNORECASE)
HTML_BLOCK_BOUNDARY = re.compile(
    r"</?(?:p|div|section|article|header|footer|blockquote|ul|ol|li|table|thead|tbody|tr|h[1-6])\b[^>]*>",
    re.IGNORECASE,
)
HTML_ANY_TAG = re.compile(r"</?[A-Za-z][^>]*>")
HTML_STRONG = re.compile(r"<(?:strong|b)\b[^>]*>(.*?)</(?:strong|b)\s*>", re.IGNORECASE | re.DOTALL)
HTML_EMPHASIS = re.compile(r"<(?:em|i)\b[^>]*>(.*?)</(?:em|i)\s*>", re.IGNORECASE | re.DOTALL)
HTML_CODE = re.compile(r"<code\b[^>]*>(.*?)</code\s*>", re.IGNORECASE | re.DOTALL)
INSERTED_FIGURE_METADATA = re.compile(
    r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.DOTALL
)
REFERENCE_WEB_RESIDUE = re.compile(
    r"\b(?:Cite\s+This|Read\s+Online|Article\s+Recommendations|Supporting\s+Information)\b",
    re.IGNORECASE,
)
TEMPLATE_RESIDUE_LINE = re.compile(
    r"^\s*(?:[*_`>#-]+\s*)?(?:Review\s+Writer(?:\s*[|·-]\s*modern-survey/\d+)?|modern-survey/\d+)(?:\s*[*_`]+)?\s*$",
    re.IGNORECASE,
)


def _clean_reference_affiliation_markup(markdown: str) -> str:
    """Remove empty author-affiliation superscripts without touching scientific markup."""

    reference_match = REFERENCES_HEADING.search(markdown or "")
    if reference_match is None:
        return markdown
    reference_text = markdown[reference_match.start() :]
    cleaned = REFERENCE_AFFILIATION_SUP.sub("", reference_text)
    cleaned = "\n".join(
        re.sub(r"[ \t]{2,}", " ", line) for line in cleaned.split("\n")
    )
    return markdown[: reference_match.start()] + cleaned


SUPERSCRIPT_CHARS = str.maketrans(
    "0123456789+-=()n",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ",
)
SUBSCRIPT_CHARS = str.maketrans(
    {
        **{source: target for source, target in zip("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")},
        **{source: target for source, target in zip("aehijklmnoprstx", "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜₓ")},
    }
)


def _safe_script_text(value: str, *, superscript: bool) -> str:
    text = re.sub(r"<[^>]+>", "", html.unescape(str(value or ""))).strip()
    if not text:
        return ""
    table = SUPERSCRIPT_CHARS if superscript else SUBSCRIPT_CHARS
    converted = text.translate(table)
    convertible = all(
        character.isspace() or character.translate(table) != character
        for character in text
    )
    if convertible:
        return converted
    marker = "^" if superscript else "_"
    return f"{marker}({text})"


def _normalize_publication_markup(markdown: str) -> str:
    """Convert harmless HTML formatting while preserving workflow comments."""

    source = _clean_reference_affiliation_markup(str(markdown or ""))
    # A replacement character is evidence that an upstream byte could not be
    # decoded.  It has no publishable meaning, so remove it while retaining the
    # surrounding text instead of blocking all exports.
    source = source.replace("\ufffd", "")
    comments: list[str] = []

    def stash_comment(match: re.Match[str]) -> str:
        comments.append(match.group(0))
        return f"\x00RWCOMMENT{len(comments) - 1}\x00"

    normalized = HTML_COMMENT_BLOCK.sub(stash_comment, source)
    normalized = HTML_DANGEROUS_BLOCK.sub("", normalized)
    for _pass in range(2):
        normalized = HTML_STRONG.sub(lambda match: f"**{match.group(1).strip()}**", normalized)
        normalized = HTML_EMPHASIS.sub(lambda match: f"*{match.group(1).strip()}*", normalized)
        normalized = HTML_CODE.sub(lambda match: f"`{match.group(1).strip()}`", normalized)
        normalized = HTML_SUP.sub(
            lambda match: _safe_script_text(match.group(1), superscript=True),
            normalized,
        )
        normalized = HTML_SUB.sub(
            lambda match: _safe_script_text(match.group(1), superscript=False),
            normalized,
        )
        normalized = HTML_BREAK.sub("\n", normalized)
        normalized = HTML_BLOCK_BOUNDARY.sub("\n", normalized)
        normalized = HTML_ANY_TAG.sub("", normalized)
        normalized = html.unescape(normalized)
    cleaned_lines: list[str] = []
    in_references = False
    for line in normalized.split("\n"):
        if re.match(
            r"^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$",
            line,
            re.IGNORECASE,
        ):
            in_references = True
        if TEMPLATE_RESIDUE_LINE.match(line):
            continue
        if in_references:
            line = REFERENCE_WEB_RESIDUE.sub("", line)
        cleaned_lines.append(re.sub(r"[ \t]{2,}", " ", line).rstrip())
    normalized = "\n".join(cleaned_lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    for index, comment in enumerate(comments):
        normalized = normalized.replace(f"\x00RWCOMMENT{index}\x00", comment)
    return normalized


def _figure_argument_findings(markdown: str) -> list[dict[str, Any]]:
    """Check reader-visible figure closure without treating hidden metadata as a callout."""

    findings: list[dict[str, Any]] = []
    parsed_image_sources = {
        image.source
        for line in str(markdown or "").splitlines()
        if (image := parse_markdown_image(line)) is not None
    }
    for match in INSERTED_FIGURE_METADATA.finditer(markdown or ""):
        try:
            metadata = json.loads(match.group(1))
        except json.JSONDecodeError:
            findings.append({"figure_id": "", "issues": ["invalid_inserted_figure_metadata"]})
            continue
        if not isinstance(metadata, dict):
            continue
        figure_id = str(metadata.get("figure_id") or "")
        published_label = str(metadata.get("published_label") or "").strip()
        output_artifact_id = str(metadata.get("output_artifact_id") or "")
        issues: list[str] = []
        if not published_label:
            issues.append("published_label_missing")
        escaped_label = re.escape(published_label)
        expected_source = f"/api/v1/artifacts/{output_artifact_id}/content"
        if output_artifact_id and expected_source not in parsed_image_sources:
            issues.append("image_missing")
        if published_label and not re.search(rf"\*{escaped_label}\.", markdown):
            issues.append("caption_missing")
        visible_prose = INSERTED_FIGURE_METADATA.sub("", markdown)
        visible_prose = "\n".join(
            "" if parse_markdown_image(line) is not None else line
            for line in visible_prose.splitlines()
        )
        visible_prose = re.sub(r"(?m)^\s*\*Figure\s+\d+\..*?\*\s*$", "", visible_prose)
        if published_label and not re.search(
            rf"\b{escaped_label}\b[^\n]{{0,500}}\b(?:presents?|shows?|summarizes?|illustrates?|compares?|depicts?)\b",
            visible_prose,
            re.IGNORECASE,
        ):
            issues.append("visible_callout_or_interpretation_missing")
        if not str(metadata.get("paper_id") or "").strip():
            issues.append("source_paper_identity_missing")
        if str(metadata.get("interpretation_basis") or "") != "source_caption":
            issues.append("paper_level_interpretation_missing")
        if (
            str(metadata.get("source_relationship") or "") == "source_attributed"
            and str(metadata.get("permission_status") or "") != "verified"
        ):
            issues.append("source_reuse_permission_unverified")
        if issues:
            findings.append(
                {
                    "figure_id": figure_id,
                    "published_label": published_label,
                    "paper_id": str(metadata.get("paper_id") or ""),
                    "issues": issues,
                }
            )
    return findings


class FinalNotReady(WorkflowConflict):
    code = "FINAL_NOT_READY"


class FinalService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        drafts: DraftsService,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.drafts = drafts
        self._write_lock = threading.RLock()

    def _artifact(self, principal: Principal, project_id: str, logical_name: str):
        principal.require(Permission.PROJECT_READ)
        if self.repository.get_owned_project(principal.user_id, project_id) is None:
            raise WorkflowNotFound("Project not found.")
        return self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )

    def _read_text(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = False,
    ) -> tuple[str, ArtifactRecord | None]:
        artifact = self._artifact(principal, project_id, logical_name)
        if artifact is None:
            if required:
                raise WorkflowNotFound("Current workflow artifact not found.")
            return "", None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        return resolved.path.read_text(encoding="utf-8"), artifact

    def _read_json(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
    ) -> tuple[dict[str, Any], ArtifactRecord | None]:
        text, artifact = self._read_text(principal, project_id, logical_name)
        if artifact is None:
            return {}, None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkflowConflict("The current workflow artifact is invalid.") from exc
        if not isinstance(value, dict):
            raise WorkflowConflict("The current workflow artifact is invalid.")
        return value, artifact

    def _canonical_reference_section(
        self,
        principal: Principal,
        paper_ids: list[str],
        matrix_rows: list[dict[str, Any]],
        *,
        reference_numbers: dict[str, int] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Rebuild references from the latest canonical Library metadata.

        Draft prose and citation numbers remain immutable, while a later
        high-confidence bibliography correction can still reach the Final
        manuscript without forcing scientific paragraphs to be regenerated.
        """

        explicit_numbers = {
            str(paper_id).strip(): int(number)
            for paper_id, number in (reference_numbers or {}).items()
            if str(paper_id).strip() and int(number) > 0
        }
        ordered = list(dict.fromkeys(str(value) for value in paper_ids if str(value)))
        if explicit_numbers:
            ordered.sort(key=lambda paper_id: explicit_numbers.get(paper_id, 10**9))
        if not ordered:
            return "", {}
        with database_session(self.repository.session_factory) as session:
            rows = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(ordered),
                    LibraryPaper.status == "active",
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
        live = {row.paper_id: row for row in rows}
        fallback = {
            str(row.get("paper_id") or ""): row
            for row in matrix_rows
            if isinstance(row, dict) and str(row.get("paper_id") or "")
        }
        artifact_ids: dict[str, str] = {}
        references = ["## References"]
        for fallback_number, paper_id in enumerate(ordered, start=1):
            number = explicit_numbers.get(paper_id, fallback_number)
            library_row = live.get(paper_id)
            metadata = (
                dict(library_row.metadata_json or {})
                if library_row is not None
                else dict(fallback.get(paper_id) or {})
            )

            references.append(
                f"[{number}] {reference_text(metadata, fallback=paper_id)}"
            )
            if library_row is not None:
                metadata_artifact_id = str(
                    ((library_row.metadata_json or {}).get("_artifact_ids") or {}).get(
                        "metadata"
                    )
                    or ""
                )
                if metadata_artifact_id:
                    artifact_ids[paper_id] = metadata_artifact_id
        return "\n".join(references), artifact_ids

    @staticmethod
    def _reference_ledger(
        draft_markdown: str,
        quality: dict[str, Any],
        section_index: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve numeric callouts to Paper IDs without guessing from section order."""

        repaired = quality.get("reference_repair")
        candidate = (
            dict(repaired)
            if isinstance(repaired, dict) and repaired.get("entries")
            else citation_entries_from_draft(draft_markdown, section_index)
        )
        body = REFERENCES_HEADING.split(str(draft_markdown or ""), maxsplit=1)[0]
        used = ordered_callouts(body)
        by_number: dict[int, str] = {}
        conflicts = [
            dict(value)
            for value in candidate.get("conflicts") or []
            if isinstance(value, dict)
        ]
        for item in candidate.get("entries") or []:
            if not isinstance(item, dict):
                continue
            try:
                number = int(item.get("callout"))
            except (TypeError, ValueError):
                continue
            paper_id = str(item.get("paper_id") or "").strip()
            if number <= 0 or not paper_id:
                continue
            previous = by_number.get(number)
            if previous and previous != paper_id:
                conflicts.append(
                    {
                        "callout": number,
                        "paper_ids": [previous, paper_id],
                        "reason": "callout_maps_to_multiple_papers",
                    }
                )
                continue
            by_number[number] = paper_id
        paper_numbers: dict[str, list[int]] = {}
        for number, paper_id in by_number.items():
            paper_numbers.setdefault(paper_id, []).append(number)
        for paper_id, numbers in paper_numbers.items():
            if len(numbers) > 1:
                conflicts.append(
                    {
                        "paper_id": paper_id,
                        "callouts": sorted(numbers),
                        "reason": "paper_maps_to_multiple_callouts",
                    }
                )
        unresolved = sorted(
            {
                *(
                    int(value)
                    for value in candidate.get("unresolved_callouts") or []
                    if str(value).isdigit()
                ),
                *(number for number in used if number not in by_number),
            }
        )
        ordered_entries = [
            {"callout": number, "paper_id": by_number[number]}
            for number in used
            if number in by_number
        ]
        complete = bool(used) and not unresolved and not conflicts and len(ordered_entries) == len(used)
        return {
            "status": "resolved" if complete else "unresolved",
            "complete": complete,
            "used_callouts": used,
            "entries": ordered_entries,
            "unresolved_callouts": unresolved,
            "conflicts": conflicts,
            "source": (
                "draft_quality_reference_repair"
                if isinstance(repaired, dict) and repaired.get("entries")
                else "section_index_recovery"
            ),
        }

    @staticmethod
    def _render_final_citation_numbers(
        markdown: str,
        ledger: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Render display numbers from Paper IDs in final first-citation order."""

        if not ledger.get("complete"):
            return markdown, dict(ledger)
        old_to_paper = {
            int(item["callout"]): str(item["paper_id"])
            for item in ledger.get("entries") or []
            if isinstance(item, dict)
            and str(item.get("callout") or "").isdigit()
            and str(item.get("paper_id") or "").strip()
        }
        used = ordered_callouts(markdown)
        if any(number not in old_to_paper for number in used):
            unresolved = [number for number in used if number not in old_to_paper]
            failed = dict(ledger)
            failed.update(
                {
                    "status": "unresolved",
                    "complete": False,
                    "unresolved_callouts": unresolved,
                }
            )
            return markdown, failed
        paper_order: list[str] = []
        for number in used:
            paper_id = old_to_paper[number]
            if paper_id not in paper_order:
                paper_order.append(paper_id)
        paper_to_number = {
            paper_id: index for index, paper_id in enumerate(paper_order, start=1)
        }
        old_to_new = {
            old: paper_to_number[paper_id] for old, paper_id in old_to_paper.items()
        }

        def replace_group(match: re.Match[str]) -> str:
            rendered: list[int] = []
            for old in expand_callouts(match.group(1)):
                new = old_to_new.get(old)
                if new is not None and new not in rendered:
                    rendered.append(new)
            return (
                "[" + ", ".join(map(str, rendered)) + "]"
                if rendered
                else match.group(0)
            )

        updated = CITATION_CALLOUT.sub(replace_group, markdown)
        rendered_ledger = dict(ledger)
        rendered_ledger.update(
            {
                "status": "resolved",
                "complete": True,
                "numbering_basis": "final_first_citation_order",
                "old_to_new": {str(key): value for key, value in old_to_new.items()},
                "entries": [
                    {
                        "callout": paper_to_number[paper_id],
                        "paper_id": paper_id,
                        "source_callouts": sorted(
                            old
                            for old, mapped_paper in old_to_paper.items()
                            if mapped_paper == paper_id
                        ),
                    }
                    for paper_id in paper_order
                ],
                "used_callouts": list(range(1, len(paper_order) + 1)),
            }
        )
        return updated, rendered_ledger

    def _bibliography_identity_report(
        self,
        principal: Principal,
        paper_ids: list[str],
    ) -> dict[str, Any]:
        """Summarize canonical publication-identity readiness for Final release."""

        normalized = list(
            dict.fromkeys(
                str(value).strip() for value in paper_ids if str(value).strip()
            )
        )
        if not normalized:
            return {"papers": [], "unresolved_paper_ids": [], "verified_count": 0}
        with database_session(self.repository.session_factory) as session:
            rows = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(normalized),
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
            report_rows: list[dict[str, Any]] = []
            by_id = {row.paper_id: row for row in rows}
            for paper_id in normalized:
                row = by_id.get(paper_id)
                audit = (
                    dict(row.bibliography_audit_row.audit_json or {})
                    if row is not None and row.bibliography_audit_row is not None
                    else {}
                )
                status = str(audit.get("status") or "not_audited")
                manually_resolved = str(audit.get("manual_review_status") or "") in {
                    "approved",
                    "resolved",
                    "verified",
                }
                supporting_resolved = bool(
                    str(audit.get("manual_review_status") or "") == "supporting_only"
                    and audit.get("direct_claim_eligible") is True
                    and not bool(audit.get("context_only"))
                )
                verified = bool(
                    row is not None
                    and (status == "verified" or manually_resolved or supporting_resolved)
                )
                report_rows.append(
                    {
                        "paper_id": paper_id,
                        "status": status if row is not None else "missing",
                        "verified": verified,
                        "bibliography_role": str(
                            audit.get("bibliography_role") or "primary"
                        ),
                        "direct_claim_eligible": bool(
                            audit.get("direct_claim_eligible", True)
                        ),
                        "context_only": bool(audit.get("context_only", False)),
                        "verification_method": str(audit.get("verification_method") or ""),
                        "unresolved_conflict_count": len(audit.get("unresolved_conflicts") or []),
                    }
                )
        unresolved = [row["paper_id"] for row in report_rows if not row["verified"]]
        return {
            "papers": report_rows,
            "unresolved_paper_ids": unresolved,
            "verified_count": len(report_rows) - len(unresolved),
            "total_count": len(report_rows),
        }

    @staticmethod
    def _claim_citation_mapping_report(
        compatibility: dict[str, Any],
        reference_ledger: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate Claim→evidence→paper identity independently of display numbers."""

        evidence_registry = {
            str(row.get("evidence_key") or ""): row
            for row in (
                (compatibility.get("section_evidence") or {}).get(
                    "evidence_registry"
                )
                or []
            )
            if isinstance(row, dict) and str(row.get("evidence_key") or "")
        }
        ledger_papers = {
            str(row.get("paper_id") or "")
            for row in reference_ledger.get("entries") or []
            if isinstance(row, dict) and str(row.get("paper_id") or "")
        }
        issues: list[dict[str, Any]] = []
        claim_count = 0
        for section in (
            (compatibility.get("writing_plan") or {}).get("sections") or []
        ):
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("section_id") or "")
            for claim in section.get("claims") or []:
                if not isinstance(claim, dict):
                    continue
                claim_count += 1
                claim_id = str(claim.get("claim_id") or "")
                cited = {
                    str(value)
                    for value in claim.get("citation_group") or []
                    if str(value).strip()
                }
                evidence_keys = {
                    str(ref.get("evidence_key") or "")
                    for ref in claim.get("evidence_refs") or []
                    if isinstance(ref, dict) and str(ref.get("evidence_key") or "")
                }
                missing_evidence_keys = sorted(evidence_keys - set(evidence_registry))
                evidence_papers = {
                    str(evidence_registry[key].get("paper_id") or "")
                    for key in evidence_keys & set(evidence_registry)
                    if str(evidence_registry[key].get("paper_id") or "")
                }
                unsupported_citations = sorted(cited - evidence_papers)
                unrendered_papers = sorted(cited - ledger_papers)
                claim_issues: list[str] = []
                if not evidence_keys:
                    claim_issues.append("claim_has_no_evidence_identity")
                if missing_evidence_keys:
                    claim_issues.append("claim_evidence_identity_missing")
                if unsupported_citations:
                    claim_issues.append("claim_cites_paper_outside_evidence")
                if unrendered_papers:
                    claim_issues.append("claim_paper_missing_from_reference_ledger")
                if claim_issues:
                    issues.append(
                        {
                            "section_id": section_id,
                            "claim_id": claim_id,
                            "issues": claim_issues,
                            "citation_paper_ids": sorted(cited),
                            "evidence_paper_ids": sorted(evidence_papers),
                            "missing_evidence_keys": missing_evidence_keys,
                            "unsupported_citation_paper_ids": unsupported_citations,
                            "unrendered_paper_ids": unrendered_papers,
                        }
                    )
        return {
            "status": "pass" if not issues else "failed",
            "claim_count": claim_count,
            "issue_count": len(issues),
            "issues": issues,
        }

    def _publish_files(
        self,
        principal: Principal,
        project_id: str,
        files: dict[str, tuple[bytes, str]],
        *,
        expected_revision: int,
        metadata: dict[str, Any],
        status: str = "review",
        expected_current_artifacts: dict[str, str] | None = None,
        expected_stage_states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            "final",
            status="succeeded",
            input_snapshot=metadata,
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        published: dict[str, ArtifactRecord] = {}
        for index, (logical_name, (content, artifact_type)) in enumerate(files.items()):
            suffix = Path(logical_name).suffix or ".bin"
            filename = f"{index:03d}-{uuid.uuid4().hex}{suffix}"
            (staging / filename).write_bytes(content)
            published[logical_name] = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                filename,
                logical_name=logical_name,
                artifact_type=artifact_type,
                producer_stage="final",
                make_current=False,
                metadata=metadata,
            )
        state = self.repository.promote_stage_artifacts_atomically(
            principal.user_id,
            project_id,
            "final",
            artifact_ids={name: record.id for name, record in published.items()},
            run_id=run.id,
            expected_revision=expected_revision,
            status=status,
            expected_current_artifacts=expected_current_artifacts,
            expected_stage_states=expected_stage_states,
        )
        return published, state

    def _approved_draft(
        self, principal: Principal, project_id: str
    ) -> tuple[str, ArtifactRecord, dict[str, Any]]:
        draft_payload = self.drafts.get(principal, project_id)
        if not draft_payload.get("draft_approval_current"):
            raise FinalNotReady("Human-approve the exact current Draft before Final work.")
        text, artifact = self._read_text(
            principal, project_id, DRAFT_DOCUMENT, required=True
        )
        approval, _approval_artifact = self._read_json(
            principal, project_id, DRAFT_APPROVAL
        )
        if approval.get("draft_artifact_id") != artifact.id:
            raise FinalNotReady("Draft approval is stale.")
        return text, artifact, approval

    @staticmethod
    def _insert_before_introduction(markdown: str, block: str) -> str:
        """Place a front-of-article artifact immediately before Introduction."""

        normalized_block = str(block or "").strip()
        body = str(markdown or "").rstrip()
        if not normalized_block:
            return body
        headings = list(MARKDOWN_HEADING.finditer(body))
        insertion: int | None = None
        for match in headings:
            title = re.sub(
                r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", match.group(2)
            ).strip().casefold()
            if any(
                title == candidate
                or any(
                    title.startswith(f"{candidate}{separator}")
                    for separator in (" ", ":", "：", "与", "和")
                )
                for candidate in INTRODUCTION_TITLES
            ):
                insertion = match.start()
                break
        if insertion is None and headings and len(headings[0].group(1)) == 1:
            insertion = headings[0].end()
        if insertion is None:
            insertion = 0
        before = body[:insertion].rstrip()
        after = body[insertion:].lstrip()
        return "\n\n".join(
            value for value in (before, normalized_block, after) if value
        )

    @staticmethod
    def _remove_review_methods(markdown: str) -> str:
        """Keep workflow provenance internal instead of publishing it as prose."""

        body = str(markdown or "").rstrip()
        heading = re.compile(
            r"(?im)^\s*(#{1,6})\s*"
            r"(?:\d+(?:\.\d+)*[.)]?\s*)?"
            r"(?:review methods?|methods? of this review|综述方法|检索方法)\s*$"
        )
        while match := heading.search(body):
            level = len(match.group(1))
            next_heading = next(
                (
                    candidate
                    for candidate in MARKDOWN_HEADING.finditer(body, match.end())
                    if len(candidate.group(1)) <= level
                ),
                None,
            )
            end = next_heading.start() if next_heading is not None else len(body)
            body = "\n\n".join(
                value
                for value in (
                    body[: match.start()].rstrip(),
                    body[end:].lstrip(),
                )
                if value
            )
        return body

    @staticmethod
    def _sanitize_internal_section_headings(markdown: str) -> str:
        """Remove classification-workflow labels from legacy manuscript headings."""

        def replace(match: re.Match[str]) -> str:
            return (
                f"{match.group(1)} "
                f"{sanitize_internal_section_title(match.group(2))}"
            )

        return MARKDOWN_HEADING.sub(replace, str(markdown or ""))

    @staticmethod
    def _overview_semantic_report(
        overview_text: dict[str, Any],
        discovery: dict[str, Any],
    ) -> dict[str, Any]:
        """Reject prompt/template residue without inventing domain labels."""

        title = " ".join(str(overview_text.get("title") or "").split()).strip()
        topic = " ".join(str(discovery.get("topic") or "").split()).strip()
        labels = [
            " ".join(str(value).split()).strip()
            for value in overview_text.get("labels") or []
            if str(value).strip()
        ]
        issues: list[str] = []
        if title and generated_title_needs_rewrite(title, topic):
            issues.append("overview_title_is_prompt_or_not_publication_ready")
        subtitle = " ".join(
            str(overview_text.get("subtitle") or "").split()
        ).strip()
        residue = re.compile(
            r"(?:please\s+write\s+(?:an?\s+)?review|module[-_ ]cards?|"
            r"crosscut[-_ ]sidebar|modern[-_ ]survey|review\s+writer|"
            r"prompt\s*:|\b(?:reaction_type|group_by|layout_type|"
            r"taxonomy_profile|catalyst_or_method)\b)",
            re.I,
        )
        raw_topic_leaked = bool(
            topic and len(topic) > 70 and topic.casefold() in " ".join(
                [title, subtitle, *labels]
            ).casefold()
        )
        if residue.search(" ".join([title, subtitle])) or raw_topic_leaked:
            issues.append("overview_caption_contains_internal_residue")
        unsupported_labels = [
            value
            for value in labels
            if residue.search(value) or len(value) > 100 or len(value.split()) > 14
        ]
        if unsupported_labels:
            issues.append("overview_unsupported_labels")
        return {
            "status": "invalid" if issues else "aligned",
            "issues": issues,
            "unsupported_labels": unsupported_labels,
            "title": title,
        }


    @classmethod
    def _apply_front_matter(
        cls, markdown: str, front_matter: dict[str, Any]
    ) -> str:
        body = str(markdown or "").rstrip()
        title = " ".join(str(front_matter.get("title") or "").split()).strip()
        if title:
            if re.search(r"(?m)^#\s+.+$", body):
                body = re.sub(r"(?m)^#\s+.+$", f"# {title}", body, count=1)
            else:
                body = f"# {title}\n\n{body}"
        lines: list[str] = []
        authors = [
            " ".join(str(value).split()).strip()
            for value in front_matter.get("authors") or []
            if str(value).strip()
        ]
        affiliations = [
            " ".join(str(value).split()).strip()
            for value in front_matter.get("affiliations") or []
            if str(value).strip()
        ]
        abstract = str(front_matter.get("abstract") or "").strip()
        keywords = [
            " ".join(str(value).split()).strip()
            for value in front_matter.get("keywords") or []
            if str(value).strip()
        ]
        if authors:
            lines.append(f"**Authors:** {', '.join(authors)}")
        if affiliations:
            lines.append(f"**Affiliations:** {'; '.join(affiliations)}")
        if abstract:
            lines.extend(("## Abstract", abstract))
        if keywords:
            lines.append(f"**Keywords:** {', '.join(keywords)}")
        return cls._insert_before_introduction(body, "\n\n".join(lines))

    @staticmethod
    def _default_front_matter(
        markdown: str,
        *,
        fallback_title: str = "",
        author_candidate: str = "",
        source_draft_artifact_id: str = "",
    ) -> dict[str, Any]:
        title_match = re.search(r"(?m)^#\s+(.+?)\s*$", str(markdown or ""))
        author = " ".join(str(author_candidate or "").split()).strip()
        manuscript_title = title_match.group(1).strip() if title_match else ""
        title = build_publication_review_title(
            fallback_title or manuscript_title,
            manuscript_title=manuscript_title,
        )
        return {
            "schema_version": 2,
            "title": title or "Untitled Review",
            "authors": [author] if author else [],
            "affiliations": [],
            "abstract": "",
            "keywords": [],
            "source": "system-default",
            "source_draft_artifact_id": source_draft_artifact_id,
            "field_states": {
                "title": "generated",
                "authors": "generated" if author else "missing",
                "affiliations": "missing",
                "abstract": "missing",
                "keywords": "missing",
            },
            "field_source_draft_artifact_ids": {
                "title": source_draft_artifact_id,
                "authors": source_draft_artifact_id if author else "",
                "affiliations": "",
                "abstract": "",
                "keywords": "",
            },
            "generation_warnings": [],
        }

    def _author_candidate(self, principal: Principal) -> str:
        display_name = " ".join(str(principal.display_name or "").split()).strip()
        if display_name:
            return display_name
        try:
            user_id = uuid.UUID(principal.user_id)
        except ValueError:
            return ""
        with database_session(self.repository.session_factory) as session:
            user = session.get(User, user_id)
            return " ".join(str(user.display_name or "").split()).strip() if user else ""

    @staticmethod
    def _abstract_source(markdown: str) -> str:
        """Return body evidence only; conclusion-like sections never feed Abstract."""

        source = str(markdown or "")
        reference_match = REFERENCES_HEADING.search(source)
        if reference_match:
            source = source[: reference_match.start()]
        excluded = re.compile(
            r"^(?:conclusion|conclusions|challenges?|future directions?|"
            r"outlook|references|bibliography|publication notes?|结论|挑战|未来展望|参考文献)\b",
            re.IGNORECASE,
        )
        lines: list[str] = []
        skip_level: int | None = None
        for line in source.splitlines():
            heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                level = len(heading.group(1))
                title = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", heading.group(2)).strip()
                if excluded.match(title):
                    skip_level = level
                    continue
                if skip_level is not None and level <= skip_level:
                    skip_level = None
            if skip_level is not None:
                continue
            if line.lstrip().startswith("<!--") or parse_markdown_image(line):
                continue
            if re.match(r"^\s*\*(?:Figure|Fig\.|Scheme|Table|图|表)\s*\d+", line, re.I):
                continue
            if re.search(r"unresolved placeholder|publication note", line, re.I):
                continue
            lines.append(line)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    def save_front_matter(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        title: str,
        authors: list[str],
        affiliations: list[str],
        abstract: str,
        keywords: list[str],
        omitted_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        _text, draft, _approval = self._approved_draft(principal, project_id)
        current_value, current = self._read_json(
            principal, project_id, FINAL_FRONT_MATTER
        )
        submitted = {
            "title": " ".join(str(title).split()).strip(),
            "authors": [
                " ".join(str(item).split()).strip()
                for item in authors
                if str(item).strip()
            ],
            "affiliations": [
                " ".join(str(item).split()).strip()
                for item in affiliations
                if str(item).strip()
            ],
            "abstract": str(abstract).strip(),
            "keywords": [
                " ".join(str(item).split()).strip()
                for item in keywords
                if str(item).strip()
            ],
        }
        omitted = {
            str(field)
            for field in omitted_fields or []
            if str(field) in {"authors", "affiliations", "abstract", "keywords"}
        }
        previous_states = dict(current_value.get("field_states") or {})
        field_states: dict[str, str] = {}
        for field in ("title", "authors", "affiliations", "abstract", "keywords"):
            if field in omitted:
                submitted[field] = [] if field in {"authors", "affiliations", "keywords"} else ""
                field_states[field] = "user_omitted"
            elif field != "title" and not submitted.get(field):
                field_states[field] = "missing"
            elif current is None or current_value.get(field) != submitted.get(field):
                field_states[field] = "user_modified"
            else:
                field_states[field] = str(previous_states.get(field) or "user_modified")
        value = {
            "schema_version": 2,
            **submitted,
            "source": "user",
            "source_draft_artifact_id": draft.id,
            "field_states": field_states,
            "field_source_draft_artifact_ids": {
                field: draft.id
                for field in ("title", "authors", "affiliations", "abstract", "keywords")
            },
            "generation_warnings": list(current_value.get("generation_warnings") or []),
            "updated_at": utc_now().isoformat(),
        }
        if not value["title"]:
            raise WorkflowValidationError("Final title cannot be blank.")
        comparable = (
            "title", "authors", "affiliations", "abstract", "keywords", "field_states"
        )
        if current is not None and all(
            current_value.get(key) == value.get(key) for key in comparable
        ):
            raise WorkflowValidationError("Final front matter has no change.")
        expected = {FINAL_FRONT_MATTER: current.id} if current is not None else None
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_FRONT_MATTER: (
                        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                metadata={
                    "operation": "front-matter-edit",
                    "source_draft_artifact_id": draft.id,
                },
                expected_current_artifacts=expected,
            )
        return {
            "front_matter_artifact_id": published[FINAL_FRONT_MATTER].id,
            "revision": state.revision,
        }

    def _revision(self, principal: Principal, project_id: str) -> int:
        state = self.repository.get_stage_state(principal.user_id, project_id, "final")
        return state.revision if state else 0

    @staticmethod
    def _evidence_boundary(
        compatibility: dict[str, Any], quality: dict[str, Any]
    ) -> dict[str, Any]:
        blueprint = compatibility.get("blueprint") or {}
        scope = dict(blueprint.get("scope_contract") or {})
        coverage = dict(blueprint.get("coverage_diagnostics") or {})
        evidence_sections = [
            section
            for section in (compatibility.get("section_evidence") or {}).get(
                "sections"
            )
            or []
            if isinstance(section, dict)
        ]
        writeable = {
            str(paper_id)
            for section in evidence_sections
            for paper_id in section.get("writeable_primary_papers") or []
            if str(paper_id).strip()
        }
        context_only = {
            str(paper_id)
            for section in evidence_sections
            for paper_id in section.get("context_only_primary_papers") or []
            if str(paper_id).strip()
        }
        unresolved = {
            str(paper_id)
            for section in evidence_sections
            for paper_id in section.get("unresolved_primary_papers") or []
            if str(paper_id).strip()
        }
        corpus_gaps = sorted(
            {
                f"{section.get('section_id')}:{question_id}"
                for section in evidence_sections
                for question_id in section.get("corpus_gap_questions") or []
                if str(question_id).strip()
            }
        )
        unverified_manual = [
            str(value)
            for value in quality.get("unverified_manual_paragraph_ids") or []
            if str(value).strip()
        ]
        warnings: list[str] = []
        if context_only:
            warnings.append("abstract_or_context_only_primary_papers")
        if unresolved:
            warnings.append("unresolved_primary_papers")
        if corpus_gaps:
            warnings.append("question_level_corpus_gaps")
        if unverified_manual:
            warnings.append("unverified_manual_claims_exported")
        return {
            "review_type": str(
                scope.get("review_type") or "narrative_topic_review"
            ),
            "coverage_claim": str(
                coverage.get("coverage_claim") or "selected_corpus_only"
            ),
            "selected_paper_count": int(
                coverage.get("selected_paper_count")
                or (scope.get("coverage_basis") or {}).get(
                    "selected_paper_count"
                )
                or 0
            ),
            "section_count": len(evidence_sections),
            "writeable_primary_paper_count": len(writeable),
            "context_only_primary_paper_ids": sorted(context_only),
            "unresolved_primary_paper_ids": sorted(unresolved),
            "corpus_gap_questions": corpus_gaps,
            "unverified_manual_paragraph_ids": unverified_manual,
            "warnings": warnings,
            "statement": (
                "This narrative review is limited to the user-confirmed corpus. "
                "It does not claim exhaustive global literature coverage."
            ),
        }

    def conclusion_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        text, draft, _approval = self._approved_draft(principal, project_id)
        synthesis = self.drafts.automatic_synthesis_source(
            principal, project_id, text=text, draft=draft
        )
        return {
            **self.drafts.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "draft_text": synthesis["draft_text"],
            "source_draft_artifact_id": draft.id,
            "source_quality_artifact_id": synthesis["source_quality_artifact_id"],
            "excluded_manual_paragraph_ids": synthesis[
                "excluded_manual_paragraph_ids"
            ],
            "expected_revision": self._revision(principal, project_id),
        }

    def build_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        text, draft, _approval = self._approved_draft(principal, project_id)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        front_matter, front_matter_artifact = self._read_json(
            principal, project_id, FINAL_FRONT_MATTER
        )
        if front_matter_artifact is None:
            front_matter = self._default_front_matter(
                text,
                fallback_title=project.topic if project is not None else "",
                author_candidate=self._author_candidate(principal),
                source_draft_artifact_id=draft.id,
            )
        states = dict(front_matter.get("field_states") or {})
        field_sources = dict(
            front_matter.get("field_source_draft_artifact_ids") or {}
        )
        raw_topic = str(project.topic if project is not None else "")
        suggested_title = build_publication_review_title(
            raw_topic or front_matter.get("title") or "",
            manuscript_title=front_matter.get("title") or "",
        )
        generation_fields: list[str] = []
        for field in ("title", "abstract", "keywords"):
            state = str(states.get(field) or "")
            if state in {"user_modified", "user_omitted"}:
                continue
            needs_refresh = (
                not front_matter.get(field)
                or str(field_sources.get(field) or "") != draft.id
            )
            if field == "title":
                needs_refresh = bool(
                    front_matter_artifact is None
                    or needs_refresh
                    or generated_title_needs_rewrite(
                        front_matter.get("title") or "", raw_topic
                    )
                )
            if needs_refresh:
                generation_fields.append(field)
        return {
            "project_id": project_id,
            "source_draft_artifact_id": draft.id,
            "source_front_matter_artifact_id": (
                front_matter_artifact.id if front_matter_artifact else ""
            ),
            "expected_revision": self._revision(principal, project_id),
            "title": (
                suggested_title
                if "title" in generation_fields
                else str(front_matter.get("title") or suggested_title)
            ),
            "review_topic": raw_topic,
            "front_matter": front_matter,
            "generation_fields": generation_fields,
            "abstract_source": self._abstract_source(text),
        }

    def publish_generated_front_matter(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        generated: dict[str, Any] | None,
        *,
        generation_error: str = "",
    ) -> dict[str, Any]:
        """Merge machine-owned fields without overwriting user-owned values."""

        text, draft, _approval = self._approved_draft(principal, project_id)
        if draft.id != str(job_payload.get("source_draft_artifact_id") or ""):
            raise WorkflowConflict("Draft changed while front matter was generated.")
        current, current_artifact = self._read_json(
            principal, project_id, FINAL_FRONT_MATTER
        )
        expected_front_id = str(job_payload.get("source_front_matter_artifact_id") or "")
        if (current_artifact.id if current_artifact else "") != expected_front_id:
            raise WorkflowConflict("Front matter changed while the final build was running.")
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if current_artifact is None:
            current = dict(job_payload.get("front_matter") or {})
            if not current:
                current = self._default_front_matter(
                    text,
                    fallback_title=project.topic if project is not None else "",
                    author_candidate=self._author_candidate(principal),
                    source_draft_artifact_id=draft.id,
                )
        value = deepcopy(current)
        value["schema_version"] = 2
        states = dict(value.get("field_states") or {})
        field_sources = dict(value.get("field_source_draft_artifact_ids") or {})
        # Legacy user-authored artifacts predate field_states.  Their populated
        # values are treated as user-owned and therefore never overwritten.
        for field in ("title", "authors", "affiliations", "abstract", "keywords"):
            if field not in states:
                states[field] = (
                    "user_modified"
                    if current_artifact is not None and value.get(field)
                    else "missing"
                )
        if not value.get("authors") and states.get("authors") != "user_omitted":
            candidate = self._author_candidate(principal)
            if candidate:
                value["authors"] = [candidate]
                states["authors"] = "generated"
                field_sources["authors"] = draft.id
        result = dict(generated or {})
        warnings = [str(item) for item in result.get("warnings") or [] if str(item)]
        if generation_error:
            warnings.append("front_matter_generation_unavailable")
        for field in ("abstract", "keywords"):
            if (
                states.get(field) == "user_modified"
                and str(field_sources.get(field) or current.get("source_draft_artifact_id") or "")
                != draft.id
            ):
                warnings.append(f"{field}_user_modified_on_older_draft")
        requested = {
            str(field) for field in job_payload.get("generation_fields") or []
        }
        if "title" in requested and states.get("title") != "user_modified":
            raw_topic = str(
                job_payload.get("review_topic")
                or (project.topic if project is not None else "")
            )
            generated_title = " ".join(
                str(result.get("title") or "").split()
            ).strip("# \t")
            if (
                not generated_title_is_acceptable(generated_title)
                or generated_title_needs_rewrite(generated_title, raw_topic)
            ):
                generated_title = build_publication_review_title(
                    raw_topic or value.get("title") or "",
                    manuscript_title=value.get("title") or "",
                )
                warnings.append("title_deterministic_fallback")
            value["title"] = generated_title
            states["title"] = "generated"
            field_sources["title"] = draft.id
        if "abstract" in requested and states.get("abstract") not in {
            "user_modified", "user_omitted"
        }:
            abstract = str(result.get("abstract") or "").strip()
            if abstract:
                value["abstract"] = abstract
                states["abstract"] = "generated"
                field_sources["abstract"] = draft.id
            else:
                states["abstract"] = "missing"
        if "keywords" in requested and states.get("keywords") not in {
            "user_modified", "user_omitted"
        }:
            keywords = [
                " ".join(str(item).split()).strip()
                for item in result.get("keywords") or []
                if str(item).strip()
            ]
            if keywords:
                value["keywords"] = list(dict.fromkeys(keywords))[:8]
                states["keywords"] = "generated"
                field_sources["keywords"] = draft.id
            else:
                states["keywords"] = "missing"
        for field in ("authors", "abstract", "keywords"):
            if not value.get(field) and states.get(field) != "user_omitted":
                warnings.append(f"{field}_missing")
        value.update(
            {
                "source": "generated+user-merge",
                "source_draft_artifact_id": draft.id,
                "field_states": states,
                "field_source_draft_artifact_ids": field_sources,
                "generation_warnings": list(dict.fromkeys(warnings)),
                "updated_at": utc_now().isoformat(),
            }
        )
        expected = (
            {FINAL_FRONT_MATTER: current_artifact.id}
            if current_artifact is not None
            else None
        )
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_FRONT_MATTER: (
                        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=self._revision(principal, project_id),
                metadata={
                    "operation": "front-matter-auto-merge",
                    "source_draft_artifact_id": draft.id,
                    "generation_error": str(generation_error or "")[:500],
                },
                expected_current_artifacts=expected,
            )
        return {
            "front_matter_artifact_id": published[FINAL_FRONT_MATTER].id,
            "revision": state.revision,
            "warnings": value["generation_warnings"],
        }

    def publish_conclusion(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        _text, current, _approval = self._approved_draft(principal, project_id)
        if current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while conclusion was generated.")
        markdown = str(built.get("markdown") or "").strip()
        if not markdown:
            raise WorkflowValidationError("Conclusion generation returned no Markdown.")
        report = built.get("report")
        report = {
            **(report if isinstance(report, dict) else {}),
            "source_quality_artifact_id": str(
                job_payload.get("source_quality_artifact_id") or ""
            ),
            "excluded_manual_paragraph_ids": list(
                job_payload.get("excluded_manual_paragraph_ids") or []
            ),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_CONCLUSION: ((markdown + "\n").encode(), "markdown"),
                    FINAL_CONCLUSION_REPORT: (
                        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "conclusion",
                    "source_draft_artifact_id": current.id,
                    "source_quality_artifact_id": str(
                        job_payload.get("source_quality_artifact_id") or ""
                    ),
                    "excluded_manual_paragraph_ids": list(
                        job_payload.get("excluded_manual_paragraph_ids") or []
                    ),
                },
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
            )
        return {
            "conclusion_artifact_id": published[FINAL_CONCLUSION].id,
            "conclusion_report_artifact_id": published[FINAL_CONCLUSION_REPORT].id,
            "revision": state.revision,
        }

    def overview_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        text, draft, _approval = self._approved_draft(principal, project_id)
        synthesis = self.drafts.automatic_synthesis_source(
            principal, project_id, text=text, draft=draft
        )
        return {
            **self.drafts.compatibility_payload(principal, project_id),
            "project_id": project_id,
            "draft_text": synthesis["draft_text"],
            "source_draft_artifact_id": draft.id,
            "source_quality_artifact_id": synthesis["source_quality_artifact_id"],
            "excluded_manual_paragraph_ids": synthesis[
                "excluded_manual_paragraph_ids"
            ],
            "expected_revision": self._revision(principal, project_id),
        }

    def publish_overview(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        _text, current, _approval = self._approved_draft(principal, project_id)
        if current.id != job_payload["source_draft_artifact_id"]:
            raise WorkflowConflict("Draft changed while overview was generated.")
        raw_output = str(built.get("output_path") or "").strip()
        output = Path(raw_output).resolve() if raw_output else None
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        try:
            if output is None:
                raise ValueError
            output.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError("Overview output escaped its user workspace.") from exc
        if output.is_symlink() or not output.is_file():
            raise WorkflowValidationError("Overview generation produced no image.")
        try:
            image_size(output)
        except (OSError, UnidentifiedImageError) as exc:
            raise WorkflowValidationError("Overview image is unreadable.") from exc
        editable = built.get("editable_text")
        if not isinstance(editable, dict) or not str(editable.get("title") or "").strip():
            raise WorkflowValidationError("Overview generation returned no editable text model.")
        files = {
            FINAL_OVERVIEW_IMAGE: (output.read_bytes(), output.suffix.lstrip(".") or "png"),
            FINAL_OVERVIEW_TEXT: (
                (json.dumps(editable, ensure_ascii=False, indent=2) + "\n").encode(),
                "json",
            ),
        }
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                files,
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "overview",
                    "source_draft_artifact_id": current.id,
                    "source_quality_artifact_id": str(
                        job_payload.get("source_quality_artifact_id") or ""
                    ),
                    "excluded_manual_paragraph_ids": list(
                        job_payload.get("excluded_manual_paragraph_ids") or []
                    ),
                    "report": {
                        **dict(built.get("report") or {}),
                        "excluded_manual_paragraph_ids": list(
                            job_payload.get("excluded_manual_paragraph_ids") or []
                        ),
                    },
                },
                expected_current_artifacts={DRAFT_DOCUMENT: current.id},
            )
        return {
            "overview_artifact_id": published[FINAL_OVERVIEW_IMAGE].id,
            "overview_text_artifact_id": published[FINAL_OVERVIEW_TEXT].id,
            "revision": state.revision,
        }

    def save_overview_text(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        title: str,
        subtitle: str,
        labels: list[str],
    ) -> dict[str, Any]:
        _draft_text, draft, _approval = self._approved_draft(principal, project_id)
        current_value, current = self._read_json(
            principal, project_id, FINAL_OVERVIEW_TEXT
        )
        if current is None:
            raise FinalNotReady("Generate the overview before editing its text.")
        overview = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        if (
            overview is None
            or current.metadata.get("source_draft_artifact_id") != draft.id
            or overview.metadata.get("source_draft_artifact_id") != draft.id
        ):
            raise FinalNotReady(
                "The overview belongs to an older Draft. Generate it again before editing."
            )
        requested = {
            "title": str(title).strip(),
            "subtitle": str(subtitle).strip(),
            "labels": [str(value).strip() for value in labels if str(value).strip()],
        }
        if all(current_value.get(key) == value for key, value in requested.items()):
            raise WorkflowValidationError("Overview text has no change.")
        edited = {
            **current_value,
            **requested,
            "edited_at": utc_now().isoformat(),
        }
        metadata = {**dict(current.metadata), "operation": "overview-text-edit"}
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_OVERVIEW_TEXT: (
                        (json.dumps(edited, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    )
                },
                expected_revision=revision,
                metadata=metadata,
                expected_current_artifacts={FINAL_OVERVIEW_TEXT: current.id},
            )
        return {
            "overview_text_artifact_id": published[FINAL_OVERVIEW_TEXT].id,
            "revision": state.revision,
        }

    def _validate_markdown(
        self,
        principal: Principal,
        project_id: str,
        markdown: str,
        *,
        source_paper_ids: list[str],
        source_reference_numbers: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        missing: list[str] = []
        wrong_project: list[str] = []
        referenced = list(dict.fromkeys(ARTIFACT_URL.findall(markdown or "")))
        for artifact_id in referenced:
            try:
                resolved = self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact_id
                )
                if resolved.artifact.project_id != project_id:
                    wrong_project.append(artifact_id)
            except (WorkflowNotFound, WorkflowConflict):
                missing.append(artifact_id)
        reference_match = REFERENCES_HEADING.search(markdown or "")
        body = markdown[: reference_match.start()] if reference_match else markdown
        reference_text = markdown[reference_match.end() :] if reference_match else ""

        def numbers(text: str) -> set[int]:
            values: set[int] = set()
            for match in CITATION_CALLOUT.finditer(text):
                for start, end in re.findall(r"(\d+)\s*-\s*(\d+)", match.group(1)):
                    lower, upper = sorted((int(start), int(end)))
                    values.update(range(lower, upper + 1))
                for token in re.findall(r"\d+", match.group(1)):
                    values.add(int(token))
            return values

        callouts = numbers(body)
        listed_rows = REFERENCE_ITEM.findall(reference_text)
        listed = {int(number) for number, _text in listed_rows}
        normalized_sources = list(
            dict.fromkeys(
                str(value).strip()
                for value in source_paper_ids
                if str(value).strip()
            )
        )
        reference_numbers = {
            paper_id: int(number)
            for paper_id, number in (source_reference_numbers or {}).items()
            if paper_id in normalized_sources and int(number) > 0
        }
        for index, paper_id in enumerate(normalized_sources, start=1):
            reference_numbers.setdefault(paper_id, index)
        missing_sources = [
            paper_id
            for paper_id in normalized_sources
            if reference_numbers[paper_id] not in listed
        ]
        listed_source_ids: list[str] = []
        unmapped_reference_numbers: list[int] = []
        sources_by_number = {
            number: paper_id for paper_id, number in reference_numbers.items()
        }
        for number, _text in listed_rows:
            matched_paper_id = sources_by_number.get(int(number))
            if matched_paper_id:
                listed_source_ids.append(matched_paper_id)
            else:
                unmapped_reference_numbers.append(int(number))
        active_sources, immutable_sources = self._library_source_sets(
            principal, normalized_sources
        )
        unavailable_sources = sorted(set(normalized_sources) - active_sources)
        missing_source_artifacts = sorted(
            set(normalized_sources) - immutable_sources
        )
        blocking_issues: list[str] = []
        malformed_images = malformed_markdown_image_lines(markdown)
        if missing:
            blocking_issues.append("missing_artifact_references")
        if wrong_project:
            blocking_issues.append("cross_project_artifact_references")
        if malformed_images:
            blocking_issues.append("malformed_markdown_image")
        warning_issues: list[str] = []
        if not reference_match:
            warning_issues.append("missing_references_section")
        elif not listed:
            warning_issues.append("empty_references_section")
        if normalized_sources and not callouts:
            warning_issues.append("draft_has_no_citation_callouts")
        if callouts != listed:
            warning_issues.append("citation_reference_map_mismatch")
        if missing_sources:
            warning_issues.append("citation_sources_missing_from_references")
        if unmapped_reference_numbers:
            warning_issues.append("references_include_unmapped_sources")
        if unavailable_sources:
            warning_issues.append("library_sources_unavailable")
        if missing_source_artifacts:
            warning_issues.append("library_source_artifacts_missing")
        voice_issues = publication_voice_issues(body)
        if voice_issues:
            warning_issues.append("publication_voice_leakage")
        figure_argument_findings = _figure_argument_findings(body)
        if figure_argument_findings:
            warning_issues.append("figure_argument_closure_incomplete")
        return {
            "valid": not blocking_issues,
            "referenced_artifact_ids": referenced,
            "missing_artifact_ids": missing,
            "cross_project_artifact_ids": wrong_project,
            "malformed_markdown_images": malformed_images,
            "citation_callouts": sorted(callouts),
            "listed_references": sorted(listed),
            "source_paper_ids": normalized_sources,
            "missing_source_paper_ids": missing_sources,
            "listed_source_paper_ids": list(dict.fromkeys(listed_source_ids)),
            "unmapped_reference_numbers": sorted(unmapped_reference_numbers),
            "unavailable_source_paper_ids": unavailable_sources,
            "missing_source_artifact_paper_ids": missing_source_artifacts,
            "publication_voice_issues": voice_issues,
            "figure_argument_findings": figure_argument_findings,
            "references_section_present": bool(reference_match),
            "blocking_issues": blocking_issues,
            "warning_issues": warning_issues,
            "validated_at": utc_now().isoformat(),
        }

    def _library_source_sets(
        self, principal: Principal, paper_ids: list[str]
    ) -> tuple[set[str], set[str]]:
        normalized = tuple(
            dict.fromkeys(str(value).strip() for value in paper_ids if str(value).strip())
        )
        if not normalized:
            return set(), set()
        with database_session(self.repository.session_factory) as session:
            active = set(
                session.scalars(
                    select(LibraryPaper.paper_id).where(
                        LibraryPaper.user_id == uuid.UUID(principal.user_id),
                        LibraryPaper.paper_id.in_(normalized),
                        LibraryPaper.status == "active",
                        LibraryPaper.deleted_at.is_(None),
                    )
                ).all()
            )
            available = set(
                session.scalars(
                    select(LibraryArtifact.paper_id)
                    .where(
                        LibraryArtifact.user_id == uuid.UUID(principal.user_id),
                        LibraryArtifact.paper_id.in_(normalized),
                        LibraryArtifact.availability == "available",
                    )
                    .distinct()
                ).all()
            )
        return active, available

    def build(self, principal: Principal, project_id: str) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        draft_text, draft, approval = self._approved_draft(principal, project_id)
        draft_state = self.repository.get_stage_state(
            principal.user_id, project_id, "draft"
        )
        approval_artifact = self._artifact(principal, project_id, DRAFT_APPROVAL)
        quality_artifact = self._artifact(principal, project_id, DRAFT_QUALITY)
        if (
            draft_state is None
            or draft_state.status != "approved"
            or approval_artifact is None
            or quality_artifact is None
            or approval.get("quality_artifact_id") != quality_artifact.id
        ):
            raise FinalNotReady("Draft approval or evaluation changed before Final build.")
        conclusion, conclusion_artifact = self._read_text(
            principal, project_id, FINAL_CONCLUSION
        )
        conclusion_report, conclusion_report_artifact = self._read_json(
            principal, project_id, FINAL_CONCLUSION_REPORT
        )
        overview = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        overview_text, overview_text_artifact = self._read_json(
            principal, project_id, FINAL_OVERVIEW_TEXT
        )
        front_matter, front_matter_artifact = self._read_json(
            principal, project_id, FINAL_FRONT_MATTER
        )
        if front_matter_artifact is None:
            project = self.repository.get_owned_project(principal.user_id, project_id)
            front_matter = self._default_front_matter(
                draft_text,
                fallback_title=project.topic if project is not None else "",
                author_candidate=self._author_candidate(principal),
                source_draft_artifact_id=draft.id,
            )
        if conclusion_artifact and conclusion_artifact.metadata.get(
            "source_draft_artifact_id"
        ) != draft.id:
            raise FinalNotReady(
                "The conclusion belongs to an older Draft. Generate it again or remove it."
            )
        if bool(conclusion_artifact) != bool(conclusion_report_artifact):
            raise FinalNotReady("The conclusion and its quality report are incomplete.")
        if bool(overview) != bool(overview_text_artifact):
            raise FinalNotReady("The overview image and editable text are incomplete.")
        if overview and (
            overview.metadata.get("source_draft_artifact_id") != draft.id
            or overview_text_artifact.metadata.get("source_draft_artifact_id") != draft.id
        ):
            raise FinalNotReady(
                "The overview belongs to an older Draft. Generate it again or remove it."
            )
        reference_match = REFERENCES_HEADING.search(draft_text)
        draft_body = (
            draft_text[: reference_match.start()].rstrip()
            if reference_match
            else draft_text.rstrip()
        )
        draft_references = (
            draft_text[reference_match.start() :].strip() if reference_match else ""
        )
        draft_body = self._apply_front_matter(draft_body, front_matter)
        compatibility = self.drafts.compatibility_payload(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME
        )
        blueprint, _blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        if overview and overview_text_needs_rewrite(
            overview_text,
            discovery.get("topic") or blueprint.get("review_topic") or "",
        ):
            basis = blueprint.get("classification_basis")
            basis = basis if isinstance(basis, dict) else {}
            query_plan = discovery.get("query_plan")
            query_plan = query_plan if isinstance(query_plan, dict) else {}
            overview_text = build_publication_overview_text(
                discovery.get("topic") or blueprint.get("review_topic") or "",
                manuscript_title=front_matter.get("title") or "",
                group_by=query_plan.get("group_by") or discovery.get("group_by") or [],
                classification_rule=(
                    basis.get("description")
                    or basis.get("primary_axis")
                    or basis.get("overview_axis")
                ),
            )
        matrix_rows = (
            compatibility.get("matrix", {}).get("rows")
            if isinstance(compatibility.get("matrix"), dict)
            else []
        )
        quality, _quality_record = self._read_json(
            principal, project_id, DRAFT_QUALITY
        )
        section_index = (
            compatibility.get("section_index")
            if isinstance(compatibility.get("section_index"), dict)
            else {}
        )
        structured_source_paper_ids = [
            str(paper_id)
            for section in section_index.get("sections") or []
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict)
            for paper_id in (
                paragraph.get("cited_paper_ids")
                or ([paragraph.get("paper_id")] if paragraph.get("paper_id") else [])
            )
            if str(paper_id).strip()
        ]
        reference_ledger = self._reference_ledger(
            draft_text,
            quality,
            section_index,
        )
        draft_body, reference_ledger = self._render_final_citation_numbers(
            draft_body,
            reference_ledger,
        )
        ledger_paper_ids = [
            str(item.get("paper_id") or "").strip()
            for item in reference_ledger.get("entries") or []
            if isinstance(item, dict) and str(item.get("paper_id") or "").strip()
        ]
        source_paper_ids = list(
            dict.fromkeys(
                ledger_paper_ids
                if reference_ledger.get("complete")
                else [*ledger_paper_ids, *structured_source_paper_ids]
            )
        )
        source_reference_numbers = {
            str(item.get("paper_id") or "").strip(): int(item.get("callout"))
            for item in reference_ledger.get("entries") or []
            if isinstance(item, dict)
            and str(item.get("paper_id") or "").strip()
            and str(item.get("callout") or "").isdigit()
        }
        reference_metadata_artifact_ids: dict[str, str] = {}
        if reference_ledger.get("complete"):
            canonical_references, reference_metadata_artifact_ids = (
                self._canonical_reference_section(
                    principal,
                    source_paper_ids,
                    [row for row in matrix_rows or [] if isinstance(row, dict)],
                    reference_numbers=source_reference_numbers,
                )
            )
            if canonical_references:
                draft_references = canonical_references
        # Search execution, query logs, Matrix counts, and model-assisted workflow
        # provenance remain available in internal artifacts and audit views. They
        # are not publication prose.
        draft_body = self._sanitize_internal_section_headings(draft_body)
        draft_body = self._remove_review_methods(draft_body)
        overview_block = ""
        if overview is not None:
            overview_lines = [
                f"![Overview figure](/api/v1/artifacts/{overview.id}/content)",
            ]
            caption = ". ".join(
                value
                for value in (
                    str(overview_text.get("title") or "").strip(),
                    str(overview_text.get("subtitle") or "").strip(),
                )
                if value
            )
            labels = [
                str(value).strip()
                for value in overview_text.get("labels") or []
                if str(value).strip()
            ]
            if labels:
                caption = (caption + " — " if caption else "") + ", ".join(labels)
            if caption:
                overview_lines.append(f"*{caption.rstrip(' .')}.*")
            else:
                overview_lines.append("*Review overview.*")
            overview_block = "\n".join(overview_lines)
        assembled_body = self._insert_before_introduction(draft_body, overview_block)
        parts = [assembled_body]
        if conclusion:
            parts.append(conclusion.strip())
        if draft_references:
            parts.append(draft_references)
        markdown = "\n\n".join(parts).rstrip() + "\n"
        markdown = _normalize_publication_markup(markdown)
        validation = self._validate_markdown(
            principal,
            project_id,
            markdown,
            source_paper_ids=source_paper_ids,
            source_reference_numbers=source_reference_numbers,
        )
        validation["reference_ledger"] = reference_ledger
        claim_citation_mapping = self._claim_citation_mapping_report(
            compatibility, reference_ledger
        )
        validation["claim_citation_mapping"] = claim_citation_mapping
        bibliography_identity = self._bibliography_identity_report(
            principal, source_paper_ids
        )
        validation["bibliography_identity"] = bibliography_identity
        taxonomy_report = (
            blueprint.get("taxonomy_diagnostics")
            if isinstance(blueprint.get("taxonomy_diagnostics"), dict)
            else {}
        )
        validation["classification_contract"] = {
            "status": str(
                taxonomy_report.get("classification_contract_status") or "unknown"
            ),
            "missing_topic_partitions": list(
                taxonomy_report.get("missing_topic_partitions") or []
            ),
            "boundary_section_ids": list(
                taxonomy_report.get("boundary_section_ids") or []
            ),
        }
        validation["coverage_diagnostics"] = dict(
            discovery.get("coverage_diagnostics")
            or blueprint.get("coverage_diagnostics")
            or {}
        )
        search_record = (
            discovery.get("search_record")
            if isinstance(discovery.get("search_record"), dict)
            else discovery_search_record(discovery)
        )
        validation["methods_execution"] = {
            "status": "aligned",
            "requested_sources": list(search_record.get("requested_sources") or []),
            "executed_sources": list(search_record.get("executed_sources") or []),
            "failed_sources": list(search_record.get("failed_sources") or []),
            "publication_methods_section": "omitted",
            "issues": [],
        }
        overview_semantics = self._overview_semantic_report(
            overview_text if isinstance(overview_text, dict) else {},
            discovery,
        )
        validation["overview_semantics"] = overview_semantics
        release_integrity_issues: list[str] = []
        if not reference_ledger.get("complete"):
            release_integrity_issues.append("citation_identity_unresolved")
        if claim_citation_mapping.get("issues"):
            release_integrity_issues.append("claim_citation_mapping_failure")
        if bibliography_identity.get("unresolved_paper_ids"):
            release_integrity_issues.append("bibliography_identity_unresolved")
        if taxonomy_report.get("classification_contract_status") == "drift":
            release_integrity_issues.append("classification_contract_drift")
        if overview_semantics.get("issues"):
            release_integrity_issues.append("overview_semantics_invalid")
        figure_findings = validation.get("figure_argument_findings") or []
        if any(
            "source_reuse_permission_unverified" in (row.get("issues") or [])
            for row in figure_findings
            if isinstance(row, dict)
        ):
            release_integrity_issues.append("figure_rights_unresolved")
        if any(
            set(row.get("issues") or [])
            - {"source_reuse_permission_unverified"}
            for row in figure_findings
            if isinstance(row, dict)
        ):
            release_integrity_issues.append("figure_evidence_binding_incomplete")
        blueprint_metadata_ids = (
            blueprint.get("source_bibliography_metadata_artifact_ids")
            if isinstance(
                blueprint.get("source_bibliography_metadata_artifact_ids"), dict
            )
            else {}
        )
        changed_after_blueprint = sorted(
            paper_id
            for paper_id, artifact_id in reference_metadata_artifact_ids.items()
            if str(blueprint_metadata_ids.get(paper_id) or "")
            and str(blueprint_metadata_ids.get(paper_id) or "") != str(artifact_id)
        )
        if changed_after_blueprint:
            release_integrity_issues.append(
                "scope_selection_requires_recheck_after_metadata_change"
            )
        validation["metadata_changed_after_blueprint_paper_ids"] = changed_after_blueprint
        release_integrity_warnings = {
            "missing_references_section",
            "empty_references_section",
            "draft_has_no_citation_callouts",
            "citation_reference_map_mismatch",
            "citation_sources_missing_from_references",
            "references_include_unmapped_sources",
            "library_sources_unavailable",
            "library_source_artifacts_missing",
        }
        if release_integrity_warnings.intersection(validation.get("warning_issues") or []):
            release_integrity_issues.append("citation_or_source_integrity_failure")
        validation["release_integrity_issues"] = list(
            dict.fromkeys(release_integrity_issues)
        )
        validation["release_ready"] = bool(
            validation["valid"] and not validation["release_integrity_issues"]
        )
        evidence_boundary = self._evidence_boundary(compatibility, quality)
        validation["evidence_boundary"] = evidence_boundary
        unverified_manual = [
            str(value)
            for value in quality.get("unverified_manual_paragraph_ids") or []
            if str(value).strip()
        ]
        if unverified_manual:
            validation["warning_issues"] = list(
                dict.fromkeys(
                    [
                        *validation.get("warning_issues", []),
                        "unverified_manual_claims_exported",
                    ]
                )
            )
            validation["unverified_manual_paragraph_ids"] = unverified_manual
        if not validation["valid"]:
            raise FinalNotReady(
                "Final manuscript contains publication-blocking markup or an invalid artifact reference."
            )
        release = {
            "status": "released",
            "publication_status": (
                "release_ready" if validation["release_ready"] else "review_only"
            ),
            "release_ready": validation["release_ready"],
            "source_draft_artifact_id": draft.id,
            "source_paper_ids": validation["source_paper_ids"],
            "validation_blocking_issues": [],
            "release_integrity_issues": validation["release_integrity_issues"],
            "validation_warning_issues": validation["warning_issues"],
            "unverified_manual_paragraph_ids": unverified_manual,
            "evidence_boundary": evidence_boundary,
            "reference_metadata_artifact_ids": reference_metadata_artifact_ids,
            "reference_ledger": reference_ledger,
            "bibliography_identity": bibliography_identity,
            "released_at": utc_now().isoformat(),
        }
        source_ids = {
            "source_draft_artifact_id": draft.id,
            "conclusion_artifact_id": conclusion_artifact.id if conclusion_artifact else "",
            "conclusion_report_artifact_id": (
                conclusion_report_artifact.id if conclusion_report_artifact else ""
            ),
            "overview_artifact_id": overview.id if overview else "",
            "overview_text_artifact_id": (
                overview_text_artifact.id if overview_text_artifact else ""
            ),
            "front_matter_artifact_id": (
                front_matter_artifact.id if front_matter_artifact else ""
            ),
        }
        expected_currents = {
            DRAFT_DOCUMENT: draft.id,
            DRAFT_APPROVAL: approval_artifact.id,
            DRAFT_QUALITY: quality_artifact.id,
        }
        if conclusion_artifact:
            expected_currents[FINAL_CONCLUSION] = conclusion_artifact.id
        if conclusion_report_artifact:
            expected_currents[FINAL_CONCLUSION_REPORT] = conclusion_report_artifact.id
        if overview:
            expected_currents[FINAL_OVERVIEW_IMAGE] = overview.id
        if overview_text_artifact:
            expected_currents[FINAL_OVERVIEW_TEXT] = overview_text_artifact.id
        if front_matter_artifact:
            expected_currents[FINAL_FRONT_MATTER] = front_matter_artifact.id
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_DRAFT: (markdown.encode(), "markdown"),
                    FINAL_VALIDATION: (
                        (json.dumps(validation, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_RELEASE: (
                        (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                },
                expected_revision=self._revision(principal, project_id),
                metadata={
                    "operation": "final-build",
                    "reference_metadata_artifact_ids": reference_metadata_artifact_ids,
                    **source_ids,
                },
                expected_current_artifacts=expected_currents,
                expected_stage_states={
                    "draft": {
                        "revision": draft_state.revision,
                        "status": "approved",
                    }
                },
            )
        return {
            "final_artifact_id": published[FINAL_DRAFT].id,
            "validation_artifact_id": published[FINAL_VALIDATION].id,
            "release_artifact_id": published[FINAL_RELEASE].id,
            "revision": state.revision,
        }

    def export_payload(self, principal: Principal, project_id: str) -> dict[str, Any]:
        current_payload = self.get(principal, project_id)
        if (
            not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise FinalNotReady("Build the current Final manuscript before Word export.")
        _draft_text, draft, _approval = self._approved_draft(principal, project_id)
        final_text, final_artifact = self._read_text(
            principal, project_id, FINAL_DRAFT, required=True
        )
        # Reapply publication-safe normalization so Final artifacts assembled
        # by an older build remain exportable without mutating that artifact.
        final_text = _normalize_publication_markup(final_text)
        compatibility = self.drafts.compatibility_payload(principal, project_id)
        artifact_paths = dict(compatibility.get("figure_artifact_paths") or {})
        for artifact_id in dict.fromkeys(ARTIFACT_URL.findall(final_text)):
            artifact_paths[artifact_id] = str(
                self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact_id
                ).path
            )
        compatibility["figure_artifact_paths"] = artifact_paths
        return {
            **compatibility,
            "project_id": project_id,
            "source_draft_artifact_id": draft.id,
            "source_final_artifact_id": final_artifact.id,
            "final_markdown": final_text,
            "expected_revision": self._revision(principal, project_id),
            "source_release_artifact_id": current_payload["release_artifact_id"],
            "source_paper_ids": list(
                (current_payload.get("release") or {}).get("source_paper_ids") or []
            ),
        }

    def publish_export(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        final = self._artifact(principal, project_id, FINAL_DRAFT)
        current_payload = self.get(principal, project_id)
        if (
            final is None
            or final.id != job_payload["source_final_artifact_id"]
            or not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise WorkflowConflict("Final manuscript changed while DOCX was generated.")
        raw = str(built.get("output_path") or "").strip()
        output = Path(raw).resolve() if raw else None
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        try:
            if output is None:
                raise ValueError
            output.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError("DOCX output escaped its user workspace.") from exc
        if output.is_symlink() or not output.is_file() or output.suffix.casefold() != ".docx":
            raise WorkflowValidationError("DOCX export produced no document.")
        try:
            with zipfile.ZipFile(output) as archive:
                corrupt = archive.testzip()
                names = set(archive.namelist())
                required = {"[Content_Types].xml", "word/document.xml"}
                missing_parts = sorted(required - names)
                document = ET.fromstring(archive.read("word/document.xml"))
                namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraph_count = len(document.findall(".//w:p", namespace))
                table_count = len(document.findall(".//w:tbl", namespace))
                image_count = sum(name.startswith("word/media/") for name in names)
        except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise WorkflowValidationError(
                f"DOCX structural QA could not read the generated package: {type(exc).__name__}."
            ) from exc
        blockers = []
        if corrupt:
            blockers.append({"type": "corrupt_zip_member", "member": corrupt})
        if missing_parts:
            blockers.append({"type": "missing_docx_parts", "parts": missing_parts})
        if paragraph_count <= 0:
            blockers.append({"type": "empty_document_body"})
        docx_qa = {
            "schema_version": 1,
            "status": "blocked" if blockers else "pass",
            "paragraph_count": paragraph_count,
            "table_count": table_count,
            "image_count": image_count,
            "blocking_issues": blockers,
            "warning_issues": [],
            "checked_at": utc_now().isoformat(),
        }
        if blockers:
            raise WorkflowValidationError("DOCX failed structural package QA.")
        with self._write_lock:
            published, state = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_DOCX: (output.read_bytes(), "docx"),
                    FINAL_DOCX_QA: (
                        (json.dumps(docx_qa, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata={
                    "operation": "docx-export",
                    "source_final_artifact_id": final.id,
                    "source_release_artifact_id": job_payload[
                        "source_release_artifact_id"
                    ],
                    "download_name": str(built.get("download_name") or "review.docx"),
                },
                status="completed",
                expected_current_artifacts={
                    FINAL_DRAFT: final.id,
                    FINAL_RELEASE: job_payload["source_release_artifact_id"],
                },
            )
        return {
            "docx_artifact_id": published[FINAL_DOCX].id,
            "docx_qa_artifact_id": published[FINAL_DOCX_QA].id,
            "docx_qa": docx_qa,
            "download_name": str(built.get("download_name") or "review.docx"),
            "revision": state.revision,
        }

    def pdf_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        language_profile: str,
    ) -> dict[str, Any]:
        profile = str(language_profile or "").strip()
        if profile not in SUPPORTED_PROFILES:
            raise WorkflowValidationError(
                "PDF language profile must be `en` or `zh-CN`."
            )
        current_payload = self.get(principal, project_id)
        if (
            not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise FinalNotReady("Build the current Final manuscript before PDF export.")
        _draft_text, draft, _approval = self._approved_draft(principal, project_id)
        final_text, final_artifact = self._read_text(
            principal, project_id, FINAL_DRAFT, required=True
        )
        final_text = _normalize_publication_markup(final_text)
        compatibility = self.drafts.compatibility_payload(principal, project_id)
        # The PDF worker receives only assets that the released manuscript
        # actually references. This keeps the isolated request bounded without
        # changing the upstream Images approval/redraw workflow.
        artifact_paths: dict[str, str] = {}
        for artifact_id in dict.fromkeys(ARTIFACT_URL.findall(final_text)):
            artifact_paths[artifact_id] = str(
                self.artifacts.resolve_owned_artifact(
                    principal.user_id, artifact_id
                ).path
            )
        return {
            **compatibility,
            "project_id": project_id,
            "source_draft_artifact_id": draft.id,
            "source_final_artifact_id": final_artifact.id,
            "source_release_artifact_id": current_payload["release_artifact_id"],
            "final_markdown": final_text,
            "figure_artifact_paths": artifact_paths,
            "language_profile": profile,
            "template": "modern-survey",
            "expected_revision": self._revision(principal, project_id),
        }

    @staticmethod
    def _trusted_generated_file(
        raw: Any,
        *,
        user_root: Path,
        suffix: str,
        label: str,
    ) -> Path:
        value = str(raw or "").strip()
        path = Path(value).resolve() if value else None
        try:
            if path is None:
                raise ValueError
            path.relative_to(user_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                f"{label} output escaped its user workspace."
            ) from exc
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() != suffix:
            raise WorkflowValidationError(f"{label} export produced no valid output.")
        return path

    def publish_pdf(
        self,
        principal: Principal,
        project_id: str,
        job_payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        final = self._artifact(principal, project_id, FINAL_DRAFT)
        current_payload = self.get(principal, project_id)
        if (
            final is None
            or final.id != job_payload["source_final_artifact_id"]
            or current_payload.get("release_artifact_id")
            != job_payload["source_release_artifact_id"]
            or not current_payload.get("final_current")
            or not current_payload.get("release_current")
        ):
            raise WorkflowConflict("Final manuscript changed while PDF was generated.")
        profile = str(job_payload.get("language_profile") or "")
        if profile not in SUPPORTED_PROFILES:
            raise WorkflowValidationError("PDF language profile is invalid.")
        user_root = self.artifacts.workspace_manager.user_root(principal.user_id)
        pdf_path = self._trusted_generated_file(
            built.get("output_path"),
            user_root=user_root,
            suffix=".pdf",
            label="PDF",
        )
        tex_path = self._trusted_generated_file(
            built.get("tex_path"),
            user_root=user_root,
            suffix=".tex",
            label="LaTeX",
        )
        log_path = self._trusted_generated_file(
            built.get("compile_log_path"),
            user_root=user_root,
            suffix=".log",
            label="PDF compile log",
        )
        state_payload = built.get("manuscript_state")
        manifest = built.get("render_manifest")
        qa = built.get("pdf_qa")
        if not all(isinstance(item, dict) for item in (state_payload, manifest, qa)):
            raise WorkflowValidationError("PDF renderer returned an incomplete state bundle.")
        if (
            manifest.get("language_profile") != profile
            or manifest.get("template") != "modern-survey"
            or manifest.get("template_version") != TEMPLATE_VERSION
            or manifest.get("source_final_artifact_id") != final.id
            or manifest.get("source_release_artifact_id")
            != job_payload["source_release_artifact_id"]
            or manifest.get("shell_escape") is not False
        ):
            raise WorkflowValidationError("PDF render manifest does not match the current job.")
        if (
            not state_payload.get("validation", {}).get("valid")
            or qa.get("status") not in {"pass", "pass_with_warnings"}
            or qa.get("blocking_issues")
            or not qa.get("all_fonts_embedded")
        ):
            raise WorkflowValidationError(
                "PDF failed deterministic content or visual publication gates."
            )
        expected_state = build_manuscript_state(
            str(job_payload.get("final_markdown") or ""),
            artifact_paths=dict(job_payload.get("figure_artifact_paths") or {}),
        )
        if (
            state_payload.get("source_markdown_sha256")
            != expected_state.get("source_markdown_sha256")
            or state_payload.get("semantic_sha256")
            != expected_state.get("semantic_sha256")
            or state_payload.get("counts") != expected_state.get("counts")
        ):
            raise WorkflowValidationError(
                "PDF Final Manuscript State diverged from the released Markdown."
            )
        if (
            manifest.get("source_markdown_sha256")
            != state_payload.get("source_markdown_sha256")
            or manifest.get("semantic_sha256")
            != state_payload.get("semantic_sha256")
            or set(dict(manifest.get("asset_sha256") or {}))
            != set(dict(job_payload.get("figure_artifact_paths") or {}))
        ):
            raise WorkflowValidationError(
                "PDF render manifest diverged from its manuscript or approved assets."
            )
        metadata = {
            "operation": "pdf-export",
            "source_final_artifact_id": final.id,
            "source_release_artifact_id": job_payload["source_release_artifact_id"],
            "language_profile": profile,
            "template": "modern-survey",
            "download_name": str(
                built.get("download_name") or f"review.{profile}.pdf"
            ),
        }
        with self._write_lock:
            published, stage = self._publish_files(
                principal,
                project_id,
                {
                    FINAL_MANUSCRIPT_STATE: (
                        (json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_RENDER_MANIFEST: (
                        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_TEX: (tex_path.read_bytes(), "tex"),
                    FINAL_PDF: (pdf_path.read_bytes(), "pdf"),
                    FINAL_PDF_QA: (
                        (json.dumps(qa, ensure_ascii=False, indent=2) + "\n").encode(),
                        "json",
                    ),
                    FINAL_PDF_COMPILE_LOG: (log_path.read_bytes(), "log"),
                },
                expected_revision=int(job_payload["expected_revision"]),
                metadata=metadata,
                status="completed",
                expected_current_artifacts={
                    FINAL_DRAFT: final.id,
                    FINAL_RELEASE: job_payload["source_release_artifact_id"],
                },
            )
        return {
            "pdf_artifact_id": published[FINAL_PDF].id,
            "tex_artifact_id": published[FINAL_TEX].id,
            "pdf_qa_artifact_id": published[FINAL_PDF_QA].id,
            "language_profile": profile,
            "download_name": metadata["download_name"],
            "revision": stage.revision,
        }

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        draft_payload = self.drafts.get(principal, project_id)
        final_text, final_artifact = self._read_text(
            principal, project_id, FINAL_DRAFT
        )
        conclusion, conclusion_artifact = self._read_text(
            principal, project_id, FINAL_CONCLUSION
        )
        conclusion_report, conclusion_report_artifact = self._read_json(
            principal, project_id, FINAL_CONCLUSION_REPORT
        )
        overview = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        overview_text, overview_text_artifact = self._read_json(
            principal, project_id, FINAL_OVERVIEW_TEXT
        )
        front_matter, front_matter_artifact = self._read_json(
            principal, project_id, FINAL_FRONT_MATTER
        )
        validation, validation_artifact = self._read_json(
            principal, project_id, FINAL_VALIDATION
        )
        release, release_artifact = self._read_json(
            principal, project_id, FINAL_RELEASE
        )
        docx = self._artifact(principal, project_id, FINAL_DOCX)
        docx_qa, docx_qa_artifact = self._read_json(
            principal, project_id, FINAL_DOCX_QA
        )
        manuscript_state, manuscript_state_artifact = self._read_json(
            principal, project_id, FINAL_MANUSCRIPT_STATE
        )
        render_manifest, render_manifest_artifact = self._read_json(
            principal, project_id, FINAL_RENDER_MANIFEST
        )
        pdf_qa, pdf_qa_artifact = self._read_json(
            principal, project_id, FINAL_PDF_QA
        )
        tex_artifact = self._artifact(principal, project_id, FINAL_TEX)
        pdf_artifact = self._artifact(principal, project_id, FINAL_PDF)
        compile_log_artifact = self._artifact(
            principal, project_id, FINAL_PDF_COMPILE_LOG
        )
        state = self.repository.get_stage_state(principal.user_id, project_id, "final")
        current_draft_id = str(draft_payload.get("draft_artifact_id") or "")
        approved = bool(draft_payload.get("draft_approval_current"))
        conclusion_current = bool(
            conclusion_artifact
            and conclusion_report_artifact
            and approved
            and conclusion_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
            and conclusion_report_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
        )
        overview_current = bool(
            overview
            and overview_text_artifact
            and approved
            and overview.metadata.get("source_draft_artifact_id") == current_draft_id
            and overview_text_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
        )
        if front_matter_artifact is None:
            project = self.repository.get_owned_project(principal.user_id, project_id)
            front_matter = self._default_front_matter(
                str(draft_payload.get("first_draft_md") or ""),
                fallback_title=project.topic if project is not None else "",
                author_candidate=self._author_candidate(principal),
                source_draft_artifact_id=current_draft_id,
            )
        front_matter_current = bool(
            front_matter_artifact
            and approved
        )
        final_current = bool(
            final_artifact
            and final_artifact.metadata.get("source_draft_artifact_id") == current_draft_id
            and approved
            and final_artifact.metadata.get("conclusion_artifact_id")
            == (conclusion_artifact.id if conclusion_artifact else "")
            and final_artifact.metadata.get("overview_artifact_id")
            == (overview.id if overview else "")
            and final_artifact.metadata.get("overview_text_artifact_id")
            == (overview_text_artifact.id if overview_text_artifact else "")
            and final_artifact.metadata.get("front_matter_artifact_id")
            == (front_matter_artifact.id if front_matter_artifact else "")
            and (not conclusion_artifact or conclusion_current)
            and (not overview and not overview_text_artifact or overview_current)
            and (not front_matter_artifact or front_matter_current)
        )
        release_current = bool(
            final_current
            and validation_artifact
            and validation.get("valid")
            and release_artifact
            and release.get("status") == "released"
            and release.get("source_draft_artifact_id") == current_draft_id
            and validation_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
            and release_artifact.metadata.get("source_draft_artifact_id")
            == current_draft_id
        )
        release_ready = bool(release_current and release.get("release_ready"))
        docx_current = bool(
            docx
            and final_artifact
            and final_current
            and release_current
            and docx.metadata.get("source_final_artifact_id") == final_artifact.id
            and release_artifact
            and docx.metadata.get("source_release_artifact_id")
            == release_artifact.id
            and (not docx_qa_artifact or docx_qa.get("status") == "pass")
        )
        pdf_bundle = (
            manuscript_state_artifact,
            render_manifest_artifact,
            pdf_qa_artifact,
            tex_artifact,
            pdf_artifact,
            compile_log_artifact,
        )
        pdf_current = bool(
            all(pdf_bundle)
            and final_artifact
            and final_current
            and release_current
            and all(
                artifact.metadata.get("source_final_artifact_id") == final_artifact.id
                and artifact.metadata.get("source_release_artifact_id")
                == release_artifact.id
                for artifact in pdf_bundle
                if artifact is not None
            )
            and manuscript_state.get("validation", {}).get("valid")
            and render_manifest.get("template") == "modern-survey"
            and render_manifest.get("language_profile") in SUPPORTED_PROFILES
            and render_manifest.get("shell_escape") is False
            and pdf_qa.get("status") in {"pass", "pass_with_warnings"}
            and not pdf_qa.get("blocking_issues")
            and pdf_qa.get("all_fonts_embedded") is True
        )
        overview_url = f"/api/v1/artifacts/{overview.id}/content" if overview else ""
        docx_url = f"/api/v1/artifacts/{docx.id}/content" if docx else ""
        pdf_url = (
            f"/api/v1/artifacts/{pdf_artifact.id}/content" if pdf_artifact else ""
        )
        tex_url = (
            f"/api/v1/artifacts/{tex_artifact.id}/content" if tex_artifact else ""
        )
        final_jobs = [
            job
            for job in self.repository.list_project_jobs(
                principal.user_id, project_id, limit=50
            )
            if job.job_type in {
                "final.conclusion",
                "final.overview",
                "final.build",
                "final.export",
                "final.pdf",
            }
        ]
        latest_final_job = final_jobs[0] if final_jobs else None
        active_final_job = next(
            (
                job
                for job in final_jobs
                if job.status in {"queued", "running", "cancel_requested"}
            ),
            None,
        )
        evidence_boundary = dict(release.get("evidence_boundary") or {})
        if not evidence_boundary:
            evidence_boundary = self._evidence_boundary(
                self.drafts.compatibility_payload(principal, project_id),
                dict(draft_payload.get("quality") or {}),
            )
        validation_report = ""
        if validation:
            validation_report = "\n".join(
                (
                    "## Final audit",
                    "",
                    f"- Status: {'passed' if validation.get('valid') else 'blocked'}",
                    f"- Artifact references: {len(validation.get('referenced_artifact_ids') or [])}",
                    f"- Citation callouts: {', '.join(map(str, validation.get('citation_callouts') or [])) or 'none'}",
                    f"- Listed references: {', '.join(map(str, validation.get('listed_references') or [])) or 'none'}",
                    f"- Sources: {', '.join(validation.get('source_paper_ids') or []) or 'none'}",
                    f"- Blocking issues: {', '.join(validation.get('blocking_issues') or []) or 'none'}",
                    f"- Warnings: {', '.join(validation.get('warning_issues') or []) or 'none'}",
                    "",
                )
            )
        release_report = ""
        if release:
            release_report = "\n".join(
                (
                    "## Release report",
                    "",
                    f"- Status: {release.get('status', 'unknown')}",
                    f"- Publication status: {release.get('publication_status', 'review_only')}",
                    f"- Release ready: {'yes' if release.get('release_ready') else 'no'}",
                    f"- Draft artifact: {release.get('source_draft_artifact_id', '')}",
                    f"- Source papers: {', '.join(release.get('source_paper_ids') or []) or 'none'}",
                    f"- Coverage claim: {evidence_boundary.get('coverage_claim', 'selected_corpus_only')}",
                    f"- Unresolved primary papers: {len(evidence_boundary.get('unresolved_primary_paper_ids') or [])}",
                    f"- Question-level corpus gaps: {len(evidence_boundary.get('corpus_gap_questions') or [])}",
                    f"- Release integrity issues: {', '.join(release.get('release_integrity_issues') or []) or 'none'}",
                    f"- Warnings: {', '.join(release.get('validation_warning_issues') or []) or 'none'}",
                    f"- Released at: {release.get('released_at', '')}",
                    "",
                )
            )
        return {
            "project_id": project_id,
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "draft_approval_current": approved,
            "draft_approval": {
                **dict(draft_payload.get("draft_approval") or {}),
                "record": dict(draft_payload.get("draft_approval") or {}),
            },
            "final_draft_md": final_text,
            "final_artifact_id": final_artifact.id if final_artifact else "",
            "final_current": final_current,
            "conclusion_generated_md": conclusion,
            "conclusion_artifact_id": conclusion_artifact.id if conclusion_artifact else "",
            "conclusion_report": conclusion_report,
            "conclusion_report_artifact_id": (
                conclusion_report_artifact.id if conclusion_report_artifact else ""
            ),
            "conclusion_current": conclusion_current,
            "overview_figure_url": overview_url,
            "overview_figure_path": overview_url,
            "overview_figure_exists": bool(overview),
            "overview_figure_current": overview_current,
            "overview_artifact_id": overview.id if overview else "",
            "overview_text": overview_text,
            "overview_text_artifact_id": (
                overview_text_artifact.id if overview_text_artifact else ""
            ),
            "front_matter": front_matter,
            "front_matter_artifact_id": (
                front_matter_artifact.id if front_matter_artifact else ""
            ),
            "front_matter_current": front_matter_current,
            "validation": validation,
            "validation_artifact_id": validation_artifact.id if validation_artifact else "",
            "release": release,
            "evidence_boundary": evidence_boundary,
            "release_artifact_id": release_artifact.id if release_artifact else "",
            "release_current": release_current,
            "release_ready": release_ready,
            "docx_artifact_id": docx.id if docx else "",
            "docx_url": docx_url,
            "final_draft_docx_path": docx_url,
            "final_draft_docx_exists": docx_current,
            "final_draft_docx_stale": bool(docx and not docx_current),
            "docx_qa": docx_qa,
            "docx_qa_artifact_id": docx_qa_artifact.id if docx_qa_artifact else "",
            "manuscript_state": manuscript_state,
            "render_manifest": render_manifest,
            "pdf_qa": pdf_qa,
            "pdf_artifact_id": pdf_artifact.id if pdf_artifact else "",
            "pdf_url": pdf_url,
            "tex_artifact_id": tex_artifact.id if tex_artifact else "",
            "tex_url": tex_url,
            "pdf_language_profile": str(
                render_manifest.get("language_profile") or ""
            ),
            "final_pdf_exists": pdf_current,
            "final_pdf_stale": bool(pdf_artifact and not pdf_current),
            "active_final_job_id": (
                active_final_job.id if active_final_job else ""
            ),
            "active_final_job_type": (
                active_final_job.job_type if active_final_job else ""
            ),
            "latest_final_job_id": (
                latest_final_job.id if latest_final_job else ""
            ),
            "latest_final_job_type": (
                latest_final_job.job_type if latest_final_job else ""
            ),
            "latest_final_job_status": (
                latest_final_job.status if latest_final_job else ""
            ),
            "final_audit_report_md": validation_report,
            "release_report_md": release_report,
            "freshness": {
                "draft_stale": not approved,
                "final_stale": bool(final_artifact and not final_current),
                "release_stale": bool(release_artifact and not release_current),
                "pdf_stale": bool(pdf_artifact and not pdf_current),
                "stale": not approved
                or bool(final_artifact and not final_current)
                or bool(release_artifact and not release_current)
                or bool(pdf_artifact and not pdf_current),
            },
        }
