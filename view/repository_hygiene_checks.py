from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneChecks(unittest.TestCase):
    def test_skill_docs_do_not_contain_machine_or_retired_paths(self) -> None:
        forbidden = re.compile(
            r"(?:[A-Za-z]:\\|/home/|/Users/|/mnt/|review-writer-main|"
            r"source-paper/Progargylic|review-library/(?:paper_pdf|mineru-outputs)|"
            r"allene_classification_rules|<review-root>/template|references/templates)"
        )
        matches: list[str] = []
        for path in (ROOT / "skills").rglob("SKILL.md"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if forbidden.search(line):
                    matches.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(matches, [])

    def test_legacy_library_dashboard_is_absent(self) -> None:
        self.assertFalse((ROOT / "review-library" / "dashboard").exists())
        self.assertTrue((ROOT / "view" / "assets" / "dashboard" / "library.html").is_file())

    def test_online_registration_uses_the_canonical_mineru_output(self) -> None:
        script = (
            ROOT / "skills" / "review-online-paper-discovery" / "scripts" / "discover.py"
        ).read_text(encoding="utf-8")
        self.assertIn('else review_root / "mineru-outputs"', script)
        self.assertNotIn('review_root / "review-library" / "mineru-outputs"', script)


if __name__ == "__main__":
    unittest.main()
