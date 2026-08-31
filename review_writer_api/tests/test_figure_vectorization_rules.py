from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from review_writer_api.figure_rules import build_full_vector_svg


class FigureVectorizationRuleTests(unittest.TestCase):
    def test_neutral_light_halos_are_not_traced_or_darkened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "halo.png"
            image = Image.new("RGB", (5, 1), "white")
            image.putdata(
                [
                    (255, 255, 255),
                    (247, 247, 247),
                    (210, 206, 208),
                    (128, 128, 128),
                    (0, 0, 0),
                ]
            )
            image.save(path)

            svg = build_full_vector_svg(path)

        self.assertNotIn('fill="#e0e0e0"', svg)
        self.assertNotIn('fill="#c0c0c0"', svg)
        self.assertIn('fill="#808080"', svg)
        self.assertIn('fill="#000000"', svg)

    def test_saturated_scientific_colours_remain_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "colours.png"
            image = Image.new("RGB", (3, 1), "white")
            image.putdata([(250, 8, 8), (8, 8, 250), (8, 180, 8)])
            image.save(path)

            svg = build_full_vector_svg(path)

        self.assertIn('fill="#ff0000"', svg)
        self.assertIn('fill="#0000ff"', svg)
        self.assertIn('fill="#00c000"', svg)


if __name__ == "__main__":
    unittest.main()
