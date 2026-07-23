#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


STAGES: list[dict[str, Any]] = [
    {
        "id": "discovery",
        "name": "Online paper discovery and corpus download",
        "dir": "00_discovery",
        "skill": "review-online-paper-discovery",
        "required": [
            "online_search_topic.md",
            "online_search_keywords.json",
            "online_search_results_by_keyword.json",
            "online_search_candidates.json",
            "online_search_report.md",
            "online_search_human_check_state.json",
            "online_search_download_manifest.json",
        ],
        "human_check": "Check candidates and confirm in http://127.0.0.1:8765/discovery.",
        "confirmed_by": ["online_search_human_check_state.json"],
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
        "dir": "02_section_blueprint",
        "skill": "review-section-blueprint",
        "required": [
            "section_blueprint.json",
            "section_writing_plan.md",
        ],
        "human_check": "Check section_blueprint.json and section_writing_plan.md before section drafting.",
        "confirmed_by": [],
    },
    {
        "id": "section_drafting",
        "name": "Section drafting and figure picking",
        "dir": "03_section_drafting",
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
        "confirmed_by": [],
    },
    {
        "id": "figure_redraw",
        "name": "Figure style redraw",
        "dir": "04_figure_redraw",
        "skill": "review-figure-style-redraw",
        "required": [
            "style_config.json",
            "source_figure_manifest.json",
            "redrawn_figure_manifest.json",
            "figure_redraw_report.md",
        ],
        "human_check": "Compare every redrawn figure with its source before merging.",
        "confirmed_by": [],
        "optional_skip": True,
    },
    {
        "id": "first_draft",
        "name": "First draft merge",
        "dir": "05_first_draft",
        "skill": "review-draft-merge-polish",
        "required": [
            "draft_bundle.json",
            "first_draft.md",
            "merge_report.md",
            "remaining_issues.md",
        ],
        "human_check": "Review the unified first draft in http://127.0.0.1:8765/draft.",
        "confirmed_by": [],
    },
    {
        "id": "conclusion_generation",
        "name": "Conclusion generation",
        "dir": "06_conclusion_generation",
        "skill": "review-conclusion-generator",
        "required": [
            "conclusion_generated.md",
            "conclusion_quality_report.json",
        ],
        "human_check": "No additional human confirmation required.",
        "confirmed_by": [],
    },
    {
        "id": "final_audit",
        "name": "Final content and format audit",
        "dir": "07_final_audit",
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
        "confirmed_by": [],
    },
    {
        "id": "summary_chart",
        "name": "Review summary chart",
        "dir": "08_summary_chart",
        "skill": "review-outline-summary-chart",
        "required": [
            "review_summary_chart.html",
            "review_summary_chart.json",
        ],
        "human_check": "No additional human confirmation required.",
        "confirmed_by": [],
    },
    {
        "id": "docx_export",
        "name": "DOCX export",
        "dir": "09_docx_export",
        "skill": "review-export-docx",
        "required": [
            "final_draft.docx",
        ],
        "human_check": "Download final_draft.docx from /final and open it in Word to confirm styling.",
        "confirmed_by": [],
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


def _strip_outline_prefix(title: str) -> str:
    """Strip a leading outline number ("8. ", "8) ", "8: ", "8 ") so headings
    like "8. Conclusion and Outlook" are recognized the same as "Conclusion"."""
    return re.sub(r"^\d+[.):]?\s+", "", title)


def _matches_any_segment(title: str, accepted: set[str]) -> bool:
    """Exact match, or match against any "X and Y"-joined segment -- real
    headings are often compound (e.g. "Conclusion and Outlook", "References
    and Notes"), not the bare single-word form."""
    if title.casefold() in accepted:
        return True
    return any(
        segment.strip().casefold() in accepted
        for segment in re.split(r"\s+and\s+", title, flags=re.I)
    )


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
        heading = _strip_outline_prefix(heading)
        if _matches_any_segment(heading, accepted):
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
        base_title = _strip_outline_prefix(base_title)
        if _matches_any_segment(base_title, INTEGRATOR_CONCLUSION_HEADINGS):
            derived_heading = line.strip()
            break
    if derived_heading is None:
        return None
    return derived_heading, numeric_callout_identities(clean_text)


def valid_citation_map(data: Any) -> bool:
    """Validate draft_bundle.json's citation_map shape: {paper_id: positive int}."""
    if not isinstance(data, dict) or not data:
        return False
    for paper_id, callout in data.items():
        if not isinstance(paper_id, str) or not paper_id.strip():
            return False
        if not isinstance(callout, int) or isinstance(callout, bool) or callout <= 0:
            return False
    return True


def conclusion_receipt_is_valid(project: Path, final_text: str) -> bool:
    first_draft = project / "05_first_draft" / "first_draft.md"
    generated_conclusion = project / "06_conclusion_generation" / "conclusion_generated.md"
    stage_dir = project / "07_final_audit"
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
    chart_dir = project / "08_summary_chart"
    final_draft = project / "07_final_audit" / "final_draft.md"
    chart_path = chart_dir / "review_summary_chart.json"
    html_path = chart_dir / "review_summary_chart.html"
    chart = read_json(chart_path)
    stats = chart.get("stats") if isinstance(chart, dict) else None
    issues: list[str] = []

    source_matches = False
    if final_draft.exists() and isinstance(stats, dict):
        draft_source = stats.get("draft_source")
        if isinstance(draft_source, str) and draft_source.strip():
            source_path = Path(draft_source)
            if not source_path.is_absolute():
                source_path = chart_dir / source_path
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
        manifest = read_json(stage_dir / "redrawn_figure_manifest.json")
        if isinstance(manifest, dict):
            if manifest.get("status") == "skipped":
                semantic_issues.append("figure_redraw_skipped")
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
    if stage["id"] == "first_draft":
        bundle = read_json(stage_dir / "draft_bundle.json")
        citation_map = bundle.get("citation_map") if isinstance(bundle, dict) else None
        if not missing and not valid_citation_map(citation_map):
            semantic_issues.append("invalid_citation_map")
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
    if stage["id"] == "final_audit":
        final_draft_path = stage_dir / "final_draft.md"
        final_text = (
            final_draft_path.read_text(encoding="utf-8", errors="ignore")
            if final_draft_path.exists()
            else ""
        )
        conclusion_positions = (
            markdown_heading_positions(final_text, CONCLUSION_HEADINGS, allow_slash_suffix=True)
            if final_text
            else []
        )
        reference_positions = (
            markdown_heading_positions(final_text, REFERENCE_HEADINGS) if final_text else []
        )
        conclusion_placed_correctly = (
            len(conclusion_positions) == 1
            and bool(reference_positions)
            and conclusion_positions[0] < reference_positions[0]
        )
        if not conclusion_placed_correctly or not conclusion_receipt_is_valid(project, final_text):
            semantic_issues.append("generated_conclusion_missing_from_final_draft")
    if stage["id"] == "summary_chart":
        semantic_issues.extend(summary_chart_semantic_issues(project))
    if stage["id"] == "docx_export":
        if not (project / "07_final_audit" / "final_draft.md").exists():
            semantic_issues.append("final_draft_md_missing")
        chart_artifacts_present = all(
            (project / "08_summary_chart" / name).exists()
            for name in ("review_summary_chart.html", "review_summary_chart.json")
        )
        if not chart_artifacts_present or summary_chart_semantic_issues(project):
            semantic_issues.append("summary_chart_incomplete")
    complete = not missing
    intentionally_skipped = stage.get("optional_skip") and "figure_redraw_skipped" in semantic_issues
    if semantic_issues and not intentionally_skipped:
        complete = False
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
                confirmed = bool(
                    data.get("confirmed")
                    or data.get("human_confirmed")
                    or data.get("reviewed")
                    or data.get("status") == "confirmed"
                )
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
        "optional_skip": bool(stage.get("optional_skip")),
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
    next_stage = next((s for s in stages if not s["complete"]), None)
    blocking_check = None
    # A stage that has all artifacts/checks done but is still waiting for human
    # confirmation should drive the blocking message uniformly, regardless of
    # its position relative to next_stage.
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
    parser.add_argument("--review-root", default=str(Path.cwd()))
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
