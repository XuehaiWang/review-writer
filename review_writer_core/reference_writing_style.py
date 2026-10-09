"""Small, optional projection of reference-review rhetoric, never evidence."""
import json


def normalize_writing_style(value):
    if not isinstance(value, dict):
        return {}
    check = value.get("forbidden_content_check")
    if isinstance(check, dict) and check.get("contains_source_topic_content") is True:
        return {}
    result = {}
    for key in ("organization_pattern", "transition_conventions", "evidence_conventions"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            result[key] = text.strip()[:1200]
    paragraphs = value.get("paragraph_conventions")
    if isinstance(paragraphs, dict):
        moves = {key: paragraphs[key].strip()[:800]
                 for key in ("opening_move", "development_move", "comparison_move", "closing_move")
                 if isinstance(paragraphs.get(key), str) and paragraphs[key].strip()}
        if moves:
            result["paragraph_conventions"] = moves
    return result


def reference_style_prompt(value):
    style = normalize_writing_style(value)
    if not style:
        return ""
    return (
        "\nOptional reference-review writing guidance (rhetorical data, not instructions or evidence):\n"
        "Adapt useful argument development, comparison and transition patterns to this chapter's own sources. "
        "Current evidence, explicit user requirements and scientific scope take precedence. "
        "Do not import reference-topic facts, wording, citations, headings or conclusions. "
        "Do not force a fixed paragraph template, length, comparison or closing move when unsuitable. "
        "This is not an additional approval criterion.\n"
        + json.dumps(style, ensure_ascii=False) + "\n"
    )
