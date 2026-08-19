from __future__ import annotations

import unittest

from review_writer_core.retrieval import build_document_chunks


class RetrievalChunkerTests(unittest.TestCase):
    def test_preserves_layout_lineage_assets_references_and_neighbors(self) -> None:
        content_list = [
            [
                {
                    "type": "title",
                    "content": {
                        "title_content": [{"type": "text", "content": "Results"}],
                        "level": 1,
                    },
                },
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "Catalyst A gave 91% yield."}
                        ]
                    },
                },
                {
                    "type": "image",
                    "img_path": "images/scheme-1.png",
                    "image_caption": ["Scheme 1. Catalytic cycle"],
                },
            ],
            [
                {
                    "type": "title",
                    "content": {
                        "title_content": [{"type": "text", "content": "References"}],
                        "level": 1,
                    },
                },
                {
                    "type": "paragraph",
                    "content": {
                        "paragraph_content": [
                            {"type": "text", "content": "1. Example citation"}
                        ]
                    },
                },
            ],
        ]

        first = build_document_chunks("P001", "version-a", content_list, min_tokens=1)
        second = build_document_chunks("P001", "version-a", content_list, min_tokens=1)

        self.assertEqual([item.chunk_id for item in first], [item.chunk_id for item in second])
        self.assertTrue(any("images/scheme-1.png" in item.asset_refs for item in first))
        self.assertTrue(any(item.is_reference and "Example citation" in item.content for item in first))
        self.assertTrue(any("Results" in item.section_path for item in first))
        self.assertEqual("", first[0].previous_chunk_id)
        self.assertEqual(first[1].chunk_id, first[0].next_chunk_id)
        self.assertEqual(first[-2].chunk_id, first[-1].previous_chunk_id)
        self.assertEqual("", first[-1].next_chunk_id)

    def test_oversized_blocks_are_split_but_short_blocks_merge_without_overlap(self) -> None:
        long_text = " ".join(f"token{index}" for index in range(90))
        content_list = [
            {"type": "text", "text": "short one", "page_idx": 0},
            {"type": "text", "text": "short two", "page_idx": 0},
            {"type": "text", "text": long_text, "page_idx": 1},
        ]

        chunks = build_document_chunks(
            "P002",
            "version-b",
            content_list,
            min_tokens=10,
            max_tokens=20,
            overlap_tokens=5,
        )

        self.assertIn("short one\n\nshort two", chunks[0].content)
        long_chunks = [item for item in chunks if "token" in item.content]
        self.assertGreaterEqual(len(long_chunks), 3)
        self.assertIn("token35", long_chunks[0].content)
        self.assertIn("token35", long_chunks[1].content)


if __name__ == "__main__":
    unittest.main()
