import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from review_writer_api.workflow_schemas import DraftOverviewGenerateRequest, OverviewStructureReference
from review_writer_core.overview_references import prepare_reference_images


class OverviewReferenceTests(unittest.TestCase):
    def test_optional_and_invalid(self):
        self.assertEqual(DraftOverviewGenerateRequest().structure_references, [])
        for value in ({}, {"smiles": "not-smiles"}, {"kind": "reaction", "smiles": "CCO>>"},
                      {"kind": "reaction", "smiles": "CCO>>bad"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                OverviewStructureReference(**value)

    def test_two_dimensional_molecule_and_reaction(self):
        from PIL import Image
        values = [OverviewStructureReference(smiles="C#Cc1ccccc1", role="substrate").model_dump(),
                  OverviewStructureReference(kind="reaction", smiles="CCO>>CC=O", conditions="Example only").model_dump(),
                  OverviewStructureReference(name="Unconfirmed name").model_dump()]
        with tempfile.TemporaryDirectory() as tmp:
            paths, prompt = prepare_reference_images(values, Path(tmp))
            self.assertEqual(len(paths), 2)
            for path in paths:
                with Image.open(path) as image:
                    self.assertGreater(image.width, 200)
                    self.assertGreater(image.height, 200)
            self.assertIn("NOT verified literature evidence", prompt)
            self.assertIn('"role": "substrate"', prompt)
            self.assertIn("never infer a product", prompt)

    def test_no_references_requires_no_rendering(self):
        self.assertEqual(prepare_reference_images([], Path("unused")), ([], ""))

    def test_limit(self):
        with self.assertRaises(ValidationError):
            DraftOverviewGenerateRequest(structure_references=[{"name": "water"}] * 5)
