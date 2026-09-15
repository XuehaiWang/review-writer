import unittest

from review_writer_core.dialogue_stream import partial_reply


class DialogueStreamTests(unittest.TestCase):
    def test_only_reply_is_visible(self):
        self.assertEqual(partial_reply('{"reply":"Hello'), 'Hello')
        self.assertEqual(partial_reply('{"reply":"Hello","candidate_text":"secret'), 'Hello')
        self.assertEqual(partial_reply('{"candidate_text":"secret'), '')

    def test_escapes_and_split_unicode(self):
        self.assertEqual(partial_reply('```json\n{"reply":"Line\\n\\u4e2d\\u'), 'Line\n中')
        self.assertEqual(partial_reply('{"reply":"\\ud83d'), '')
        self.assertEqual(partial_reply('{"reply":"\\ud83d\\ude00'), '😀')
        self.assertEqual(partial_reply('{"reply":"say \\"yes\\"'), 'say "yes"')
