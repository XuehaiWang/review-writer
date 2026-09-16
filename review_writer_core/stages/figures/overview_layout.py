"""Soft content-capacity hints for reference selection, not output gates."""


def density_metrics(template, features):
    display = features.get('overview_display_contract')
    if not isinstance(display, dict):
        return {'known': False, 'score_delta': 0., 'fill': 1., 'blank': 0.}
    modules = display.get('modules') or []
    facts = [i.get('text', '') for m in modules for i in m.get('items', []) if i.get('text')]
    facts += display.get('unassigned_findings') or []
    caps = template.get('layout_capabilities') or {}
    columns = int(caps.get('nominal_columns', caps.get('module_count_range', [1, 5])[1]))
    per_card = float(caps.get('words_per_card', 24 if caps.get('information_density') == 'high' else 16))
    # Style references may be recomposed. Assess the adapted layout, not the
    # number of fixed boxes in the example picture.
    slots = min(columns, max(1, len(facts)))
    words = sum(len(str(s).split()) for s in facts)
    # Short statements still need a heading and a line box; do not reward filler.
    used = sum(max(8, len(str(s).split())) for s in facts)
    fill = min(1., used / max(1, slots * per_card))
    reaction = (features.get('_chemistry_decision') or {}).get('mode') in {'reaction', 'reaction_generic'}
    hero = float(caps.get('reaction_slot_ratio', 0)) if reaction else 0.
    penalty = 8 * max(0., .60 - fill)
    return {'known': True, 'facts': len(facts), 'words': words, 'columns': columns,
            'fill': round(fill, 3), 'blank': round(1-fill, 3), 'reaction_share': hero,
            'score_delta': round(8*fill + (6*hero if reaction else 0) - penalty, 3)}
