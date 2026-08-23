"""Unit and end-to-end tests for vbs_collapse_blanklines.py.

Regression coverage for a real-sample bug: `pass5.vbs` mixes line-ending
styles (real `\\r\\n` pairs plus bare `\\r` characters used as blank-line
separators, e.g. `Sub f(je,tv)\\r\\r\\r\\r\\r\\nDim r,...`), inherited verbatim
from the original obfuscated source through every earlier tokenizer-based
pass (which already treats `\\r`, `\\n`, and `\\r\\n` as equally valid line
terminators — see tests/test_tokenizer_newlines.py). The old implementation
used plain `\\n`-anchored regexes (`re.MULTILINE` for `^`/`$`, and `\\n{3,}`
for the squeeze pass). Python's `re.MULTILINE` never treats a bare `\\r` as
a line boundary, so a run like `\\r\\r\\r\\r\\r\\n` was seen as one single
"line" ending at the final `\\n` — invisible to both passes. Running the old
tool against the real file was a byte-identical no-op: `{"changed":0,...}`.
The fix generalizes both passes to treat `\\r\\n`, `\\r`, and `\\n` as
interchangeable newline tokens, matching the tokenizer's own definition.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_collapse_blanklines as M

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = TOOL_DIR / 'vbs_collapse_blanklines.py'


def _run(src: str) -> tuple[str, dict]:
    return M.run(src)


class TestPlainLfUnaffected(unittest.TestCase):
    """Regression guard: behavior for ordinary \\n-only files must not
    change from the original implementation."""

    def test_blank_run_squeezes_to_one_blank_line(self):
        out, stats = _run('a\n\n\nb\n')
        self.assertEqual(out, 'a\n\nb\n')
        self.assertEqual(stats['changed'], 1)

    def test_single_blank_line_is_a_noop(self):
        out, stats = _run('a\n\nb\n')
        self.assertEqual(out, 'a\n\nb\n')
        self.assertEqual(stats['changed'], 0)

    def test_no_blank_lines_is_a_noop(self):
        out, stats = _run('a\nb\n')
        self.assertEqual(out, 'a\nb\n')
        self.assertEqual(stats['changed'], 0)


class TestWhitespaceOnlyLineStripping(unittest.TestCase):
    def test_lf_whitespace_only_line_stripped(self):
        out, stats = _run('a\n   \nb\n')
        self.assertEqual(out, 'a\n\nb\n')
        self.assertEqual(stats['changed'], 1)

    def test_crlf_whitespace_only_line_stripped(self):
        out, stats = _run('a\r\n   \r\nb\r\n')
        self.assertEqual(out, 'a\r\n\r\nb\r\n')
        self.assertEqual(stats['changed'], 1)

    def test_bare_cr_whitespace_only_line_stripped(self):
        out, stats = _run('a\r   \rb\r')
        self.assertEqual(out, 'a\r\rb\r')
        self.assertEqual(stats['changed'], 1)

    def test_tabs_and_spaces_mixed_stripped(self):
        out, stats = _run('a\n \t \t\nb\n')
        self.assertEqual(out, 'a\n\nb\n')
        self.assertEqual(stats['changed'], 1)

    def test_leading_whitespace_only_line_stripped(self):
        out, stats = _run('   \na\n')
        self.assertEqual(out, '\na\n')
        self.assertEqual(stats['changed'], 1)

    def test_trailing_whitespace_only_line_without_newline_stripped(self):
        out, stats = _run('a\n   ')
        self.assertEqual(out, 'a\n')
        self.assertEqual(stats['changed'], 1)


class TestBareCrBlankRunRegression(unittest.TestCase):
    """The exact defect reported against the real pass5.vbs sample: bare
    \\r characters used as blank-line separators must still be recognized
    and squeezed, not silently left untouched."""

    def test_bare_cr_run_is_squeezed(self):
        src = 'Sub f(je,tv)' + '\r' * 4 + '\n' + 'Dim r\r\n'
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 1)
        # Squeezed to exactly one blank line's worth of newline tokens
        # (first two tokens of the run: \r, \r) between the statements.
        self.assertEqual(out, 'Sub f(je,tv)\r\rDim r\r\n')

    def test_bare_cr_run_was_a_noop_under_old_semantics(self):
        """Direct proof of the reported bug shape: the pre-fix regexes
        (\\n-anchored MULTILINE ^/$ and \\n{3,}) treat this input as a
        single line with no 3+ run of literal \\n, so old-style matching
        finds nothing to change. The new implementation must not."""
        import re
        src = 'Sub f(je,tv)' + '\r' * 4 + '\n' + 'Dim r\r\n'
        old_step1 = re.sub(r'^[ \t]+$', '', src, flags=re.MULTILINE)
        old_step2 = re.sub(r'\n{3,}', '\n\n', old_step1)
        self.assertEqual(old_step2, src, 'sanity check: old regex is a no-op on this input')

        _, stats = _run(src)
        self.assertEqual(stats['changed'], 1)


class TestMixedStyleRun(unittest.TestCase):
    def test_mixed_tokens_squeeze_to_first_two(self):
        # \n then \r\n then \r: ordered so no two adjacent tokens combine
        # into a different token when concatenated (\r immediately
        # followed by \n would greedily re-tokenize as \r\n).
        src = 'a' + '\n' + '\r\n' + '\r' + 'b'
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertEqual(out, 'a\n\r\nb')


class TestCrlfOnlySqueezeParity(unittest.TestCase):
    def test_crlf_run_squeezes_like_lf(self):
        out, stats = _run('a\r\n\r\n\r\nb\r\n')
        self.assertEqual(out, 'a\r\n\r\nb\r\n')
        self.assertEqual(stats['changed'], 1)


class TestIdempotency(unittest.TestCase):
    def test_second_pass_is_a_noop(self):
        src = 'Sub f(je,tv)' + '\r' * 4 + '\n' + 'Dim r\r\n' + '   \r\n' + 'End Sub\r\n'
        out1, stats1 = _run(src)
        out2, stats2 = _run(out1)
        self.assertEqual(stats1['changed'], 1)
        self.assertEqual(out1, out2)
        self.assertEqual(stats2['changed'], 0)


class TestEndToEndCli(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _run_cli(self, src: str) -> tuple[str, dict]:
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(src.encode('utf-8'))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--input', str(inp), '--output', str(out)],
            capture_output=True, text=True, check=True)
        stats = json.loads(result.stdout)
        return out.read_bytes().decode('utf-8'), stats

    def test_bare_cr_blank_run_squeezed_via_cli(self):
        src = 'Sub f(je,tv)' + '\r' * 4 + '\n' + 'Dim r\r\n' + 'End Sub\r\n'
        out, stats = self._run_cli(src)
        self.assertEqual(stats['changed'], 1)
        self.assertEqual(out, 'Sub f(je,tv)\r\rDim r\r\nEnd Sub\r\n')


if __name__ == '__main__':
    unittest.main()
