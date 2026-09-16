import pytest
from review_writer_core.stages.figures.overview_product_contract import confirmed_product_contract, reaction_product_problem


@pytest.mark.parametrize('field', ['target_product', 'product_smiles', 'target_product_smiles'])
def test_confirmed_product_fields(field):
    fact = {'field_id': field, 'value': {'smiles': 'CCO'}, 'status': 'resolved'}
    result = confirmed_product_contract(matrix={'rows': [{'scientific_facts': [fact]}]})
    assert result['smiles'] == 'CCO'
    assert not reaction_product_problem({'product_smiles': 'CCO'}, result)
    assert reaction_product_problem({'product_smiles': 'CC'}, result)


@pytest.mark.parametrize('fact', [
    {'role': 'substrate', 'status': 'resolved', 'smiles': 'CC'},
    {'role': 'substrate', 'status': 'resolved', 'product_smiles': 'CC'},
    {'field_id': 'target_product', 'status': 'pending', 'value': {'smiles': 'CCO'}},
    {'label': 'target product', 'smiles': 'CCO'},
    {'field_id': 'target_product', 'status': 'resolved', 'value': {'smiles': 'invalid!'}},
])
def test_unconfirmed_or_substrate_never_locks_product(fact):
    assert confirmed_product_contract(matrix={'scientific_facts': [fact]})['status'] == 'unresolved'


def test_nested_substrate_does_not_lose_parent_role():
    assert confirmed_product_contract(query_plan={'representative_structure': {
        'role': 'substrate', 'status': 'resolved', 'value': {'product_smiles': 'CCO'}}})['status'] == 'unresolved'


def test_verified_local_fact_contract():
    fact = {'field_id': 'target_product_smiles', 'value': 'CCO', 'support_level': 'direct',
            'verification': {'status': 'supported'}, 'evidence_refs': [{'evidence_key': 'source:1'}]}
    assert confirmed_product_contract(matrix={'scientific_facts': [fact]})['smiles'] == 'CCO'


def test_substrate_field_cannot_hide_product_alias():
    fact = {'field_id': 'substrate', 'status': 'resolved', 'value': {'product_smiles': 'CCO'}}
    assert confirmed_product_contract(matrix={'scientific_facts': [fact]})['status'] == 'unresolved'


def test_container_workflow_status_is_not_product_status():
    matrix = {'status': 'draft', 'scientific_facts': [
        {'field_id': 'target_product', 'role_status': 'confirmed', 'value': 'CCO'}]}
    assert confirmed_product_contract(matrix=matrix)['smiles'] == 'CCO'


def test_explicit_confirmed_target_string():
    assert confirmed_product_contract(blueprint={'target_product': 'CCO', 'status': 'confirmed'})['smiles'] == 'CCO'
