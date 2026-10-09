from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from review_writer_core.atomic_io import atomic_write_json


@pytest.mark.parametrize("relative", [
    "review-topic-paper-discovery/scripts/discover.py",
    "review-online-paper-discovery/scripts/discover.py",
    "review-section-blueprint/scripts/init_section_blueprint.py",
    "review-metadata-prep/scripts/prepare_metadata.py",
    "review-first-draft-feedback-loop/scripts/feedback_loop.py",
    "review-figure-style-redraw/scripts/redraw_figures.py",
    "review-conclusion-generator/scripts/generate_conclusion1.py",
])
def test_cli_writers_use_the_same_atomic_implementation(relative):
    path = Path(__file__).resolve().parents[1] / "skills" / relative
    spec = importlib.util.spec_from_file_location("atomic_writer_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.write_json is atomic_write_json


def test_atomic_write_json_creates_parent_and_replaces_existing_file(tmp_path):
    destination = tmp_path / "nested" / "state.json"
    atomic_write_json(destination, {"revision": 1})
    atomic_write_json(destination, {"revision": 2, "label": "综述"})

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "revision": 2,
        "label": "综述",
    }
    assert destination.read_text(encoding="utf-8").endswith("\n")
    assert not destination.with_suffix(".json.tmp").exists()
