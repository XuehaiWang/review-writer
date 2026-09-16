"""Overview-only adapter: confirmed product structures, never substrate inference."""
from collections.abc import Mapping

PRODUCT_FIELDS = frozenset({'target_product', 'product', 'product_smiles', 'target_product_smiles', 'product_structure'})
CONFIRMED = frozenset({'resolved', 'confirmed', 'approved', 'accepted'})


def confirmed_product_contract(*, blueprint=None, query_plan=None, matrix=None):
    from rdkit import Chem
    def walk(value, role='', confirmed=False):
        if isinstance(value, list):
            for item in value:
                yield from walk(item, role, confirmed)
        if not isinstance(value, Mapping):
            return
        explicit_role = str(value.get('role') or '').strip().casefold()
        if explicit_role and explicit_role not in PRODUCT_FIELDS:
            return  # Preserve parent-role exclusion through nested value/structure.
        role = explicit_role or str(value.get('field_id') or value.get('field') or value.get('category') or role).casefold()
        if role and role not in PRODUCT_FIELDS:
            return
        status = str(value.get('status') or value.get('role_status') or '').casefold()
        structural = bool(role) or any(key in value for key in ('smiles', 'product_smiles', 'target_product_smiles'))
        if structural and status and status not in CONFIRMED:
            return
        if value.get('superseded_by_fact_id'):
            return
        verdict = value.get('verification') or {}
        if isinstance(verdict, Mapping) and verdict:
            if verdict.get('status') != 'supported' or verdict.get('source_damage'):
                return
            if value.get('validation_contract'):
                from review_writer_core.scientific_facts import fact_needs_verification
                if fact_needs_verification(value):
                    return
        verified_fact = (isinstance(verdict, Mapping) and verdict.get('status') == 'supported'
                         and value.get('support_level') == 'direct' and bool(value.get('evidence_refs')))
        confirmed = status in CONFIRMED or verified_fact or confirmed
        if confirmed:
            for key in ('target_product', 'product_smiles', 'target_product_smiles', 'smiles', 'canonical_smiles', 'isomeric_smiles', 'value'):
                raw = value.get(key)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                if key not in {'target_product', 'product_smiles', 'target_product_smiles'} and role not in PRODUCT_FIELDS:
                    continue
                mol = Chem.MolFromSmiles(raw)
                if mol is not None:
                    yield {'status': 'resolved', 'role': 'target_product', 'smiles': Chem.MolToSmiles(mol),
                           'required_smarts': str(value.get('required_smarts') or ''),
                           'fact_id': value.get('fact_id', ''), 'evidence_refs': value.get('evidence_refs', [])}
        for key, nested in value.items():
            if isinstance(nested, (Mapping, list)):
                scoped = key in PRODUCT_FIELDS or key in {'value', 'structure', 'representative_structure'}
                yield from walk(nested, key if key in PRODUCT_FIELDS else role if scoped else '', confirmed if scoped else False)
    for source, data in [('blueprint', blueprint), ('query_plan', query_plan), ('fact_matrix', matrix)]:
        for candidate in walk(data):
            return {'schema_version': 2, **candidate, 'evidence_sources': [source]}
    return {'schema_version': 2, 'status': 'unresolved', 'role': 'target_product', 'smiles': '',
            'reason': 'No confirmed product-role structure; substrate and untyped SMILES are not eligible.'}


def reaction_product_problem(scheme, contract):
    """Validate product-family and stereochemical constraints with RDKit."""
    if not isinstance(contract, Mapping) or contract.get('status') != 'resolved':
        return ''
    if contract.get('role') != 'target_product':
        return 'Structure contract is not a target product.'
    from rdkit import Chem
    product = Chem.MolFromSmiles(str(scheme.get('product_smiles') or ''))
    required = contract.get('required_smarts') or contract.get('smiles') or ''
    pattern = Chem.MolFromSmarts(required)
    if product is None or pattern is None or not product.HasSubstructMatch(pattern, useChirality=True):
        return 'Candidate product conflicts with the confirmed upstream target-product structure.'
    return ''
