from __future__ import annotations

import unittest

from review_writer_core.publication_caption import normalize_publication_caption


class PublicationCaptionTests(unittest.TestCase):
    def test_chemistry_caption_becomes_readable_without_losing_raw_evidence(self) -> None:
        raw = (
            r"$Pd_{2}(dba)_{3}\cdot CHCl_{3}$ , $(S)-(-)$ -MeO-MOP, "
            r"$CHCl_{3}$ ; ii, PhCHO, $-78^{\circ}C$"
        )

        result = normalize_publication_caption(raw)

        self.assertEqual(result.source_text, raw)
        self.assertEqual(
            result.publication_text,
            "Pd₂(dba)₃·CHCl₃, (S)-(−)-MeO-MOP, CHCl₃; ii, PhCHO, −78 °C",
        )
        self.assertEqual(result.status, "cleaned")

    def test_source_number_is_removed_from_publication_caption_only(self) -> None:
        result = normalize_publication_caption("Scheme 6. Catalytic asymmetric hydroboration.")

        self.assertEqual(result.source_text, "Scheme 6. Catalytic asymmetric hydroboration.")
        self.assertEqual(result.publication_text, "Catalytic asymmetric hydroboration")

    def test_unsupported_tex_is_non_blocking_and_reported(self) -> None:
        result = normalize_publication_caption(r"Scheme 2. malformed \unknown{CH}_{3}$")

        self.assertEqual(result.status, "partial")
        self.assertIn("unknownCH₃", result.publication_text)
        self.assertTrue(result.warnings)

    def test_xml_control_characters_do_not_escape_the_normalizer(self) -> None:
        result = normalize_publication_caption("Scheme 1. Pd\x00 catalyst")

        self.assertEqual(result.status, "partial")
        self.assertNotIn("\x00", result.publication_text)

    def test_unambiguous_chemistry_ocr_word_split_is_repaired(self) -> None:
        result = normalize_publication_caption(
            "Figure 1. Mechanism for the phosphine-catalyzed [3+2] cycloaddi tion."
        )

        self.assertEqual(
            result.publication_text,
            "Mechanism for the phosphine-catalyzed [3+2] cycloaddition",
        )
        self.assertEqual(result.version, "publication-caption/2")

        capitalized = normalize_publication_caption(
            "Figure 2. Cycloaddi tions enabled by phosphines."
        )
        self.assertEqual(
            capitalized.publication_text,
            "Cycloadditions enabled by phosphines",
        )


if __name__ == "__main__":
    unittest.main()
