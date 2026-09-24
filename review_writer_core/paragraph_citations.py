"""Conservative citation display grouping; never alters claim/source records."""
import re


def render_paragraph_citations(rows):
    """Rows are (exact text, formatted citation, claim kind).

    A run stays within one paragraph and one kind of assertion. Explicit
    quotations, pre-existing citations and long runs retain local attribution.
    """
    output, pending, previous, words = [], [], None, 0

    def flush():
        if pending:
            output.append(" ".join(pending) + " " + previous[0])
            pending.clear()

    for text, citation, kind in rows:
        if not citation:
            flush()
            output.append(text)
            previous, words = None, 0
            continue
        # Internal labels for direct reports do not change citation scope.
        scope = "direct_report" if kind in {"reported_finding", "reported_data", "direct_source_report"} else kind
        identity = (citation, scope)
        count = len(text.split())
        ambiguous = bool(re.search(r'["“”«»]|\[\d', text)) or not kind or any(
            value in kind for value in ("inference", "interpretation", "comparison", "synthesis"))
        if ambiguous or identity != previous or words + count > 120:
            flush()
            words = 0
        previous = identity
        pending.append(text)
        words += count
        if ambiguous:
            flush()
            words = 0
            previous = None
    flush()
    return " ".join(output)
