from review_writer_api.domain_services.final_editing import final_paragraphs


def test_editable_spans_preserve_publication_structure():
    markdown = """# Title

## Introduction

First paragraph. [1]
<!-- paragraph_id: original -->

![Figure](artifact://figure)

Figure 1. Caption.

| Column | Value |
| --- | --- |
| A | B |

```text
Not prose.

Still code.
```

Last paragraph.

## References

[1] Bibliography entry.
"""
    rows = final_paragraphs(markdown)
    assert [row["text"] for row in rows] == ["First paragraph. [1]", "Last paragraph."]
    for row in rows:
        assert markdown[row["start"]:row["end"]] == row["text"]
    first = rows[0]
    updated = markdown[:first["start"]] + "Revised. [1]" + markdown[first["end"]:]
    assert "<!-- paragraph_id: original -->" in updated
    assert updated[updated.index("![Figure]"):] == markdown[markdown.index("![Figure]"):]
