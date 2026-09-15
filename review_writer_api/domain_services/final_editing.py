"""Read-only final manuscript history and paragraph projection."""

import hashlib
import re

from review_writer_core.workflow.artifacts import FINAL_MANUSCRIPT, FINAL_DOCX, FINAL_PDF


def final_paragraphs(markdown):
    """Address prose spans without altering headings, assets or bibliography."""
    rows = []
    end = re.search(r"(?im)^#{1,6}\s+(?:references|bibliography|参考文献)\s*$", markdown)
    body = markdown[:end.start()] if end else markdown
    fence = None
    fenced_ranges = []
    for marker in re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})([^\n]*)$", body):
        delimiter, suffix = marker.groups()
        if fence is None:
            fence = (marker.start(), delimiter)
        elif delimiter[0] == fence[1][0] and len(delimiter) >= len(fence[1]) and not suffix.strip():
            fenced_ranges.append((fence[0], marker.end()))
            fence = None
    if fence is not None:
        fenced_ranges.append((fence[0], len(body)))
    for match in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", body):
        value = match.group()
        if any(match.start() < end and match.end() > start for start, end in fenced_ranges):
            continue
        if value.startswith(("#", "!", "<!--", "|", ">", "$$", "\\[", "- ", "* ")):
            continue
        if re.match(r"(?:\d+[.)]\s|(?:Figure|Table|图|表)\s*\d)", value):
            continue
        # Paragraph provenance markers are structural, not editable prose.
        content = value.split("<!--", 1)[0].rstrip()
        if not content:
            continue
        rows.append({"paragraph_id": f"p{len(rows)+1}-{hashlib.sha256(content.encode()).hexdigest()[:10]}",
                     "text": content, "start": match.start(), "end": match.start()+len(content)})
    return rows


class FinalEditingMixin:
    def manuscript_versions(self, principal, project_id, current_id):
        exports = []
        for logical_name, kind in ((FINAL_DOCX, "DOCX"), (FINAL_PDF, "PDF")):
            exports.extend((artifact, kind) for artifact in self.repository.list_artifacts(
                principal.user_id, project_id, logical_name))
        return [{"artifact_id": a.id, "created_at": a.created_at,
                 "operation": a.metadata.get("operation", "final-build"),
                 "current": a.id == current_id,
                 "downloads": [{"artifact_id": output.id, "format": kind} for output, kind in exports
                               if output.metadata.get("source_final_artifact_id") == a.id]}
                for a in self.repository.list_artifacts(principal.user_id, project_id, FINAL_MANUSCRIPT)]
