"""Shared Rule Pack selection and immutable identity for Blueprint/Sections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


RULE_PACK_PROMPT_VERSION = "balanced-rule-pack/1"


class RulePackConfigurationError(ValueError):
    """Raised before publication when a selected Rule Pack cannot be trusted."""


def _skill_root(review_root: Path) -> Path:
    return Path(review_root).resolve() / "skills" / "review-section-blueprint"


def resolve_rule_pack(
    review_root: Path,
    *,
    topic: str = "",
    name: str = "",
) -> dict[str, Any]:
    skill_root = _skill_root(review_root)
    manifest_path = skill_root / "references" / "rule_packs.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RulePackConfigurationError(f"Rule Pack catalog is unavailable: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise RulePackConfigurationError("Rule Pack catalog must be a JSON object.")
    packs = manifest.get("rule_packs")
    default = str(manifest.get("default_rule_pack") or "general").strip()
    if not isinstance(packs, dict) or not default:
        raise RulePackConfigurationError("Rule Pack catalog is missing its default or entries.")

    selected = str(name or "").strip()
    if not selected:
        normalized_topic = " ".join(str(topic or "").casefold().split())
        selected = next(
            (
                str(pack_name)
                for pack_name, config in packs.items()
                if pack_name != default
                and isinstance(config, dict)
                and any(
                    str(signal).casefold() in normalized_topic
                    for signal in config.get("topic_signals") or []
                    if str(signal).strip()
                )
            ),
            default,
        )
    config = packs.get(selected)
    if not isinstance(config, dict):
        raise RulePackConfigurationError(f"Unknown Rule Pack: {selected}")

    relative_path = str(config.get("path") or f"references/rule_packs/{selected}").strip()
    pack_root = (skill_root / relative_path).resolve()
    try:
        pack_root.relative_to(skill_root.resolve())
    except ValueError as exc:
        raise RulePackConfigurationError(f"Rule Pack path escapes its skill root: {relative_path}") from exc
    files = [str(item).strip() for item in config.get("files") or [] if str(item).strip()]
    if not files:
        raise RulePackConfigurationError(f"Rule Pack has no declared files: {selected}")

    digest = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    resolved_files: list[str] = []
    for file_name in files:
        path = (pack_root / file_name).resolve()
        try:
            path.relative_to(pack_root)
        except ValueError as exc:
            raise RulePackConfigurationError(f"Rule Pack file escapes its directory: {file_name}") from exc
        if not path.is_file():
            raise RulePackConfigurationError(f"Rule Pack file is missing: {path}")
        digest.update(file_name.encode("utf-8"))
        digest.update(path.read_bytes())
        resolved_files.append(file_name)
    return {
        "name": selected,
        "path": relative_path.replace("\\", "/"),
        "files": resolved_files,
        "sha256": digest.hexdigest(),
    }


def validate_blueprint_rule_pack(review_root: Path, blueprint: dict[str, Any]) -> dict[str, Any]:
    selected = resolve_rule_pack(review_root, name=str(blueprint.get("rule_pack") or ""))
    recorded = str(blueprint.get("rule_pack_sha256") or "").strip()
    if not recorded:
        raise RulePackConfigurationError("Blueprint does not record a Rule Pack version.")
    if recorded != selected["sha256"]:
        raise RulePackConfigurationError(
            "Blueprint Rule Pack version differs from the installed files; regenerate and confirm Blueprint first."
        )
    return selected


def load_rule_pack_text(
    review_root: Path, blueprint: dict[str, Any], *, char_budget: int = 14_000,
) -> str:
    """Give every declared policy a share of the existing prompt budget.

    Short policies are kept whole and unused shares flow to longer policies.
    Truncated excerpts are marked; later files must never silently disappear.
    """
    selected = validate_blueprint_rule_pack(review_root, blueprint)
    root = _skill_root(review_root) / selected["path"]
    entries = []
    for name in selected["files"]:
        text = (root / name).read_text(encoding="utf-8").strip()
        if text:
            entries.append((f"<!-- rule source: {Path(name).name} -->\n", text))
    if not entries:
        raise RulePackConfigurationError("Rule Pack contains no nonempty rules.")
    remaining = char_budget - sum(len(header) for header, _ in entries) - 2 * (len(entries) - 1)
    truncation = "\n[Excerpt truncated.]"
    minimum = sum(min(len(text), len(truncation) + 1) for _, text in entries)
    if remaining < minimum:
        raise RulePackConfigurationError("Rule Pack prompt budget is too small for all declared files.")
    allowances = [0] * len(entries)
    for position, index in enumerate(sorted(range(len(entries)), key=lambda i: len(entries[i][1]))):
        allowances[index] = min(len(entries[index][1]), remaining // (len(entries) - position))
        remaining -= allowances[index]
    chunks = []
    for (header, text), allowance in zip(entries, allowances):
        if len(text) > allowance:
            text = text[:allowance - len(truncation)].rstrip() + truncation
        chunks.append(header + text)
    return "\n\n".join(chunks)
