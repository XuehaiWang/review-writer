#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


STAGES: list[dict[str, Any]] = [
    {
        "id": "discovery",
        "name": "Topic discovery",
        "dir": "00_discovery",
        "skill": "review-topic-paper-discovery",
        "required": [
            "topic_input.md",
            "keyword_set.draft.json",
            "combined_results_by_keyword.json",
            "selected_discovery_results.json",
            "human_check_state.json",
        ],
        "human_check": "Check keywords and selected papers in http://127.0.0.1:8765/discovery.",
        "confirmed_by": ["human_check_state.json"],
    },
    {
        "id": "matrix_outline",
        "name": "Literature matrix and outline",
        "dir": "01_matrix_outline",
        "skill": "review-literature-matrix-outline",
        "required": [
            "paper_reading_notes.json",
            "literature_matrix.json",
            "literature_matrix.csv",
            "outline_options.md",
            "matrix_outline_report.md",
        ],
        "human_check": "Choose or edit the outline and write selected_outline.md.",
        "confirmed_by": ["selected_outline.md"],
    },
    {
        "id": "section_blueprint",
        "name": "Section blueprint",
        "dir": "01_matrix_outline",
        "skill": "review-section-blueprint",
        "required": [
            "section_blueprint.json",
            "section_writing_plan.md",
        ],
        "human_check": "Check section_blueprint.json and section_writing_plan.md before section drafting.",
        "confirmed_by": ["human_check.json"],
    },
    {
        "id": "section_drafting",
        "name": "Section drafting and figure picking",
        "dir": "02_section_drafting",
        "skill": "review-section-drafting-figure-picking",
        "required": [
            "section_tasks.json",
            "section_drafts.json",
            "section_drafts.md",
            "paper_figure_inventory.json",
            "paper_figure_candidates.json",
            "figure_candidates.json",
            "section_drafting_report.md",
        ],
        "human_check": "Check section drafts and figure candidates before redraw.",
        "confirmed_by": ["human_check.json"],
    },
    {
        "id": "figure_redraw",
        "name": "Figure style redraw",
        "dir": "03_figure_redraw",
        "skill": "review-figure-style-redraw",
        "required": [
            "style_config.json",
            "source_figure_manifest.json",
            "redrawn_figure_manifest.json",
            "figure_redraw_report.md",
        ],
        "human_check": "Compare every redrawn figure with its source before merging.",
        "confirmed_by": ["human_check.json"],
        "skip_anchor": "skip_reason.md",
    },
    {
        "id": "first_draft",
        "name": "First draft merge",
        "dir": "04_first_draft",
        "skill": "review-draft-merge-polish",
        "required": [
            "draft_bundle.json",
            "first_draft.md",
            "citations.json",
            "merge_report.md",
            "remaining_issues.md",
        ],
        "human_check": "Review the unified first draft in http://127.0.0.1:8765/draft.",
        "confirmed_by": ["human_check.json"],
    },
    {
        "id": "conclusion_generation",
        "name": "Conclusion generation",
        "dir": "04_first_draft",
        "skill": "review-conclusion-generator",
        "required": [
            "conclusion_generated.md",
            "conclusion_quality_report.json",
        ],
        "human_check": "No additional human confirmation required.",
    },
    {
        "id": "final_audit",
        "name": "Final content and format audit",
        "dir": "05_final_audit",
        "skill": "review-final-audit-release",
        "required": [
            "format_scan.json",
            "format_scan.md",
            "content_audit_report.md",
            "format_audit_report.md",
            "conclusion_integration.json",
            "final_draft.md",
            "final_remaining_issues.md",
            "release_report.md",
        ],
        "human_check": "Check final_draft.md and release_report.md before export.",
        "confirmed_by": ["human_check.json"],
    },
    {
        "id": "summary_chart",
        "name": "Review summary chart",
        "dir": "05_final_audit",
        "skill": "review-outline-summary-chart",
        "required": [
            "review_summary_chart.html",
            "review_summary_chart.json",
        ],
        "human_check": "No additional human confirmation required.",
    },
    {
        "id": "docx_export",
        "name": "DOCX export",
        "dir": "05_final_audit",
        "skill": "review-export-docx",
        "required": [
            "final_draft.docx",
        ],
        "human_check": "Download final_draft.docx from /final and open it in Word to confirm styling.",
        "confirmed_by": ["human_check.json"],
    },
]


NUMERIC_CITATION_RE = re.compile(r"\[\d+(?:\s*[-,]\s*\d+)*\]")
RAW_PAPER_ID_RE = re.compile(r"\bP\d+\b")
LOWERCASE_SHA256_RE = re.compile(r"[0-9a-f]{64}")
ATX_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
FENCE_OPEN_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^\s{0,3}(`+|~+)\s*$")
INTEGRATOR_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
INTEGRATOR_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INTEGRATOR_CONCLUSION_HEADINGS = {
    "conclusion",
    "conclusions",
    "challenges",
    "outlook",
    "future direction",
    "future directions",
    "insight",
    "insights",
    "总结",
    "结论",
    "挑战",
    "展望",
}
CONCLUSION_HEADINGS = {
    name.casefold()
    for name in (
        "Conclusion",
        "Conclusions",
        "Challenge",
        "Challenges",
        "Outlook",
        "Future Direction",
        "Future Directions",
        "Insight",
        "Insights",
        "鎬荤粨",
        "缁撹",
        "鎸戞垬",
        "灞曟湜",
        "总结",
        "结论",
        "挑战",
        "展望",
    )
}
REFERENCE_HEADINGS = {
    name.casefold()
    for name in (
        "References",
        "Reference List",
        "Bibliography",
        "Cited Literature",
        "鍙傝€冩枃鐚?",
        "参考文献",
    )
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def markdown_heading_positions(
    text: str, accepted: set[str], *, allow_slash_suffix: bool = False
) -> list[int]:
    positions: list[int] = []
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(text.splitlines()):
        if fence_character is not None:
            closing = FENCE_CLOSE_RE.match(line)
            if (
                closing
                and closing.group(1)[0] == fence_character
                and len(closing.group(1)) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            continue
        opening = FENCE_OPEN_RE.match(line)
        if opening:
            fence_character = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        match = ATX_HEADING_RE.match(line)
        if not match:
            continue
        heading = re.sub(r"\s+#+\s*$", "", match.group(1)).strip()
        if allow_slash_suffix:
            heading = heading.split("/", 1)[0].strip()
        if heading.casefold() in accepted:
            positions.append(line_number)
    return positions


def markdown_heading_entries(text: str) -> list[tuple[int, int, str, str]]:
    entries: list[tuple[int, int, str, str]] = []
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(text.splitlines()):
        if fence_character is not None:
            closing = FENCE_CLOSE_RE.match(line)
            if (
                closing
                and closing.group(1)[0] == fence_character
                and len(closing.group(1)) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            continue
        opening = FENCE_OPEN_RE.match(line)
        if opening:
            fence_character = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        match = ATX_HEADING_RE.match(line)
        if not match:
            continue
        raw_heading = line.strip()
        heading = re.sub(r"\s+#+\s*$", "", match.group(1)).strip()
        level = len(raw_heading) - len(raw_heading.lstrip("#"))
        entries.append((line_number, level, heading, raw_heading))
    return entries


def markdown_body_paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    fence_character: str | None = None
    fence_length = 0

    def flush() -> None:
        paragraph = "\n".join(current).strip()
        if paragraph and re.search(r"\w", paragraph, re.UNICODE):
            paragraphs.append(paragraph)
        current.clear()

    for line in text.splitlines():
        if fence_character is not None:
            closing = FENCE_CLOSE_RE.match(line)
            if (
                closing
                and closing.group(1)[0] == fence_character
                and len(closing.group(1)) >= fence_length
            ):
                fence_character = None
                fence_length = 0
            continue
        opening = FENCE_OPEN_RE.match(line)
        if opening:
            flush()
            fence_character = opening.group(1)[0]
            fence_length = len(opening.group(1))
        elif ATX_HEADING_RE.match(line):
            flush()
        elif not line.strip():
            flush()
        else:
            current.append(line)
    flush()
    return paragraphs


def numeric_callout_identities(text: str) -> list[int]:
    identities: set[int] = set()
    for match in NUMERIC_CITATION_RE.finditer(text):
        inner = match.group(0)[1:-1]
        for part in re.split(r"\s*,\s*", inner):
            if "-" in part:
                left, right = (value.strip() for value in part.split("-", 1))
                if left.isdigit() and right.isdigit() and int(left) <= int(right):
                    identities.update(range(int(left), int(right) + 1))
            elif part.strip().isdigit():
                identities.add(int(part.strip()))
    return sorted(identities)


def generated_conclusion_provenance(text: str) -> tuple[str, list[int]] | None:
    clean_text = text.strip()
    fence_character: str | None = None
    fence_length = 0
    derived_heading: str | None = None
    for line in clean_text.splitlines():
        fence_match = INTEGRATOR_FENCE_RE.match(line)
        if fence_character is not None:
            if fence_match:
                marker, remainder = fence_match.groups()
                if (
                    marker[0] == fence_character
                    and len(marker) >= fence_length
                    and not remainder.strip()
                ):
                    fence_character = None
                    fence_length = 0
            continue
        if fence_match:
            marker, remainder = fence_match.groups()
            if marker[0] != "`" or "`" not in remainder:
                fence_character = marker[0]
                fence_length = len(marker)
            continue
        heading_match = INTEGRATOR_HEADING_RE.match(line)
        if not heading_match:
            continue
        title = re.sub(r"[ \t]+#+[ \t]*$", "", heading_match.group(2)).strip()
        base_title = title.split("/", 1)[0].strip()
        if base_title.casefold() in INTEGRATOR_CONCLUSION_HEADINGS:
            derived_heading = line.strip()
            break
    if derived_heading is None:
        return None
    return derived_heading, numeric_callout_identities(clean_text)


def valid_citations_data(data: Any) -> bool:
    if isinstance(data, list):
        slots = (
            (item.get("callout", item.get("index")), item)
            for item in data
            if isinstance(item, dict)
        )
    elif isinstance(data, dict):
        slots = data.items()
    else:
        return False

    for raw_callout, slot in slots:
        if not re.fullmatch(r"(?:\[\d+\]|\d+)", str(raw_callout).strip()):
            continue
        if isinstance(slot, str) and slot.strip():
            return True
        if not isinstance(slot, dict):
            continue
        paper_id = slot.get("paper_id")
        if isinstance(paper_id, str) and paper_id.strip():
            return True
        for key in ("paper_ids", "cited_paper_ids"):
            paper_ids = slot.get(key)
            if isinstance(paper_ids, list) and any(
                isinstance(value, str) and value.strip() for value in paper_ids
            ):
                return True
    return False


def conclusion_receipt_is_valid(project: Path, final_text: str) -> bool:
    first_draft = project / "04_first_draft" / "first_draft.md"
    generated_conclusion = project / "04_first_draft" / "conclusion_generated.md"
    stage_dir = project / "05_final_audit"
    receipt = read_json(stage_dir / "conclusion_integration.json")
    if (
        not isinstance(receipt, dict)
        or not isinstance(receipt.get("schema_version"), int)
        or isinstance(receipt.get("schema_version"), bool)
        or receipt.get("schema_version") != 1
    ):
        return False

    for field, expected in (
        ("first_draft_path", first_draft),
        ("generated_conclusion_path", generated_conclusion),
    ):
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
        source = Path(value)
        if not source.is_absolute():
            source = stage_dir / source
        if source.resolve() != expected.resolve():
            return False

    for field, source in (
        ("first_draft_sha256", first_draft),
        ("generated_conclusion_sha256", generated_conclusion),
    ):
        digest = receipt.get(field)
        if (
            not isinstance(digest, str)
            or not LOWERCASE_SHA256_RE.fullmatch(digest)
            or not source.exists()
            or digest != sha256_file(source)
        ):
            return False

    try:
        generated_text = generated_conclusion.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    producer_values = generated_conclusion_provenance(generated_text)
    if producer_values is None:
        return False
    producer_heading, producer_callouts = producer_values
    if (
        receipt.get("inserted_conclusion_heading") != producer_heading
        or receipt.get("generated_conclusion_callouts") != producer_callouts
    ):
        return False

    integrated_seed = receipt.get("integrated_final_draft_sha256")
    if not isinstance(integrated_seed, str) or not LOWERCASE_SHA256_RE.fullmatch(
        integrated_seed
    ):
        return False

    receipt_heading = receipt.get("inserted_conclusion_heading")
    if not isinstance(receipt_heading, str) or not receipt_heading.strip():
        return False
    entries = markdown_heading_entries(final_text)
    matches = [entry for entry in entries if entry[3] == receipt_heading.strip()]
    references = [
        entry for entry in entries if entry[2].casefold() in REFERENCE_HEADINGS
    ]
    if len(matches) != 1 or not references or matches[0][0] >= references[0][0]:
        return False

    receipt_callouts = receipt.get("generated_conclusion_callouts")
    if (
        not isinstance(receipt_callouts, list)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in receipt_callouts)
        or receipt_callouts != sorted(set(receipt_callouts))
    ):
        return False

    conclusion_line, conclusion_level, _, _ = matches[0]
    section_end = references[0][0]
    for line_number, level, _, _ in entries:
        if (
            conclusion_line < line_number < section_end
            and level <= conclusion_level
        ):
            section_end = line_number
            break
    section_lines = final_text.splitlines()[conclusion_line + 1 : section_end]
    return numeric_callout_identities("\n".join(section_lines)) == receipt_callouts


def summary_chart_semantic_issues(project: Path) -> list[str]:
    stage_dir = project / "05_final_audit"
    final_draft = stage_dir / "final_draft.md"
    chart_path = stage_dir / "review_summary_chart.json"
    html_path = stage_dir / "review_summary_chart.html"
    chart = read_json(chart_path)
    stats = chart.get("stats") if isinstance(chart, dict) else None
    issues: list[str] = []

    source_matches = False
    if final_draft.exists() and isinstance(stats, dict):
        draft_source = stats.get("draft_source")
        if isinstance(draft_source, str) and draft_source.strip():
            source_path = Path(draft_source)
            if not source_path.is_absolute():
                source_path = stage_dir / source_path
            source_matches = source_path.resolve() == final_draft.resolve()
    if not source_matches:
        issues.append("summary_chart_not_from_final_draft")

    digest_matches = False
    if isinstance(stats, dict):
        draft_sha256 = stats.get("draft_sha256")
        if (
            isinstance(draft_sha256, str)
            and LOWERCASE_SHA256_RE.fullmatch(draft_sha256)
            and final_draft.exists()
        ):
            digest_matches = draft_sha256 == sha256_file(final_draft)
    if not digest_matches:
        issues.append("summary_chart_stale")

    if not isinstance(stats, dict) or stats.get("generation_scope") != "both":
        issues.append("summary_chart_not_generated_with_both")

    html_digest_matches = False
    if isinstance(stats, dict):
        html_sha256 = stats.get("html_sha256")
        if (
            isinstance(html_sha256, str)
            and LOWERCASE_SHA256_RE.fullmatch(html_sha256)
            and html_path.exists()
        ):
            html_digest_matches = html_sha256 == sha256_file(html_path)
    if not html_digest_matches and "summary_chart_stale" not in issues:
        issues.append("summary_chart_stale")

    return issues


def discover_projects(review_root: Path) -> list[str]:
    root = review_root / "review-projects"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def stage_status(project: Path, stage: dict[str, Any]) -> dict[str, Any]:
    stage_dir = project / stage["dir"]
    missing = [name for name in stage["required"] if not (stage_dir / name).exists()]
    semantic_issues: list[str] = []
    if stage["id"] == "figure_redraw":
        skip_anchor = stage_dir / stage.get("skip_anchor", "skip_reason.md")
        skip_active = skip_anchor.exists() and bool(skip_anchor.read_text(encoding="utf-8", errors="ignore").strip())
        if skip_active:
            # User explicitly opted out. Clear `missing` so the stage can complete.
            missing = []
        else:
            manifest = read_json(stage_dir / "redrawn_figure_manifest.json")
            if isinstance(manifest, dict):
                if manifest.get("status") == "skipped":
                    semantic_issues.append("figure_redraw_skipped_without_reason")
                figures = manifest.get("figures")
                if isinstance(figures, list) and not any(isinstance(f, dict) and f.get("status") == "redrawn" for f in figures):
                    semantic_issues.append("no_redrawn_figures")
            elif not missing:
                semantic_issues.append("invalid_redrawn_figure_manifest")
    if stage["id"] == "section_drafting":
        candidates = read_json(stage_dir / "figure_candidates.json")
        if isinstance(candidates, dict):
            figures = candidates.get("figures")
        else:
            figures = candidates
        if isinstance(figures, list) and not figures:
            semantic_issues.append("empty_figure_candidates")
        elif not isinstance(figures, list) and "figure_candidates.json" not in missing:
            semantic_issues.append("invalid_figure_candidates")
        sections_dir = stage_dir / "sections"
        section_files = sorted(sections_dir.glob("*.md")) if sections_dir.exists() else []
        if not section_files:
            semantic_issues.append("sections_directory_empty")
        tasks = read_json(stage_dir / "section_tasks.json")
        task_list = tasks if isinstance(tasks, list) else (tasks.get("sections") if isinstance(tasks, dict) else None)
        if isinstance(task_list, list) and section_files:
            have = {p.stem for p in section_files}
            missing_ids = [t.get("section_id") for t in task_list if isinstance(t, dict) and t.get("section_id") and t["section_id"] not in have]
            if missing_ids:
                semantic_issues.append("section_files_missing_for_tasks")
    if stage["id"] == "first_draft":
        citations = read_json(stage_dir / "citations.json")
        if "citations.json" not in missing and not valid_citations_data(citations):
            semantic_issues.append("invalid_citations_json")
    if stage["id"] == "conclusion_generation":
        report = read_json(stage_dir / "conclusion_quality_report.json")
        paragraphs = report.get("paragraphs") if isinstance(report, dict) else None
        validation = report.get("validation") if isinstance(report, dict) else None
        substantive_paragraphs = (
            paragraphs
            if isinstance(paragraphs, list)
            and 2 <= len(paragraphs) <= 3
            and all(
                isinstance(paragraph, dict)
                and isinstance(paragraph.get("content"), str)
                and bool(paragraph["content"].strip())
                for paragraph in paragraphs
            )
            else None
        )
        actual_word_count = (
            sum(
                len(re.findall(r"\b\w+\b", paragraph["content"]))
                for paragraph in substantive_paragraphs
            )
            if substantive_paragraphs is not None
            else None
        )
        count_fields: list[Any] = []
        if isinstance(report, dict) and "total_words" in report:
            count_fields.append(report.get("total_words"))
        if isinstance(validation, dict) and "total_words" in validation:
            count_fields.append(validation.get("total_words"))
        count_fields_valid = bool(count_fields) and all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value == actual_word_count
            for value in count_fields
        )
        paragraph_count_valid = (
            not isinstance(validation, dict)
            or "paragraph_count" not in validation
            or validation.get("paragraph_count") == (
                len(substantive_paragraphs)
                if substantive_paragraphs is not None
                else None
            )
        )
        generated_path = stage_dir / "conclusion_generated.md"
        generated_text = (
            generated_path.read_text(encoding="utf-8", errors="ignore")
            if generated_path.exists()
            else ""
        )
        rendered_paragraphs = markdown_body_paragraphs(generated_text)
        report_valid = (
            isinstance(validation, dict)
            and validation.get("passes_validation") is True
            and substantive_paragraphs is not None
            and len(rendered_paragraphs) == len(substantive_paragraphs)
            and count_fields_valid
            and paragraph_count_valid
        )
        if not report_valid or not NUMERIC_CITATION_RE.search(generated_text):
            semantic_issues.append("conclusion_validation_failed")
        if RAW_PAPER_ID_RE.search(generated_text):
            semantic_issues.append("conclusion_contains_raw_paper_ids")
    if stage["id"] == "summary_chart":
        semantic_issues.extend(summary_chart_semantic_issues(project))
    if stage["id"] == "docx_export":
        # DOCX is only valid when the final audit passed all blocking checks.
        final_scan = read_json(project / "05_final_audit" / "format_scan.json")
        if isinstance(final_scan, dict) and final_scan.get("blocking_issues"):
            semantic_issues.append("final_audit_has_blocking_issues")
        elif not (project / "05_final_audit" / "final_draft.md").exists():
            semantic_issues.append("final_draft_md_missing")
        chart_artifacts_present = all(
            (project / "05_final_audit" / name).exists()
            for name in ("review_summary_chart.html", "review_summary_chart.json")
        )
        if not chart_artifacts_present or summary_chart_semantic_issues(project):
            semantic_issues.append("summary_chart_incomplete")
    if stage["id"] in {"first_draft", "final_audit"}:
        draft_path = stage_dir / ("first_draft.md" if stage["id"] == "first_draft" else "final_draft.md")
        if draft_path.exists():
            draft_text = draft_path.read_text(encoding="utf-8", errors="ignore")
            has_image = bool(re.search(r"!\[[^\]]*\]\(([^)]+)\)", draft_text))
            has_citation = bool(NUMERIC_CITATION_RE.search(draft_text))
            has_references = bool(
                markdown_heading_positions(draft_text, REFERENCE_HEADINGS)
            )
            skip_reason = project / "03_figure_redraw" / "skip_reason.md"
            figures_skipped_with_reason = skip_reason.exists() and bool(skip_reason.read_text(encoding="utf-8", errors="ignore").strip())
            if not has_image and not figures_skipped_with_reason:
                semantic_issues.append("draft_has_no_figures")
            if not has_citation:
                semantic_issues.append("draft_has_no_citation_callouts")
            if not has_references:
                semantic_issues.append("missing_references_section")
        if stage["id"] == "final_audit":
            conclusion_positions = (
                markdown_heading_positions(
                    draft_text, CONCLUSION_HEADINGS, allow_slash_suffix=True
                )
                if draft_path.exists()
                else []
            )
            reference_positions = (
                markdown_heading_positions(draft_text, REFERENCE_HEADINGS)
                if draft_path.exists()
                else []
            )
            if not (
                len(conclusion_positions) == 1
                and reference_positions
                and conclusion_positions[0] < reference_positions[0]
            ):
                semantic_issues.append(
                    "generated_conclusion_missing_from_final_draft"
                )
            if not conclusion_receipt_is_valid(project, draft_text if draft_path.exists() else ""):
                if "generated_conclusion_missing_from_final_draft" not in semantic_issues:
                    semantic_issues.append(
                        "generated_conclusion_missing_from_final_draft"
                    )
            scan = read_json(stage_dir / "format_scan.json")
            if isinstance(scan, dict):
                blockers = scan.get("blocking_issues") or []
                for issue in blockers:
                    if issue not in semantic_issues:
                        semantic_issues.append(issue)
    complete = not missing and not semantic_issues
    expects_confirmation = bool(stage.get("confirmed_by"))
    confirmed = False
    confirmation_notes: list[str] = []
    for name in stage.get("confirmed_by", []):
        path = stage_dir / name
        if not path.exists():
            continue
        if path.suffix == ".json":
            data = read_json(path)
            if isinstance(data, dict):
                confirmed = bool(data.get("confirmed") or data.get("human_confirmed") or data.get("reviewed"))
                if confirmed:
                    confirmation_notes.append(name)
        else:
            if path.read_text(encoding="utf-8", errors="ignore").strip():
                confirmed = True
                confirmation_notes.append(name)
    if expects_confirmation and not confirmed:
        complete = False
    return {
        "id": stage["id"],
        "name": stage["name"],
        "skill": stage["skill"],
        "directory": str(stage_dir),
        "complete": complete,
        "expects_confirmation": expects_confirmation,
        "missing": missing,
        "semantic_issues": semantic_issues,
        "human_check": stage["human_check"],
        "confirmed": confirmed,
        "confirmation_notes": confirmation_notes,
        "skipped_by_user": bool(stage.get("skip_anchor")) and (stage_dir / stage.get("skip_anchor", "")).exists(),
    }


def summarize(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    if not project.exists():
        return {
            "project_id": project_id,
            "exists": False,
            "error": f"Project not found: {project}",
            "available_projects": discover_projects(review_root),
        }

    stages = [stage_status(project, stage) for stage in STAGES]
    completed = [s for s in stages if s["complete"]]
    # Skip stages explicitly opted out by the user (skip_reason.md present).
    next_stage = next((s for s in stages if not s["complete"] and not s.get("skipped_by_user")), None)
    blocking_check = None
    # A stage that has all artifacts/checks done but is still waiting for human
    # confirmation should drive the blocking message uniformly.
    for stage in stages:
        if stage.get("expects_confirmation") and not stage["confirmed"]:
            inputs_ready = not stage["missing"] and not stage["semantic_issues"]
            if inputs_ready:
                blocking_check = stage["human_check"]
                break
    if blocking_check is None and next_stage is None:
        blocking_check = stages[-1]["human_check"]

    return {
        "project_id": project_id,
        "exists": True,
        "project_dir": str(project),
        "completed_stage_ids": [s["id"] for s in completed],
        "next_stage": next_stage,
        "blocking_human_check": blocking_check,
        "stages": stages,
    }


def print_text(summary: dict[str, Any]) -> None:
    if not summary.get("exists"):
        print(f"Project: {summary['project_id']}")
        print(summary["error"])
        if summary.get("available_projects"):
            print("Available projects:")
            for project in summary["available_projects"]:
                print(f"- {project}")
        return

    print(f"Project: {summary['project_id']}")
    print(f"Completed stages: {', '.join(summary['completed_stage_ids']) or 'none'}")
    if summary.get("blocking_human_check"):
        print(f"Blocking human check: {summary['blocking_human_check']}")
    next_stage = summary.get("next_stage")
    if next_stage:
        print(f"Next skill: {next_stage['skill']}")
        print(f"Next stage: {next_stage['name']}")
        if next_stage["missing"]:
            print("Missing files:")
            for name in next_stage["missing"]:
                print(f"- {name}")
        if next_stage.get("semantic_issues"):
            print("Stage issues:")
            for issue in next_stage["semantic_issues"]:
                print(f"- {issue}")
    else:
        print("Next skill: none")
        print("Status: all ten workflow stages are complete")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect review project workflow status.")
    parser.add_argument("--review-root", default="/home/ps/review-writer")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = summarize(Path(args.review_root).resolve(), args.project_id)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print_text(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
