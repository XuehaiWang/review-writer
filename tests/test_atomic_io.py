from __future__ import annotations

import json

from review_writer_core.atomic_io import atomic_write_json


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
