"""H1 -- the BOM-aware read lives in the shared run_tool harness, so *every*
wrapper benefits, not just one. Proven here through bat_strip_comments.
"""
import unittest

from tests._harness import run_cli, TOOL_DIR  # noqa: F401

SRC = 'rem this whole line is a junk banner\r\nset "X=1"\r\necho %X%\r\n'


class TestHarnessDecodesUtf16(unittest.TestCase):
    def test_utf16_le_bom_input_strips_comment_line(self):
        raw = b'\xff\xfe' + SRC.encode('utf-16-le')
        out, stats = run_cli('bat_strip_comments.py', raw)
        self.assertGreaterEqual(stats.get('rem_lines_removed', stats.get('changed', 0)), 1)
        self.assertNotIn('junk banner', out)
        self.assertIn('set "X=1"', out)
        self.assertNotIn('\x00', out)

    def test_utf16_matches_utf8(self):
        u8_out, _ = run_cli('bat_strip_comments.py', SRC.encode('utf-8'))
        u16_out, _ = run_cli('bat_strip_comments.py', b'\xff\xfe' + SRC.encode('utf-16-le'))
        self.assertEqual(u8_out, u16_out)


if __name__ == '__main__':
    unittest.main()
