from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from paragraph_manifest_builder import (
    HEADING_RE,
    PARAGRAPH_ID_RE,
    build_manifest,
    split_body_and_references,
)


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class ParagraphEditor:
    def __init__(self, review_root: Path, project_id: str) -> None:
        self.review_root = Path(review_root)
        self.project_id = project_id
        self.stage_dir = self.review_root / "review-projects" / project_id / "04_first_draft"
        self.draft_path = self.stage_dir / "first_draft.md"

    def _paragraphs(self, body: str) -> list[dict[str, Any]]:
        paragraphs: list[dict[str, Any]] = []
        markers = list(PARAGRAPH_ID_RE.finditer(body))
        headings = list(HEADING_RE.finditer(body))
        previous_end = 0
        for marker in markers:
            preceding_headings = [heading for heading in headings if heading.end() <= marker.start()]
            boundary = preceding_headings[-1].end() if preceding_headings else previous_end
            boundary = max(boundary, previous_end)
            segment = body[boundary:marker.start()]
            text_start = boundary + len(segment) - len(segment.lstrip())
            text_end = marker.start() - (len(segment) - len(segment.rstrip()))
            paragraphs.append(
                {
                    "paragraph_id": marker.group(1),
                    "text": body[text_start:text_end],
                    "start": text_start,
                    "end": text_end,
                    "marker_end": marker.end(),
                }
            )
            previous_end = marker.end()
        return paragraphs

    def _load(self) -> tuple[str, str, str]:
        raw = self.draft_path.read_text(encoding="utf-8")
        body, references = split_body_and_references(raw)
        return raw, body, references

    def list_paragraphs(self) -> list[dict[str, Any]]:
        _, body, _ = self._load()
        return [
            {
                "paragraph_id": paragraph["paragraph_id"],
                "text_preview": " ".join(paragraph["text"].split())[:120],
                "citation_count": len(re.findall(r"\[(\d+)\]", paragraph["text"])),
                "char_count": len(paragraph["text"]),
            }
            for paragraph in self._paragraphs(body)
        ]

    def get_paragraph(self, paragraph_id: str) -> dict[str, Any]:
        _, body, _ = self._load()
        paragraph = next(
            (item for item in self._paragraphs(body) if item["paragraph_id"] == paragraph_id),
            None,
        )
        if paragraph is None:
            return {"error": f"Paragraph not found: {paragraph_id}"}
        citations = {
            int(item["callout"]): item
            for item in self._citations()
            if str(item.get("callout", "")).isdigit()
        }
        callouts = sorted({int(value) for value in re.findall(r"\[(\d+)\]", paragraph["text"])})
        return {
            "paragraph_id": paragraph_id,
            "text": paragraph["text"],
            "citation_callouts": callouts,
            "cited_papers": [
                {
                    "callout": callout,
                    "paper_id": citations[callout].get("paper_id", ""),
                    "title": citations[callout].get("title", ""),
                }
                for callout in callouts
                if callout in citations
            ],
            "figures": [],
            "history": self.get_history(paragraph_id),
        }

    def _snapshot(self, paragraph_id: str, old_text: str, operation: str) -> None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        versions = self.stage_dir / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        snapshot = versions / f"first_draft_{timestamp}.md"
        shutil.copy2(self.draft_path, snapshot)
        history_path = self.stage_dir / "paragraph_history.json"
        history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {"entries": []}
        history["entries"].append(
            {
                "timestamp": timestamp,
                "paragraph_id": paragraph_id,
                "operation": operation,
                "old_text": old_text,
                "snapshot_file": snapshot.name,
            }
        )
        _write_json(history_path, history)

    def _citations(self) -> list[dict[str, Any]]:
        path = self.stage_dir / "citations.json"
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        # ``regenerate_first_draft`` writes the versioned canonical envelope,
        # while older projects stored the entries as a bare list.  Accept both
        # on read so opening the paragraph editor cannot silently discard the
        # current citation map.
        entries = value.get("entries") if isinstance(value, dict) else value
        return entries if isinstance(entries, list) else []

    def _normalize_citations(
        self, body: str, references: str
    ) -> tuple[str, str, list[dict[str, Any]]]:
        citations = self._citations()
        by_callout = {
            int(item["callout"]): item
            for item in citations
            if str(item.get("callout", "")).isdigit()
        }
        order: list[int] = []
        for match in re.finditer(r"\[(\d+)\]", body):
            callout = int(match.group(1))
            if callout in by_callout and callout not in order:
                order.append(callout)
        replacement = {old: index + 1 for index, old in enumerate(order)}
        rewritten_body = re.sub(
            r"\[(\d+)\]",
            lambda match: f"[{replacement.get(int(match.group(1)), match.group(1))}]",
            body,
        )
        normalized = []
        for old in order:
            item = dict(by_callout[old])
            item["callout"] = replacement[old]
            normalized.append(item)

        entry_re = re.compile(r"(?m)^\[(\d+)\]\s*(.*?)(?=^\[\d+\]\s*|\Z)", re.DOTALL)
        entries = {int(match.group(1)): match.group(2).strip() for match in entry_re.finditer(references)}
        heading = references[: entry_re.search(references).start()] if entry_re.search(references) else references
        rewritten_references = heading.rstrip() + "\n\n"
        for old in order:
            item = by_callout[old]
            text = entries.get(old) or str(item.get("title") or item.get("paper_id") or "")
            rewritten_references += f"[{replacement[old]}] {text}\n\n"
        return rewritten_body, rewritten_references.rstrip() + "\n", normalized

    def _save(self, body: str, references: str) -> None:
        normalized_body, normalized_references, citations = self._normalize_citations(
            body, references
        )
        temporary = self.draft_path.with_suffix(".md.tmp")
        temporary.write_text(normalized_body + normalized_references, encoding="utf-8")
        temporary.replace(self.draft_path)
        _write_json(
            self.stage_dir / "citations.json",
            {"project_id": self.project_id, "entries": citations},
        )
        build_manifest(self.review_root, self.project_id)

    def update_paragraph(self, paragraph_id: str, text: str, reason: str = "") -> dict[str, Any]:
        _, body, references = self._load()
        paragraph = next(
            (item for item in self._paragraphs(body) if item["paragraph_id"] == paragraph_id),
            None,
        )
        if paragraph is None:
            return {"error": f"Paragraph not found: {paragraph_id}"}
        self._snapshot(paragraph_id, paragraph["text"], f"update: {reason}")
        updated_body = body[: paragraph["start"]] + text + body[paragraph["end"] :]
        self._save(updated_body, references)
        return {"ok": True, "paragraph_id": paragraph_id, "operation": "update"}

    def insert_paragraph(self, after_paragraph_id: str, text: str) -> dict[str, Any]:
        _, body, references = self._load()
        paragraphs = self._paragraphs(body)
        anchor = next(
            (item for item in paragraphs if item["paragraph_id"] == after_paragraph_id),
            None,
        )
        if anchor is None:
            return {"error": f"Paragraph not found: {after_paragraph_id}"}
        section_match = re.fullmatch(r"sec(\d+)-p\d+", after_paragraph_id)
        if not section_match:
            return {"error": f"Unsupported paragraph id: {after_paragraph_id}"}
        section = section_match.group(1)
        numbers = [
            int(match.group(1))
            for item in paragraphs
            if (match := re.fullmatch(rf"sec{section}-p(\d+)", item["paragraph_id"]))
        ]
        paragraph_id = f"sec{section}-p{max(numbers, default=0) + 1}"
        self._snapshot(paragraph_id, "", f"insert after {after_paragraph_id}")
        inserted = f"\n\n{text.strip()}\n\n<!-- paragraph_id: {paragraph_id} -->"
        updated_body = body[: anchor["marker_end"]] + inserted + body[anchor["marker_end"] :]
        self._save(updated_body, references)
        return {"ok": True, "paragraph_id": paragraph_id, "operation": "insert"}

    def delete_paragraph(self, paragraph_id: str, reason: str = "") -> dict[str, Any]:
        _, body, references = self._load()
        paragraph = next(
            (item for item in self._paragraphs(body) if item["paragraph_id"] == paragraph_id),
            None,
        )
        if paragraph is None:
            return {"error": f"Paragraph not found: {paragraph_id}"}
        self._snapshot(paragraph_id, paragraph["text"], f"delete: {reason}")
        updated_body = body[: paragraph["start"]] + body[paragraph["marker_end"] :]
        self._save(updated_body, references)
        return {"ok": True, "paragraph_id": paragraph_id, "operation": "delete"}

    def get_history(self, paragraph_id: str) -> list[dict[str, Any]]:
        path = self.stage_dir / "paragraph_history.json"
        if not path.exists():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        return [
            entry
            for entry in value.get("entries", [])
            if entry.get("paragraph_id") == paragraph_id
        ]

    def rollback(self, paragraph_id: str, version_index: int = -1) -> dict[str, Any]:
        history = self.get_history(paragraph_id)
        try:
            entry = history[version_index]
        except IndexError:
            return {"error": f"Version index {version_index} out of range"}
        if entry["operation"].startswith("insert"):
            return self.delete_paragraph(paragraph_id, f"rollback {version_index}")
        return self.update_paragraph(
            paragraph_id, str(entry.get("old_text", "")), f"rollback {version_index}"
        )
