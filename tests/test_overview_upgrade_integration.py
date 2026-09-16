"""Overview acceptance with mocked providers and real extraction/RDKit/PNG/report IO."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "overview_upgrade_integration",
    ROOT / "skills/review-figure-style-redraw/scripts/generate_overview_figure.py",
)
overview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overview)

SCHEME = {
    "substrate_smiles": "*C#C", "substrate_name": "terminal alkyne",
    "product_smiles": "*C=C=C*", "product_name": "allene",
    "catalyst_label": "CuI, base", "reaction_name": "allenation",
}


def write_project(root, chemical=True, modules=2):
    project = root / "review-projects/acceptance"
    topic = "Allene synthesis" if chemical else "Digital learning methods"
    files = {
        # Regression: real chemistry projects can retain the generic profile.
        "project_config.json": {"taxonomy_profile": "general_academic"},
        "00_discovery/query_plan.draft.json": {"topic": topic, "group_by": ["catalyst_or_method"]},
        "00_discovery/selected_discovery_results.json": {"group_by": ["catalyst_or_method"]},
        "01_matrix_outline/section_blueprint.json": {
            "review_topic": topic,
            "classification_basis": {"primary_axis": "substrate" if chemical else "method"},
            "sections": [
                {"section_id": f"S{i+1:02}", "title": f"Route {i+1}", "section_role": "body"}
                for i in range(modules)
            ],
        },
    }
    for name, data in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    path = project / "04_first_draft/first_draft.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"# {topic}\n\n## Introduction\nReview scope.\n\n## Route 1\n"
        + ("Terminal alkynes yield allenes using CuI and base."
           if chemical else "Digital learning supports flexible access. Evidence varies across methods."),
        encoding="utf-8",
    )
    return project


def provider_png(blank_slot):
    fig = Image.new("RGB", (1000, 800), (214, 222, 232))
    draw = ImageDraw.Draw(fig)
    draw.rectangle((0, 0, 999, 70), fill=(9, 39, 82))
    for x in range(0, 1000, 28):
        draw.rectangle((x, 90, x + 7, 799), fill=(181, 93, 81))
    if blank_slot:
        draw.rectangle((30, 130, 969, 510), fill="white", outline=(80, 100, 120), width=3)
    output = io.BytesIO()
    fig.save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize("chemical,confidence,modules,blank_slot,expected_mode,template_id", [
    (True, 95, 2, True, "reaction", 1),
    (True, 15, 2, True, "concept", 1),
    (True, -1, 2, True, "concept", 1),
    (False, 0, 3, True, "concept", 4),
    (False, 0, 7, True, "concept", 10),
    (True, 95, 4, False, "reaction", 5),
])
def test_generation_acceptance(tmp_path, chemical, confidence, modules, blank_slot, expected_mode, template_id):
    project = write_project(tmp_path, chemical, modules)
    calls = []

    def text_model(prompt, *, label, **kwargs):
        calls.append(label)
        if label == "overview-template-selection":
            return {"template_id": template_id, "reason": "Fits module count and declared slot."}
        if label == "overview-chemistry-review":
            return {"supported": confidence >= 70, "confidence": confidence, "reason": "Fixture evidence review."}
        if label == "overview-display-audit":
            return {"passed": True, "uncertain": False, "issues": []}
        assert "Authoritative Overview contract" in prompt
        return {
            **(SCHEME if chemical and confidence >= 0 else {}),
            "chemistry_applicable": chemical,
            "key_findings": ["CuI and base yield allenes" if chemical else "Flexible access"],
            "cross_cutting": ["Evidence boundaries"], "take_home": ["Evidence varies across methods"],
        }

    argv = [str(SPEC.origin), "--review-root", str(tmp_path), "--project-id", "acceptance", "--skeleton-style", "flat"]
    with patch.object(overview.sys, "argv", argv), patch.object(overview, "load_dotenv"), \
         patch.object(overview, "resolve_api_key", return_value="fixture"), \
         patch.object(overview, "_text_gateway_configured", return_value=True), \
         patch.object(overview, "call_gateway_json", side_effect=text_model), \
         patch.object(overview, "call_image_edit_api", side_effect=[provider_png(blank_slot),provider_png(True),provider_png(True)]) as image_call:
        overview.main()
    report = json.loads((project / "03_figure_redraw/overview_template_match.json").read_text(encoding="utf-8"))
    assert report["status"] == "success"
    assert report["chemistry_generation"]["mode"] == expected_mode
    if expected_mode == "reaction":
        assert report["template_selection"]["mode"] == "ai"
        assert report["style_generation"]["mode"] == "single-pass"
    else:
        assert report["template_selection"]["mode"] == "ai"
        assert report["selected_template_id"] == template_id
    assert len(report["overview_content_contract"]["modules"]) == modules
    assert len(report["overview_content_contract_sha256"]) == 64
    assert report["overview_content_contract"]["primary_axis"] == ("substrate" if chemical else "method")
    assert report["features"]["group_by"] == (["substrate"] if chemical else ["method"])
    if expected_mode == "reaction":
        assert report["skeleton"]["chemical_identity"]
        assert report["composite"]["status"] == "not_applicable"
        assert image_call.call_args.kwargs["extra_images"]
        assert image_call.call_count == 1
        assert image_call.call_args.args[2].name != 'template_skeleton.png'
    else:
        assert report["skeleton"]["smiles"] == ""
        assert "Draw a SINGLE 3D ball-and-stick" not in report["adapted_prompt"]
        assert "AUTHORITATIVE DISPLAY CONTENT" in report["adapted_prompt"]
        assert "STYLE REFERENCE" in report["adapted_prompt"]
        assert report["composite"]["status"] == "not_applicable"
        if chemical:
            assert "do not invent molecular structures" in report["adapted_prompt"]
    with Image.open(project / "03_figure_redraw/overview_figure.png") as image:
        assert image.size == (1000, 800)


def test_reviewed_skeleton_reuses_product_without_keyword_substitution():
    features = {"taxonomy_profile": "allene", "review_title": "Allene synthesis"}
    with patch.object(overview, "_llm_content_pack", return_value={"reaction": SCHEME}), \
         patch.object(overview, "_automatic_chemistry_decision", return_value={"mode": "skeleton", "confidence": 50, "reason": "Conservative motif"}):
        assert overview.resolve_reaction_scheme(features) is None
    assert overview.resolve_skeleton_smiles(features) == SCHEME["product_smiles"]


def test_candidate_cannot_override_blueprint_product_lock():
    features = {"overview_structure_contract": {
        "status": "resolved", "role": "target_product", "smiles": "*C=C=C*", "required_smarts": "C=C=C",
    }}
    with patch.object(overview, "call_gateway_json") as model:
        decision = overview._automatic_chemistry_decision(features, {**SCHEME, "product_smiles": "*C#C"})
    assert decision["mode"] == "concept"
    model.assert_not_called()


def test_string_false_cannot_promote_a_reaction():
    with patch.object(overview, "_text_gateway_configured", return_value=True), \
         patch.object(overview, "call_gateway_json", return_value={"supported": "false", "confidence": 95}):
        assert overview._automatic_chemistry_decision({}, SCHEME)["mode"] != "reaction"


def test_reference_without_original_slot_can_be_adapted_to_reaction():
    templates = [
        {"id": 7, "name": "slotless", "layout_type": "matrix", "layout_capabilities": {"reaction_slot": "none"}},
        {"id": 22, "name": "reaction", "layout_type": "hero", "layout_capabilities": {"reaction_slot": "hero-horizontal"}},
    ]
    with patch.object(overview, "score_template", side_effect=[100, 1]), \
         patch.object(overview, "_text_gateway_configured", return_value=False):
        chosen = overview.select_best_template(templates, {"_chemistry_decision": {"mode": "reaction"}})
        assert chosen["id"] == 7
        assert "reaction_box" not in overview.normalize_style_plan({}, reaction=True)


def test_dry_run_never_calls_model_gateway(tmp_path):
    write_project(tmp_path)
    argv = [str(SPEC.origin), "--review-root", str(tmp_path), "--project-id", "acceptance", "--dry-run"]
    with patch.object(overview.sys, "argv", argv), patch.object(overview, "load_dotenv"), \
         patch.object(overview, "_text_gateway_configured", return_value=True), \
         patch.object(overview, "call_gateway_json") as text, \
         patch.object(overview, "call_image_edit_api") as image:
        overview.main()
    text.assert_not_called()
    image.assert_not_called()


@pytest.mark.parametrize("valid", [True, False])
def test_single_generation_no_output_audit_or_retry(tmp_path, valid):
    project = write_project(tmp_path, chemical=False)
    labels = []
    def model(prompt, *, label, **kwargs):
        labels.append(label)
        if label == "overview-template-selection":
            return {"template_id": 1, "reason": "Reference style"}
        return {"key_findings": ["Flexible access"], "cross_cutting": [], "take_home": []}
    argv = [str(SPEC.origin), "--review-root", str(tmp_path), "--project-id", "acceptance"]
    png = provider_png(False) if valid else b"invalid image"
    with patch.object(overview.sys, "argv", argv), patch.object(overview, "load_dotenv"), \
         patch.object(overview, "resolve_api_key", return_value="fixture"), \
         patch.object(overview, "_text_gateway_configured", return_value=True), \
         patch.object(overview, "call_gateway_json", side_effect=model), \
         patch.object(overview, "call_image_edit_api", return_value=png) as image:
        if valid:
            overview.main()
        else:
            with pytest.raises(Exception, match="cannot identify image"):
                overview.main()
    assert image.call_count == 1
    assert "overview-display-audit" not in labels
    output = project / "03_figure_redraw/overview_figure.png"
    assert output.exists() == valid
    if valid:
        assert output.read_bytes() == png  # No post-generation image modification.
