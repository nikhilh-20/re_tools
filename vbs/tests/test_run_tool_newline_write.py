"""Regression tests for run_tool's output-write newline handling.

Bug found while chasing why a real sample's v7773 self-append chain stayed
unpropagated across several pipeline passes: vbsdeoblib/io.py's run_tool
read input correctly (read_source_text manually decodes raw bytes, no
translation) but wrote output with `Path(...).write_text(new_text,
encoding='utf-8')` — no `newline=''`. Python's text-mode write, with
`newline` left at its default None, translates every '\\n' character to
os.linesep ('\\r\\n' on Windows) regardless of what already precedes it.

For a source already carrying stray CRs before its line endings (as this
whole pipeline's real sample does — traced back to an earlier tool's own
output), that silently added one extra '\\r' per line on *every* tool
invocation: pass1.vbs had 2 CRs before each '\\n', pass2.vbs had 3, pass3.vbs
had 4, pass4.vbs had 5, pass5.vbs had 6 — a strictly growing, unbounded
corruption across the pipeline, independent of which specific tool ran at
each step (every tool funnels through this same run_tool).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vbsdeoblib.io import run_tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = TOOL_DIR / 'vbs_propagate_constants.py'


def _run_cli(inp: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), '--input', str(inp), '--output', str(out)],
        capture_output=True, text=True, check=True)


class TestNewlineWritePreservation(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_crcrlf_input_round_trips_without_gaining_a_cr(self):
        src = 'x = 1\r\r\ny = 2\r\r\n'
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(src.encode('utf-8'))

        _run_cli(inp, out)
        written = out.read_bytes()

        self.assertNotIn(b'\r\r\r\n', written)
        self.assertIn(b'\r\r\n', written)

    def test_crcrlf_run_length_does_not_grow_across_two_passes(self):
        """Direct regression test for the 2->3->4->5->6 pattern found in the
        real pipeline: chaining the tool's own output back in as input must
        not add another stray \\r each time."""
        src = 'x = 1\r\r\ny = 2\r\r\n'
        p1 = self.tmp / 'p1.vbs'
        p2 = self.tmp / 'p2.vbs'
        p3 = self.tmp / 'p3.vbs'
        p1.write_bytes(src.encode('utf-8'))

        _run_cli(p1, p2)
        _run_cli(p2, p3)

        b2 = p2.read_bytes()
        b3 = p3.read_bytes()
        cr_run_2 = b2.count(b'\r\r\n')
        cr_run_3 = b3.count(b'\r\r\n')
        self.assertGreater(cr_run_2, 0)
        self.assertEqual(cr_run_2, cr_run_3)
        self.assertNotIn(b'\r\r\r\n', b3)

    def test_plain_crlf_input_unaffected(self):
        src = 'x = 1\r\ny = 2\r\n'
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(src.encode('utf-8'))

        _run_cli(inp, out)
        written = out.read_bytes()

        self.assertNotIn(b'\r\r\n', written)
        self.assertIn(b'\r\n', written)

    def test_plain_lf_input_unaffected(self):
        src = 'x = 1\ny = 2\n'
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(src.encode('utf-8'))

        _run_cli(inp, out)
        written = out.read_bytes()

        self.assertNotIn(b'\r', written)
        self.assertIn(b'\n', written)


class TestNewlineWriteErrorPath(unittest.TestCase):
    """The error branch (fn(src, **kwargs) raises) uses the same
    write_text(..., newline='') call — cover it directly rather than via a
    real tool, since none of the real tools raise on well-formed input."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_error_message_written_with_no_newline_translation(self):
        def _boom(src, **kwargs):
            raise ValueError('boom')

        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(b'x = 1\r\r\n')

        argv = ['prog', '--input', str(inp), '--output', str(out)]
        with mock.patch.object(sys, 'argv', argv):
            with self.assertRaises(SystemExit):
                run_tool(_boom, description='test')

        written = out.read_bytes()
        self.assertTrue(written.startswith(b'ERROR: '))
        self.assertIn(b'boom', written)


if __name__ == '__main__':
    unittest.main()
