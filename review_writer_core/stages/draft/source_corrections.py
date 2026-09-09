"""Exact, source-addressable corrections; never a blanket protection bypass."""
import re


def verified_corrections(text, proposals, evidence):
    passages = {p.get('ref'): p for paper in evidence.get('evidence') or []
                for p in paper.get('original_passages') or []}
    result = []
    for row in proposals or []:
        if not isinstance(row, dict):
            continue
        before, after = str(row.get('before') or ''), str(row.get('after') or '')
        quote = str(row.get('source_quote') or '')
        source = str((passages.get(row.get('source_ref')) or {}).get('text') or '')
        if (row.get('unambiguous') is not True or not before or not after or before == after
                or len(before) > 120 or len(after) > 120 or text.count(before) != 1
                or not quote or quote not in source or after not in quote
                or re.search(r'[\[\]<>\n]', before + after)):
            continue
        if any(before in c['before'] or c['before'] in before for c in result):
            continue
        result.append({k: row[k] for k in ('before', 'after', 'source_quote', 'source_ref', 'unambiguous')})
    return result


def corrected_baseline(text, corrections):
    for row in corrections or []:
        before, after = str(row.get('before') or ''), str(row.get('after') or '')
        if before and text.count(before) == 1 and after:
            text = text.replace(before, after, 1)
    return text
