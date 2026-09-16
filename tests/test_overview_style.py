from pathlib import Path

import pytest
from PIL import Image
from review_writer_core.stages.figures.overview_style import (
    unique_display,normalize_style_plan,generation_prompt,validate_image_bytes,
)
from review_writer_core.stages.figures.overview_presentation import display_texts_are_near_duplicates
from review_writer_core.stages.figures.overview_layout import density_metrics


def test_dedup_across_all_display_regions_preserves_different_values():
    d={'modules':[{'label':'A','items':[{'text':'This route gives 90% yield.'}]}],
       'unassigned_findings':['This route gives 90% yield.','This route gives 70% yield.'],
       'cross_cutting':[],'take_home':['This route gives 90% yield.']}
    result=unique_display(d)
    assert result['unassigned_findings']==['This route gives 70% yield.']
    assert not result['take_home']
    assert len(result['deduplicated_statements'])==2
    assert len(d['take_home'])==1
    assert not display_texts_are_near_duplicates('The method supports conversion.','The method does not support conversion.')


def test_fixed_five_column_reference_can_be_adapted_without_identity_rules():
    f={'overview_display_contract':{'modules':[{'items':[{'text':'Short verified finding.'}]}]*3}}
    a=density_metrics({'id':1,'layout_capabilities':{'nominal_columns':5}},f)
    b=density_metrics({'id':999,'layout_capabilities':{'nominal_columns':5}},f)
    assert a==b
    assert a['fill']>=.5


def test_prompt_uses_reference_as_style_not_fixed_grid():
    f={'overview_display_contract':{'modules':[]},'review_title':'Title'}
    prompt=generation_prompt({'description':'Radial illustrated branches'},f,normalize_style_plan({},reaction=True))
    assert 'NOT a fixed grid' in prompt
    assert 'exactly ONCE' in prompt
    assert 'Radial illustrated branches' in prompt
    assert 'repeat that reaction' in prompt


def test_plan_ignores_legacy_fixed_region():
    plan = normalize_style_plan({"reaction_box": [.1,.2,.9,.5]}, reaction=True)
    assert "reaction_box" not in plan
    assert plan["chemistry_reference"]


def test_prompt_contains_each_fact_once_and_no_blank_slot():
    text = "Catalysis yields 90% product."
    features = {"overview_display_contract": {"modules": [{"label": "Route", "items": [{"text": text}]}]}}
    prompt = generation_prompt({}, features, normalize_style_plan({}, reaction=True))
    assert prompt.count(text) == 1
    assert "programmatically inserted" not in prompt
    assert "additional image" in prompt


def test_image_validation_is_technical_only():
    import io
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(stream, format="PNG")
    assert validate_image_bytes(stream.getvalue())["width"] == 32
    with pytest.raises(Exception):
        validate_image_bytes(b"not an image")
