import io
import json
import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from review_writer_core.pdf_diagnostics import pdf_character_diagnostics
from review_writer_api.job_handlers.final import FinalJobHandlers
from review_writer_api.errors import WorkflowValidationError


class PdfDiagnosticTests(unittest.TestCase):
    def test_precheck_is_persisted_but_does_not_stop_or_modify_render_input(self):
        source = '## References\n[10] Title \x01 text'
        context = Mock(user_id='user', job_id='job')
        handler = FinalJobHandlers()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'REVIEW_WRITER_PDF_RENDERER_URL': 'http://renderer'}):
            handler._staging = Mock(return_value=Path(tmp))
            handler._write_json = Mock()
            handler._render_pdf_remotely = Mock(side_effect=WorkflowValidationError('compiler failed'))
            payload = {'final_markdown': source}
            with self.assertRaises(WorkflowValidationError):
                handler.final_pdf(context, payload)
        handler._render_pdf_remotely.assert_called_once()
        self.assertEqual(source, payload['final_markdown'])
        self.assertEqual(1, context.report_partial_result.call_args.args[0]['pdf_diagnostics']['total'])

    def test_reference_location_and_non_mutation(self):
        source = "## References\n[9] Other.\n[10] SEEBACH. \x01-Peptidic Peptidomimetics.\n"
        report = pdf_character_diagnostics(source)
        issue = report['issues'][0]
        self.assertEqual(('reference', '10', 'U+0001', 3),
                         (issue['kind'], issue['reference_number'], issue['codepoint'], issue['line']))
        self.assertIn('⟦U+0001⟧-Peptidic', issue['excerpt'])
        self.assertIn('\x01', source)
        self.assertFalse(report['source_modified'])

    def test_scientific_symbols_and_whitespace_are_not_flagged(self):
        self.assertEqual(0, pdf_character_diagnostics('α β 中文\tH₂O\r\n$Pd_{2}$ ± − °')['total'])

    def test_heading_paragraph_caption_and_comment_offsets(self):
        source = '<!-- internal \x01 -->\n## Results\nText\x02.\n\n**Figure 3. Bad\x03 caption**\n'
        issues = pdf_character_diagnostics(source)['issues']
        self.assertEqual(2, len(issues))
        self.assertEqual(('paragraph', 'Results', 3), (issues[0]['kind'], issues[0]['section'], issues[0]['line']))
        self.assertEqual(('figure', '3', 5), (issues[1]['kind'], issues[1]['figure_number'], issues[1]['line']))

    def test_bounded_report(self):
        report = pdf_character_diagnostics('x\x01' * 120)
        self.assertEqual(120, report['total'])
        self.assertEqual(100, len(report['issues']))
        self.assertTrue(report['truncated'])

    def test_compiler_error_is_not_misreported_as_publication_gate(self):
        detail = 'compiler logs ' * 500 + 'Text line contains an invalid character.'
        error = HTTPError('http://renderer', 422, '', {}, io.BytesIO(json.dumps({'detail': detail}).encode()))
        with self.assertRaises(WorkflowValidationError) as caught:
            FinalJobHandlers._raise_pdf_renderer_http_error(error)
        self.assertIn('invalid character', str(caught.exception))
        self.assertNotIn('Rebuild', str(caught.exception))

    def test_other_errors_do_not_claim_a_character_cause(self):
        error = HTTPError('http://renderer', 422, '', {}, io.BytesIO(b'{"detail":"Unsupported PDF asset type."}'))
        with self.assertRaises(WorkflowValidationError) as caught:
            FinalJobHandlers._raise_pdf_renderer_http_error(error)
        self.assertNotIn('invalid character', str(caught.exception))
