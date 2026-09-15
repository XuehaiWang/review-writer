"""Read-only source locations for characters rejected by PDF compilers."""
import re


INVALID_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def pdf_character_diagnostics(markdown: str, limit: int = 100) -> dict:
    # Mask comments without shifting source offsets or exposing internal metadata.
    visible = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m[0]), markdown, flags=re.S)
    issues = []
    total = 0
    section, reference, paragraph, figure = "", "", 0, ""
    references = False
    in_paragraph = False
    for line_number, line in enumerate(visible.split("\n"), 1):
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            section = heading[1]
            references = bool(re.fullmatch(r"(?:\d+[.\s]*)?(?:references|bibliography|参考文献)", section, re.I))
            reference, figure, paragraph, in_paragraph = "", "", 0, False
        ref = re.match(r"^\s*\[(\d+)\]\s+", line) if references else None
        if ref:
            reference = ref[1]
        caption = re.match(r"^\s*\*{0,2}(?:Figure|Fig\.?|图)\s*(\d+)", line, re.I)
        if caption:
            figure = caption[1]
        elif line.strip() and not line.lstrip().startswith("!["):
            figure = ""
        if not line.strip():
            in_paragraph = False
        elif not heading and not in_paragraph:
            paragraph += 1
            in_paragraph = True
        for match in INVALID_CONTROL.finditer(line):
            total += 1
            if len(issues) >= limit:
                continue
            kind = "reference" if references and reference else "figure" if caption or line.lstrip().startswith("![") else "heading" if heading else "paragraph"
            code = f"U+{ord(match[0]):04X}"
            excerpt = line[max(0, match.start()-60):min(len(line), match.end()+60)]
            excerpt = INVALID_CONTROL.sub(lambda m: f"⟦U+{ord(m[0]):04X}⟧", excerpt)
            issues.append({"kind": kind, "section": INVALID_CONTROL.sub("⟦?⟧", section),
                "reference_number": reference if references else "", "figure_number": figure,
                "paragraph_number": paragraph, "line": line_number, "column": match.start()+1,
                "codepoint": code, "excerpt": excerpt})
    return {"schema_version": 1, "total": total, "issues": issues,
            "truncated": total > len(issues), "source_modified": False}
