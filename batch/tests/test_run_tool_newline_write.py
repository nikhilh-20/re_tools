"""H2 -- run_tool must not translate newlines on write.

`Path(args.output).write_text(new_text, encoding='utf-8')` (no newline='')
runs Python's text-mode newline translation, turning every '\n' into
os.linesep. On Windows a source already carrying '\r\n' therefore gains one
'\r' per pass -- '\r\n' -> '\r\r\n' -> '\r\r\r\n' ... an unbounded corruption
across a multi-pass chain, independent of which tool ran (every wrapper
funnels through run_tool). Fix: newline=''.
"""
import sys
import unittest
from unittest import mock

from tests._harness import run_cli_bytes, TOOL_DIR  # noqa: F401
from batdeoblib import io as io_mod


class TestNewlineWritePreservation(unittest.TestCase):
    # bat_strip_carets on a caret-free source is a guaranteed no-op transform,
    # so any newline change in the output is the write path, not the pass.
    SCRIPT = 'bat_strip_carets.py'

    def test_crlf_input_stays_crlf(self):
        out, r = run_cli_bytes(self.SCRIPT, b'echo a\r\necho b\r\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b'\r\r\n', out)
        self.assertIn(b'\r\n', out)

    def test_lf_input_stays_lf(self):
        out, r = run_cli_bytes(self.SCRIPT, b'echo a\necho b\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b'\r', out)

    def test_crcrlf_input_does_not_gain_a_cr(self):
        out, r = run_cli_bytes(self.SCRIPT, b'echo a\r\r\necho b\r\r\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(b'\r\r\r\n', out)

    def test_crlf_run_length_stable_across_two_passes(self):
        raw = b'echo a\r\necho b\r\necho c\r\n'
        out1, r1 = run_cli_bytes(self.SCRIPT, raw)
        out2, r2 = run_cli_bytes(self.SCRIPT, out1)
        self.assertEqual((r1.returncode, r2.returncode), (0, 0))
        self.assertEqual(out1.count(b'\r\n'), out2.count(b'\r\n'))
        self.assertNotIn(b'\r\r\n', out2)


class TestNewlineWriteErrorPath(unittest.TestCase):
    def test_error_path_writes_sentinel_without_translation(self):
        import tempfile
        from pathlib import Path

        def _boom(_src, **_kw):
            raise ValueError('boom')

        with tempfile.TemporaryDirectory() as d:
            inp = Path(d) / 'in.cmd'
            out = Path(d) / 'out.cmd'
            inp.write_bytes(b'echo a\r\n')
            argv = ['prog', '--input', str(inp), '--output', str(out)]
            with mock.patch.object(sys, 'argv', argv):
                with self.assertRaises(SystemExit):
                    io_mod.run_tool(_boom, description='test')
            body = out.read_bytes()
            self.assertTrue(body.startswith(b'ERROR: '))
            self.assertIn(b'boom', body)
            self.assertNotIn(b'\r\r\n', body)


if __name__ == '__main__':
    unittest.main()
