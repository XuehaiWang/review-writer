#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple


HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
REF_CALLOUT_RE = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")
REFERENCES_TITLES = {
    "references",
    "reference list",
    "bibliography",
    "cited literature",
    "参考文献",
}
CONCLUSION_TITLES = {
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


class Heading(NamedTuple):
    start: int
    level: int
    title: str


def _clean_heading_title(title: str) -> str:
    return re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()


def _headings(text: str) -> list[Heading]:
    headings: list[Heading] = []
    fence_character: str | None = None
    fence_length = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        fence_match = FENCE_RE.match(content)
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
            offset += len(line)
            continue
        if fence_match:
            marker, remainder = fence_match.groups()
            if marker[0] != "`" or "`" not in remainder:
                fence_character = marker[0]
                fence_length = len(marker)
            offset += len(line)
            continue
        heading_match = HEADING_RE.match(content)
        if heading_match:
            headings.append(
                Heading(
                    start=offset + heading_match.start(),
                    level=len(heading_match.group(1)),
                    title=_clean_heading_title(heading_match.group(2)),
                )
            )
        offset += len(line)
    return headings


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


def _is_references(title: str) -> bool:
    return _matches_any_segment(_strip_outline_prefix(title), REFERENCES_TITLES)


def _is_conclusion_like(title: str) -> bool:
    base_title = title.split("/", 1)[0].strip()
    base_title = _strip_outline_prefix(base_title)
    return _matches_any_segment(base_title, CONCLUSION_TITLES)


def _generated_conclusion_heading(conclusion: str) -> Heading:
    heading = next(
        (heading for heading in _headings(conclusion) if _is_conclusion_like(heading.title)),
        None,
    )
    if heading is None:
        raise ValueError("generated conclusion heading not found")
    return heading


def _heading_text(text: str, heading: Heading) -> str:
    return text[heading.start :].splitlines()[0].strip()


def _numeric_callouts(text: str) -> list[int]:
    callouts: set[int] = set()
    for match in REF_CALLOUT_RE.finditer(text):
        for part in re.split(r"\s*,\s*", match.group(1)):
            if "-" in part:
                left, right = (value.strip() for value in part.split("-", 1))
                if left.isdigit() and right.isdigit() and int(left) <= int(right):
                    callouts.update(range(int(left), int(right) + 1))
            elif part.strip().isdigit():
                callouts.add(int(part.strip()))
    return sorted(callouts)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(path)


def integrate_conclusion(first_draft: str, conclusion: str) -> str:
    clean_conclusion = conclusion.strip()
    if not clean_conclusion:
        raise ValueError("generated conclusion is empty")
    _generated_conclusion_heading(clean_conclusion)

    headings = _headings(first_draft)
    reference_index = next(
        (index for index, heading in enumerate(headings) if _is_references(heading.title)),
        None,
    )
    if reference_index is None:
        raise ValueError("References heading not found")

    reference_heading = headings[reference_index]
    removable_spans: list[tuple[int, int]] = []
    for index, heading in enumerate(headings):
        if heading.level != reference_heading.level or not _is_conclusion_like(heading.title):
            continue
        section_end = len(first_draft)
        for following in headings[index + 1 :]:
            if following.level <= heading.level:
                section_end = following.start
                break
        removable_spans.append((heading.start, section_end))

    without_old_conclusions = first_draft
    for start, end in reversed(removable_spans):
        without_old_conclusions = (
            without_old_conclusions[:start] + without_old_conclusions[end:]
        )

    remaining_headings = _headings(without_old_conclusions)
    remaining_reference = next(
        heading for heading in remaining_headings if _is_references(heading.title)
    )
    prefix = without_old_conclusions[: remaining_reference.start].rstrip("\r\n")
    references = without_old_conclusions[remaining_reference.start :]
    if prefix:
        integrated = f"{prefix}\n\n{clean_conclusion}\n\n{references}"
    else:
        integrated = f"{clean_conclusion}\n\n{references}"
    return integrated.rstrip("\r\n") + "\n"


def run(args: argparse.Namespace) -> int:
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
    first_draft_path = project / "05_first_draft" / "first_draft.md"
    conclusion_path = project / "06_conclusion_generation" / "conclusion_generated.md"
    try:
        first_draft_bytes = first_draft_path.read_bytes()
        conclusion_bytes = conclusion_path.read_bytes()
    except FileNotFoundError as exc:
        print(f"Missing input file: {exc.filename}", file=sys.stderr)
        return 2

    source_files = (
        (first_draft_path, first_draft_bytes),
        (conclusion_path, conclusion_bytes),
    )
    for path, data in source_files:
        if not data.strip():
            print(f"Input file is blank: {path}", file=sys.stderr)
            return 2
    try:
        first_draft = first_draft_bytes.decode("utf-8")
        conclusion = conclusion_bytes.decode("utf-8")
        final_draft = integrate_conclusion(first_draft, conclusion)
        clean_conclusion = conclusion.strip()
        conclusion_heading = _generated_conclusion_heading(clean_conclusion)
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"Cannot integrate conclusion: {exc}", file=sys.stderr)
        return 2

    output_path = project / "07_final_audit" / "final_draft.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_draft_bytes = final_draft.encode("utf-8")
    _atomic_write_bytes(output_path, final_draft_bytes)

    receipt = {
        "schema_version": 1,
        "first_draft_path": str(first_draft_path.resolve()),
        "generated_conclusion_path": str(conclusion_path.resolve()),
        "first_draft_sha256": _sha256(first_draft_bytes),
        "generated_conclusion_sha256": _sha256(conclusion_bytes),
        "inserted_conclusion_heading": _heading_text(
            clean_conclusion,
            conclusion_heading,
        ),
        "generated_conclusion_callouts": _numeric_callouts(clean_conclusion),
        "integrated_final_draft_sha256": _sha256(final_draft_bytes),
    }
    receipt_path = output_path.parent / "conclusion_integration.json"
    receipt_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(receipt_path, receipt_bytes)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Integrate a generated conclusion into a review draft."
    )
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
