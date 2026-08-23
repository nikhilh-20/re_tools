"""Confirms the BOM-aware read fix lives in the shared vbsdeoblib.io.run_tool
harness, not just vbs_remove_deadcode.py — every one of the 19 CLI wrappers
goes through the same --input reader, so a UTF-16-with-BOM file must work
identically for any of them. Exercises a second, unrelated tool
(vbs_strip_comments.py) as a spot check.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
STRIP_COMMENTS_SCRIPT = TOOL_DIR / 'vbs_strip_comments.py'

SRC = (
    "' this is a comment-only line, should be dropped\r\n"
    'x = 1\r\n'
)


class TestStripCommentsUtf16(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_utf16_le_bom_input_strips_comment_line(self):
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(b'\xff\xfe' + SRC.encode('utf-16-le'))

        result = subprocess.run(
            [sys.executable, str(STRIP_COMMENTS_SCRIPT), '--input', str(inp), '--output', str(out)],
            capture_output=True, text=True, check=True,
        )
        stats = json.loads(result.stdout)
        cleaned = out.read_text(encoding='utf-8')

        self.assertEqual(stats['comment_lines_removed'], 1)
        self.assertNotIn('this is a comment', cleaned)
        self.assertIn('x = 1', cleaned)


if __name__ == '__main__':
    unittest.main()
