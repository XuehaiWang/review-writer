#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BOOTSTRAP_ROOT = next(
    (
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "review_writer_core").is_dir() and (parent / "skills").is_dir()
    ),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.review_structure import (  # noqa: E402
    assign_primary_paper_sections,
    infer_section_role,
)
from review_writer_core.stages.figures.overview_structure import (  # noqa: E402
    derive_overview_structure_contract,
)
from review_writer_core.stages.sections.rule_packs import (  # noqa: E402
    RulePackConfigurationError,
    resolve_rule_pack,
)
from review_writer_core.stages.sections.blueprint_builder import (  # noqa: E402
    build_section,
    parse_outline_sections,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""










def load_matrix(path: Path) -> tuple[str, list[dict[str, Any]], list[str]]:
    data = read_json(path)
    if isinstance(data, dict):
        topic = str(data.get("review_topic") or data.get("topic") or "")
        papers = data.get("papers") if isinstance(data.get("papers"), list) else data.get("rows")
        if not isinstance(papers, list):
            papers = []
        axes = data.get("comparison_axes") if isinstance(data.get("comparison_axes"), list) else []
        return topic, [p for p in papers if isinstance(p, dict)], [str(a) for a in axes]
    if isinstance(data, list):
        return "", [p for p in data if isinstance(p, dict)], []
    return "", [], []


def load_notes(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = read_json(path)
    rows = data if isinstance(data, list) else data.get("papers") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return {}
    return {str(row.get("paper_id")): row for row in rows if isinstance(row, dict) and row.get("paper_id")}




























def write_plan(path: Path, blueprint: dict[str, Any]) -> None:
    lines = [
        "# Section Writing Plan",
        "",
        f"- Project ID: `{blueprint['project_id']}`",
        f"- Review topic: {blueprint.get('review_topic') or ''}",
        f"- Rule pack: `{blueprint.get('rule_pack')}` ({blueprint.get('rule_pack_path')})",
        f"- Created at: {blueprint.get('created_at')}",
        "",
    ]
    for section in blueprint["sections"]:
        lines.extend(
            [
                f"## {section['section_id']}. {section['title']}",
                "",
                f"Thesis: {section['section_thesis']}",
                "",
                f"Major papers: {', '.join(section['major_papers']) or 'TBD'}",
                "",
                "Claims:",
            ]
        )
        for claim in section["review_claims"]:
            papers = ", ".join(p["paper_id"] for p in claim["supporting_papers"])
            lines.append(f"- `{claim['claim_id']}` {claim['claim']} Papers: {papers or 'TBD'}")
        lines.extend(["", f"Figure/table need: {section['figure_or_table_needs'][0]['type']} - {section['figure_or_table_needs'][0]['purpose']}", ""])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    project_dir = review_root / "review-projects" / args.project_id
    stage_dir = project_dir / "01_matrix_outline"
    selected_outline = stage_dir / "selected_outline.md"
    matrix_path = stage_dir / "literature_matrix.json"
    notes_path = stage_dir / "paper_reading_notes.json"
    if not selected_outline.exists():
        raise SystemExit(f"selected_outline.md not found: {selected_outline}")
    if not matrix_path.exists():
        raise SystemExit(f"literature_matrix.json not found: {matrix_path}")

    outline_text = read_text(selected_outline)
    sections = parse_outline_sections(outline_text)
    if not sections:
        raise SystemExit("No level-2 or numbered outline sections found in selected_outline.md")

    topic, papers, axes = load_matrix(matrix_path)
    project_config = read_json(project_dir / "project_config.json") if (project_dir / "project_config.json").is_file() else {}
    configured_topic = str(project_config.get("topic") or "").strip() if isinstance(project_config, dict) else ""
    effective_topic = configured_topic or topic
    query_plan_path = project_dir / "00_discovery" / "query_plan.draft.json"
    query_plan = read_json(query_plan_path) if query_plan_path.is_file() else {}
    try:
        rule_pack_contract = resolve_rule_pack(
            review_root, topic=effective_topic or outline_text
        )
    except RulePackConfigurationError as exc:
        raise SystemExit(str(exc)) from exc
    rule_pack = str(rule_pack_contract["name"])
    notes = load_notes(notes_path)
    assignment_input = [
        {
            **section,
            "paper_ids": section.get("assigned_papers") or [],
            "section_role": infer_section_role(
                section.get("title"), section.get("section_role")
            ),
        }
        for section in sections
        if infer_section_role(section.get("title"), section.get("section_role"))
        != "references"
    ]
    matrix_order = [
        str(paper.get("paper_id"))
        for paper in papers
        if str(paper.get("paper_id") or "").strip()
    ]
    normalized_sections, primary_owner = assign_primary_paper_sections(
        assignment_input, matrix_order
    )
    blueprint_sections = []
    for idx, section in enumerate(normalized_sections):
        prev_title = normalized_sections[idx - 1]["title"] if idx > 0 else ""
        next_title = normalized_sections[idx + 1]["title"] if idx + 1 < len(normalized_sections) else ""
        blueprint_sections.append(build_section(section, papers, axes, notes, prev_title, next_title))

    blueprint = {
        "project_id": args.project_id,
        "review_topic": effective_topic,
        "outline_source": str(selected_outline),
        "matrix_source": str(matrix_path),
        "rule_pack": rule_pack,
        "rule_pack_path": rule_pack_contract["path"],
        "rule_pack_files": rule_pack_contract["files"],
        "rule_pack_sha256": rule_pack_contract["sha256"],
        "overview_structure_contract": derive_overview_structure_contract(
            effective_topic,
            query_plan=query_plan,
            matrix={"review_topic": effective_topic, "papers": papers},
            sections=blueprint_sections,
            taxonomy_profile=(
                project_config.get("taxonomy_profile")
                if isinstance(project_config, dict)
                else ""
            ),
        ),
        "created_at": utc_now(),
        "status": "draft_initialization_needs_semantic_review",
        "paper_assignment_policy": {
            "mode": "single_primary_section_with_supporting_cross_references",
            "primary_section_by_paper": primary_owner,
            "introduction_and_conclusion_are_synthesis_only": True,
        },
        "sections": blueprint_sections,
    }
    out_json = stage_dir / "section_blueprint.json"
    out_md = stage_dir / "section_writing_plan.md"
    write_json(out_json, blueprint)
    write_plan(out_md, blueprint)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize section_blueprint.json from selected outline and literature matrix.")
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
