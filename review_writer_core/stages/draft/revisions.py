"""Draft-local argument revisions. No database, model calls, or upstream writes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def claim_index(plan):
    return {str(c['claim_id']): c for s in plan.get('sections') or []
            for c in s.get('claims') or [] if c.get('claim_id')}


def effective_writing_plan(plan, overlays):
    """Apply accepted scoped deltas without inheriting old scientific verification.

    The baseline fingerprint detects manual/upstream edits even when IDs survive.
    A stale revision is a conflict, never silently ignored or applied elsewhere.
    """
    result = deepcopy(plan)
    claims = claim_index(result)
    conflicts = []
    for cid, revision in (overlays.get('argument_revisions') or {}).items():
        claim = claims.get(cid)
        if (not isinstance(revision, dict) or not claim
                or fingerprint(claim) != revision.get('base_claim_sha256')
                or not isinstance(revision.get('proposition'), str) or not revision['proposition'].strip()
                or not isinstance(revision.get('reason'), str) or not revision['reason'].strip()):
            conflicts.append(cid)
            continue
        text = revision['proposition']
        claim.update(proposition=text, claim=text, allowed_assertion=text,
                     draft_revision=deepcopy(revision))
        # Do not copy an old verification signature onto a different assertion.
        basis = deepcopy(claim.get('argument_basis') or {})
        basis.pop('verification', None)
        basis['draft_revision_reason'] = revision['reason']
        claim['argument_basis'] = basis
    if conflicts:
        raise ValueError('Draft argument revision conflicts with current source plan: ' + ', '.join(conflicts))
    return result


def revision_groups(issues, plan, paragraphs):
    """Conservative section-local dependency groups, joined by shared Claims."""
    available = {p['paragraph_id'] for p in paragraphs}
    claims = claim_index(plan)
    paragraph_claims, sections = {}, {}
    for section in plan.get('sections') or []:
        sid = str(section.get('section_id') or '')
        for paragraph in section.get('paragraphs') or []:
            pid = str(paragraph.get('paragraph_id') or '')
            sections[pid] = sid
            paragraph_claims[pid] = set(paragraph.get('claim_ids') or [])
        for claim in section.get('claims') or []:
            pid = str(claim.get('paragraph_id') or '')
            if pid:
                sections[pid] = sid
                paragraph_claims.setdefault(pid, set()).add(claim['claim_id'])
    groups = []
    for issue in issues:
        joint = issue.get('repair_class') in {'planning_adjustment', 'draft_revision'}
        if not joint and not issue.get('rewrite_eligible'):
            continue
        pid = str(issue.get('paragraph_id') or '')
        sid = str(issue.get('section_id') or sections.get(pid) or '')
        target_claims = (set(issue.get('claim_ids') or issue.get('missing_core_claim_ids') or paragraph_claims.get(pid) or []) & claims.keys()) if joint else set()
        targets = ({p for p, section in sections.items() if sid and section == sid} if joint else set()) | ({pid} if pid else set())
        targets |= {p for p, ids in paragraph_claims.items() if ids & target_claims}
        targets &= available
        if not targets:
            continue
        group = {'paragraph_ids': targets, 'claim_ids': target_claims, 'issues': [deepcopy(issue)]}
        overlapping = [g for g in groups if g['paragraph_ids'] & targets or g['claim_ids'] & target_claims]
        for other in overlapping:
            group['paragraph_ids'] |= other['paragraph_ids']
            group['claim_ids'] |= other['claim_ids']
            group['issues'].extend(other['issues'])
            groups.remove(other)
        groups.append(group)
    for group in groups:
        group['paragraph_ids'] = sorted(group['paragraph_ids'])
        group['section_ids'] = sorted({sections[p] for p in group['paragraph_ids'] if sections.get(p)})
        # All Claims of dependent paragraphs are visible, but only selected
        # problem Claims may be changed by the model.
        group['claim_ids'] = sorted(group['claim_ids'])
        group['group_id'] = 'draft-' + fingerprint(group['paragraph_ids'])[:16]
    return groups


def prepare_argument_revisions(raw, *, group, baseline, effective, source_overlays, evidence_refs):
    """Whitelist mutable authoring text; never accept model-supplied roles or facts."""
    originals, current = claim_index(baseline), claim_index(effective)
    revisions = {}
    for row in raw or []:
        cid = str(row.get('claim_id') or '')
        proposition, reason = str(row.get('proposition') or '').strip(), str(row.get('reason') or '').strip()
        refs = sorted(set(str(r) for r in row.get('source_evidence_refs') or []))
        if (cid not in group['claim_ids'] or cid not in originals or cid in revisions
                or not proposition or not reason or not refs or not set(refs) <= set(evidence_refs)):
            raise ValueError('Argument revision has an unknown target or lacks registered source support.')
        owners = set(originals[cid].get('citation_group') or originals[cid].get('paper_ids') or [])
        if isinstance(evidence_refs, dict) and owners and any(evidence_refs[r] not in owners for r in refs):
            raise ValueError('Argument support belongs to a different paper; paper identities cannot be reassigned.')
        if proposition == str(current[cid].get('proposition') or current[cid].get('claim') or ''):
            continue
        previous = (source_overlays.get('argument_revisions') or {}).get(cid)
        revisions[cid] = {
            'claim_id': cid, 'group_id': group['group_id'],
            'paragraph_ids': group['paragraph_ids'],
            'base_claim_sha256': fingerprint(originals[cid]),
            'original_proposition': str(current[cid].get('proposition') or current[cid].get('claim') or ''),
            'proposition': proposition, 'reason': reason, 'source_evidence_refs': refs,
            'supersedes': fingerprint(previous) if previous else '',
        }
    return revisions


def accept_argument_revisions(source_overlays, candidate_overlays, selected_ids, changes):
    """Accept complete dependency groups; preserve unrelated/legacy overlay fields."""
    result = deepcopy(source_overlays)
    selected = set(selected_ids)
    changed = {c['paragraph_id'] for c in changes}
    groups = {}
    for change in changes:
        if change.get('group_id'):
            groups.setdefault(change['group_id'], set()).add(change['paragraph_id'])
    if any(ids & selected and not ids <= selected for ids in groups.values()):
        raise ValueError('Accept or discard the complete argument revision group.')
    revisions = dict(result.get('argument_revisions') or {})
    for cid, revision in (candidate_overlays.get('argument_revisions') or {}).items():
        if revision == revisions.get(cid):
            continue
        targets = set(revision.get('paragraph_ids') or []) & changed
        if targets & selected and not targets <= selected:
            raise ValueError('Accept or discard the complete argument revision group.')
        if targets and targets <= selected:
            revisions[cid] = deepcopy(revision)
    result['argument_revisions'] = revisions
    return result
