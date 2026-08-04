from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from review_writer_core import taxonomy  # noqa: E402


PREPARE = load_module(
    "taxonomy_profile_prepare",
    ROOT / "skills" / "review-metadata-prep" / "scripts" / "prepare_metadata.py",
)
VALIDATE = load_module(
    "taxonomy_profile_validate",
    ROOT / "skills" / "review-metadata-prep" / "scripts" / "validate_metadata.py",
)
DISCOVER = load_module(
    "taxonomy_profile_discover",
    ROOT / "skills" / "review-topic-paper-discovery" / "scripts" / "discover.py",
)


class TaxonomyProfileChecks(unittest.TestCase):
    def test_default_profile_is_shared_and_versioned(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"REVIEW_TAXONOMY_PROFILE": "allene", "REVIEW_CLASSIFICATION_RULES": ""},
            clear=False,
        ):
            path = taxonomy.resolve_taxonomy_path(ROOT)
            identity = taxonomy.taxonomy_identity(ROOT)
            rules = taxonomy.load_taxonomy_rules(ROOT)
        self.assertEqual(path, ROOT / "review_writer_core" / "taxonomies" / "allene.py")
        self.assertGreater(len(rules), 20)
        self.assertEqual(identity["profile"], "allene")
        self.assertEqual(len(identity["sha256"]), 64)

    def test_custom_rules_reach_metadata_validation_and_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            custom = root / "custom_taxonomy.py"
            custom.write_text(
                "rules = [(\"custom product\", \"product\", [\"custom alias\"])]\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"REVIEW_CLASSIFICATION_RULES": str(custom), "REVIEW_TAXONOMY_PROFILE": "allene"},
                clear=False,
            ):
                resolved = taxonomy.resolve_taxonomy_path(root)
                metadata_labels = PREPARE.load_classification_rules(resolved)
                validation_labels = VALIDATE.load_allowed_labels(root)
                discovery_aliases = DISCOVER.load_classification_rules(root)
                identity = taxonomy.taxonomy_identity(root)
            self.assertIn("custom product", metadata_labels["product"])
            self.assertIn("custom product", validation_labels["product"])
            self.assertEqual(discovery_aliases["product"]["custom product"], ["custom alias"])
            self.assertEqual(identity["profile"], "custom")
            self.assertEqual(identity["rules_path"], "custom_taxonomy.py")


if __name__ == "__main__":
    unittest.main()
