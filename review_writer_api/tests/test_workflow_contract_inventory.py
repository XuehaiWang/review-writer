from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PARITY_PATH = ROOT / "docs" / "workflow-feature-parity.md"


def parity_api():
    try:
        from review_writer_api.parity import ParityInventoryError, load_parity_rows
    except ModuleNotFoundError as exc:
        raise AssertionError("The workflow parity loader is missing.") from exc
    return ParityInventoryError, load_parity_rows


class WorkflowContractInventoryTests(unittest.TestCase):
    def test_loader_rejects_a_row_without_a_native_test(self) -> None:
        error_type, load_rows = parity_api()
        with tempfile.TemporaryDirectory() as temp_dir:
            document = Path(temp_dir) / "parity.md"
            document.write_text(
                "| ID | Stage | Current route/action | Inputs | Observable result | "
                "Artifacts/state | Native test | Status |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| LIB-001 | Library | upload | PDF | admitted | pdf | | baseline |\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(error_type, "LIB-001.*Native test"):
                load_rows(document)

    def test_repository_inventory_covers_every_stage_and_is_testable(self) -> None:
        _, load_rows = parity_api()
        rows = load_rows(PARITY_PATH)

        required_prefixes = {"LIB", "DIS", "PLN", "SEC", "FIG", "DRF", "FIN", "ISO"}
        self.assertEqual(required_prefixes, {row.row_id.split("-", 1)[0] for row in rows})
        self.assertTrue(all(row.native_test for row in rows))
        self.assertTrue(all(row.status in {"baseline", "passed"} for row in rows))
        for row in rows:
            expected = (
                "passed"
                if row.row_id.startswith(
                    ("LIB-", "DIS-", "PLN-", "SEC-", "FIG-", "DRF-", "FIN-")
                )
                else "baseline"
            )
            self.assertEqual(expected, row.status, row.row_id)


if __name__ == "__main__":
    unittest.main()
