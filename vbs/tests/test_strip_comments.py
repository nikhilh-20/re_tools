"""Unit and regression tests for vbs_strip_comments.py.

Covers every control flow exercised by the pipeline:
  - Comment-only single-quote lines removed
  - REM keyword lines removed
  - Code lines preserved
  - Comment between two code lines (blank line not merged into adjacent code)
  - --include-trailing strips inline end-of-line comments
  - Pipeline stat regression: pass 8, changed == 2, comment_lines_removed == 2
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_strip_comments as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_strip_comments.py'
SAMPLE   = Path(r'C:\Users\Ashura\Desktop\cef108df7267250b66dca8e6ab87a629591b9840f27e3ab1821248ebfe2cdb1f.vbs')


def _run_script(script: Path, inp: Path, out: Path, extra: list | None = None) -> dict:
    cmd = [sys.executable, str(script), '--input', str(inp), '--output', str(out)]
    if extra:
        cmd.extend(extra)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestStripComments(unittest.TestCase):

    def test_single_quote_comment_line_removed(self):
        src = "' this is a comment\nx = 1\n"
        out, stats = tool.run(src)
        self.assertNotIn('this is a comment', out)
        self.assertIn('x = 1', out)
        self.assertEqual(stats['changed'], 1)
        self.assertEqual(stats['comment_lines_removed'], 1)

    def test_two_comment_lines_removed(self):
        # Matches the exact pattern in the VBS sample (two comment lines)
        src = (
            "x = 1\n"
            "' Envia log estagio2\n"
            "y = 2\n"
            "' Ja instalado - notifica servidor\n"
            "z = 3\n"
        )
        out, stats = tool.run(src)
        self.assertNotIn('Envia log', out)
        self.assertNotIn('Ja instalado', out)
        self.assertIn('x = 1', out)
        self.assertIn('y = 2', out)
        self.assertIn('z = 3', out)
        self.assertEqual(stats['changed'], 2)
        self.assertEqual(stats['comment_lines_removed'], 2)

    def test_code_line_preserved(self):
        src = "On Error Resume Next\nx = 1\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_rem_keyword_line_removed(self):
        src = "REM this is a REM comment\nx = 1\n"
        out, stats = tool.run(src)
        self.assertNotIn('REM this', out)
        self.assertIn('x = 1', out)
        self.assertEqual(stats['changed'], 1)
        self.assertEqual(stats['comment_lines_removed'], 1)

    def test_inline_trailing_comment_preserved_by_default(self):
        # By default, trailing comments on code lines are NOT stripped
        src = "x = 1 ' inline comment\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn("' inline comment", out)

    def test_include_trailing_strips_inline_comment(self):
        src = "x = 1 ' inline comment\n"
        out, stats = tool.run(src, include_trailing=True)
        self.assertEqual(stats['changed'], 1)
        self.assertNotIn("' inline comment", out)
        self.assertIn('x = 1', out)

    def test_comment_between_code_lines_leaves_no_merge(self):
        # The two code lines should not be merged; the comment line simply disappears
        src = "a = 1\n' middle comment\nb = 2\n"
        out, stats = tool.run(src)
        self.assertNotIn('middle comment', out)
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(lines, ['a = 1', 'b = 2'])
        self.assertEqual(stats['changed'], 1)

    def test_empty_source_unchanged(self):
        src = ''
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, '')

    def test_all_comment_lines_removed(self):
        src = "' line 1\n' line 2\n' line 3\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 3)
        self.assertEqual(out.strip(), '')

    def test_no_comment_lines_reports_zero(self):
        src = "x = 1\ny = 2\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_include_trailing_false_is_default(self):
        # include_trailing defaults to False — same result as explicit False
        src = "x = 1 ' trailing\n"
        out_default, _ = tool.run(src)
        out_explicit, _ = tool.run(src, include_trailing=False)
        self.assertEqual(out_default, out_explicit)

    def test_only_whitespace_line_not_treated_as_comment(self):
        # A line with only spaces/tabs has no COMMENT token — it is not removed
        src = "x = 1\n   \ny = 2\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_indented_comment_line_removed(self):
        # Leading whitespace before the apostrophe still produces only a
        # COMMENT token for the line (WS is excluded from line_kinds) →
        # line_kinds == {'comment'} → removed.
        src = "   ' indented comment\nx = 1\n"
        out, stats = tool.run(src)
        self.assertNotIn('indented comment', out)
        self.assertIn('x = 1', out)
        self.assertEqual(stats['changed'], 1)
        self.assertEqual(stats['comment_lines_removed'], 1)

    def test_rem_after_code_preserved_by_default(self):
        # REM is only tokenised as a comment when it is the very first
        # non-whitespace token on a logical line.  After code ('x = 1 REM
        # inline') the preceding non-WS token is NUMBER(1), not
        # NEWLINE/COLON, so _check_rem_comment does not fire — REM stays
        # as an IDENT and the line is classified as 'code', not removed.
        src = "x = 1 REM inline\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_lowercase_rem_line_removed(self):
        # _check_rem_comment is case-insensitive: 'rem' / 'Rem' at the
        # start of a logical line both produce a COMMENT token →
        # line_kinds == {'comment'} → removed.
        src = "rem lowercase comment\nRem Mixed Case\nx = 1\n"
        out, stats = tool.run(src)
        self.assertNotIn('lowercase comment', out)
        self.assertNotIn('Mixed Case', out)
        self.assertIn('x = 1', out)
        self.assertEqual(stats['changed'], 2)
        self.assertEqual(stats['comment_lines_removed'], 2)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestStripCommentsCli(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _run_cli(self, src: str, extra: list | None = None) -> tuple[str, dict]:
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(src.encode('utf-8'))
        cmd = [sys.executable, str(SCRIPT), '--input', str(inp), '--output', str(out)]
        if extra:
            cmd.extend(extra)
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.read_text(encoding='utf-8'), json.loads(result.stdout)

    def test_cli_removes_comment_line(self):
        out, stats = self._run_cli("' comment\nx = 1\n")
        self.assertNotIn('comment', out)
        self.assertEqual(stats['comment_lines_removed'], 1)

    def test_cli_include_trailing_flag(self):
        src = "x = 1 ' trailing\n"
        out, stats = self._run_cli(src, extra=['--include-trailing'])
        self.assertNotIn("' trailing", out)
        self.assertIn('x = 1', out)

    def test_cli_json_has_both_keys(self):
        _, stats = self._run_cli("' comment\nx = 1\n")
        self.assertIn('changed', stats)
        self.assertIn('comment_lines_removed', stats)

    @unittest.skipUnless(SAMPLE.exists(), 'VBS sample not found on Desktop')
    def test_pipeline_stat_regression_pass8(self):
        """Pipeline pass 8 is strip_comments on the second fold_concat output.
        Baseline: changed == 2, comment_lines_removed == 2."""
        tmp = self.tmp
        p = [SAMPLE,
             tmp / 'pass1.vbs',
             tmp / 'pass2.vbs',
             tmp / 'pass3.vbs',
             tmp / 'pass4.vbs',
             tmp / 'pass5.vbs',
             tmp / 'pass6.vbs',
             tmp / 'pass7.vbs',
             tmp / 'pass8.vbs']

        scripts = [
            TOOL_DIR / 'vbs_fold_chr_calls.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            TOOL_DIR / 'vbs_propagate_constants.py',
            TOOL_DIR / 'vbs_inline_functions.py',
            TOOL_DIR / 'vbs_fold_builtin_calls.py',
            TOOL_DIR / 'vbs_remove_deadcode.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            SCRIPT,
        ]
        for i, script in enumerate(scripts):
            _run_script(script, p[i], p[i + 1])

        stats = _run_script(SCRIPT, p[7], p[8])
        self.assertEqual(stats['changed'], 2,
                         f'Expected changed == 2, got {stats["changed"]}')
        self.assertEqual(stats['comment_lines_removed'], 2,
                         f'Expected comment_lines_removed == 2, got {stats["comment_lines_removed"]}')

        out_text = p[8].read_text(encoding='utf-8')
        self.assertNotIn('Envia log estagio2', out_text)
        self.assertNotIn('Ja instalado', out_text)
        # Code must survive
        self.assertIn('On Error Resume Next', out_text)
        self.assertIn('WScript.Quit', out_text)


if __name__ == '__main__':
    unittest.main()
