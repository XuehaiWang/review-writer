"""Conservative citation display grouping; never alters claim/source records."""
import re

# How far a single shared marker may travel from its prose.  Raising this merges
# more consecutive sentences that cite the same source, so the same source is not
# restated every few lines; measured on real drafts it removes ~14% of the
# markers (120 -> 180 words).  Past ~250 words the marker drifts too far from the
# sentence it attributes, so the value stays well below that.
MAX_GROUPED_WORDS = 180


def render_paragraph_citations(rows):
    """Rows are (exact text, formatted citation, claim kind).

    Consecutive sentences that cite the same source share one trailing marker,
    regardless of assertion kind. Explicit quotations and pre-existing numeric
    callouts keep local attribution; long runs re-cite so the marker stays near
    its prose.
    """
    output, pending, previous, words = [], [], None, 0

    def flush():
        if pending:
            output.append(" ".join(pending) + " " + previous)
            pending.clear()

    for text, citation, kind in rows:
        if not citation:
            flush()
            output.append(text)
            previous, words = None, 0
            continue
        count = len(text.split())
        explicit = bool(re.search(r'["“”«»]|\[\d', text))
        if explicit or citation != previous or words + count > MAX_GROUPED_WORDS:
            flush()
            words = 0
        previous = citation
        pending.append(text)
        words += count
        if explicit:
            flush()
            words = 0
            previous = None
    flush()
    return " ".join(output)
