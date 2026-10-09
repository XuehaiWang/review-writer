"""End-to-end: the switch selects the condensed reader table during assembly."""

import unittest

from review_writer_api.domain_services.drafts import DraftsService


def _section_index():
    context = [
        {"evidence_key": "E1", "object": "propargylic alcohols + aldehydes",
         "catalyst": "CuBr, pyrrolidine", "conditions": "", "environment": "DCM, rt",
         "result": "92% yield", "units": ""},
        {"evidence_key": "E2", "object": "terminal alkynes + ketones",
         "catalyst": "chiral phosphoric acid", "conditions": "",
         "environment": "toluene, 60 C", "result": "80% yield", "units": ""},
    ]
    refs = [
        {"evidence_key": "E1", "paper_id": "P1", "quote": "P1 passage"},
        {"evidence_key": "E2", "paper_id": "P2", "quote": "P2 passage"},
    ]
    section = {
        "section_id": "S01",
        "heading": "Aldehydes",
        "heading_level": 2,
        "section_role": "body",
        "primary_papers": ["P1", "P2"],
        "paragraphs": [{
            "paragraph_id": "S01-p1",
            "text": "The two systems differ in catalyst and environment.",
            "cited_paper_ids": ["P1", "P2"],
            "claim_realizations": [{
                "claim_id": "c1",
                "citation_group": ["P1", "P2"],
                "evidence_refs": refs,
                "result_context": context,
            }],
        }],
    }
    return {"sections": [section]}


class ReactionQuickrefIntegrationTests(unittest.TestCase):
    def _assemble(self, *, enabled):
        return DraftsService._assemble_markdown(
            "Review", _section_index(), {}, {"rows": []}, None,
            reaction_quickref_tables=enabled,
        )

    def test_switch_on_emits_the_condensed_reader_table(self):
        markdown = self._assemble(enabled=True)
        self.assertIn("<!-- reaction_table:", markdown)
        self.assertIn("Reaction system / transformation", markdown)
        self.assertIn("Conditions / environment", markdown)

    def test_switch_off_keeps_the_legacy_renderer(self):
        markdown = self._assemble(enabled=False)
        self.assertNotIn("<!-- reaction_table:", markdown)


if __name__ == "__main__":
    unittest.main()
