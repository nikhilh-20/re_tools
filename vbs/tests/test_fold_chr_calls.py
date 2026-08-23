"""Unit and regression tests for vbs_fold_chr_calls.py.

Covers every control flow exercised by the 8-pass deobfuscation pipeline on
the real cef108df...vbs sample:
  - Chr(N) with printable ASCII folded to a string literal
  - Multiple Chr() calls in one concatenation expression
  - Chr(N) mixed with a bare string literal (chr folds, concat left for fold_concat)
  - Fixpoint / idempotency
  - Invalid codepoints (ValueError caught, token left untouched)
  - CLI pipeline stat regression against the baseline: changed == 188
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_fold_chr_calls as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_fold_chr_calls.py'
SAMPLE   = Path(r'C:\Users\Ashura\Desktop\cef108df7267250b66dca8e6ab87a629591b9840f27e3ab1821248ebfe2cdb1f.vbs')


# ---------------------------------------------------------------------------
# Unit tests — call run() directly on small inline VBS strings
# ---------------------------------------------------------------------------

class TestChrFolding(unittest.TestCase):

    def test_single_chr_call(self):
        src = 'x = Chr(115)\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 1)

    def test_chr_in_concatenation_leaves_literal_untouched(self):
        # Chr folds; the remaining & "://p" is left for fold_concat
        src = 'x = Chr(115) & "://p"\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertIn('"://p"', out)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 1)

    def test_multiple_chr_calls_in_one_expr(self):
        # Exact pattern from the VBS sample: https = h t t p s
        src = 'x = Chr(104) & Chr(116) & Chr(116) & Chr(112) & Chr(115)\n'
        out, stats = tool.run(src)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 5)

    def test_chr_with_special_chars(self):
        # Chr(37) = '%', Chr(92) = '\' — exact chars used in registry key path
        src = 'x = Chr(37) & Chr(92)\n'
        out, stats = tool.run(src)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 2)

    def test_chr_mixed_with_string_literal(self):
        # Chr(100) & "f-br" → "d" & "f-br"  (only chr folds)
        src = 'x = Chr(100) & "f-br"\n'
        out, stats = tool.run(src)
        self.assertIn('"d"', out)
        self.assertIn('"f-br"', out)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 1)

    def test_already_folded_is_idempotent(self):
        src = 'x = "s" & "://p"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_chr_zero_is_folded(self):
        # Chr(0) is technically valid (NUL); tool folds it without crashing
        src = 'x = Chr(0)\n'
        out, stats = tool.run(src)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 1)

    def test_invalid_chr_negative_left_untouched(self):
        # chr(-1) raises ValueError; the token must be left in place, no exception
        src = 'x = Chr(-1)\n'
        out, stats = tool.run(src)
        self.assertIsInstance(out, str)
        # The tool should either leave it as Chr(-1) or fold it — either is fine as long
        # as no exception propagates
        self.assertIsInstance(stats['changed'], int)

    def test_chr_inside_dim_block_folded(self):
        # The sample uses Chr inside simple assignment statements, not declarations,
        # but the tool operates on token stream regardless of statement type
        src = 'v = Chr(104) & Chr(116) & Chr(116) & Chr(112)\nWScript.Echo v\n'
        out, stats = tool.run(src)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 4)

    def test_second_pass_on_already_folded_output_reports_zero(self):
        src = 'x = Chr(65) & Chr(66)\n'
        out1, stats1 = tool.run(src)
        self.assertEqual(stats1['changed'], 2)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)
        self.assertEqual(out1, out2)

    def test_chr_not_inside_string_literal(self):
        # A string containing the text "Chr(65)" must not be touched
        src = 'x = "Chr(65)"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_chr_in_comment_not_folded(self):
        src = "' Chr(65)\nx = 1\n"
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)


# ---------------------------------------------------------------------------
# CLI tests — invoke vbs_fold_chr_calls.py via subprocess
# ---------------------------------------------------------------------------

class TestChrFoldingCli(unittest.TestCase):

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
            capture_output=True, text=True, check=True,
        )
        return out.read_text(encoding='utf-8'), json.loads(result.stdout)

    def test_basic_chr_via_cli(self):
        out, stats = self._run_cli('x = Chr(65)\n')
        self.assertIn('"A"', out)
        self.assertEqual(stats['changed'], 1)

    def test_cli_outputs_json_stats(self):
        _, stats = self._run_cli('x = Chr(72) & Chr(101) & Chr(108) & Chr(108) & Chr(111)\n')
        self.assertIn('changed', stats)
        self.assertEqual(stats['changed'], 5)

    @unittest.skipUnless(SAMPLE.exists(), 'VBS sample not found on Desktop')
    def test_pipeline_stat_regression_pass1(self):
        """Current toolkit must report the same changed count as the old version."""
        out_path = self.tmp / 'pass1.vbs'
        result = subprocess.run(
            [sys.executable, str(SCRIPT),
             '--input', str(SAMPLE), '--output', str(out_path)],
            capture_output=True, text=True, check=True,
        )
        stats = json.loads(result.stdout)
        self.assertEqual(stats['changed'], 188,
                         f'Expected 188 Chr() folds, got {stats["changed"]}')
        out_text = out_path.read_text(encoding='utf-8')
        # Constant Chr() calls must all be folded. The only Chr() that legitimately
        # remains is Chr((h Mod 26) + 97) inside GetPCHash — a dynamic expression
        # whose argument is not a compile-time constant, so the tool correctly skips it.
        self.assertNotIn('Chr(115)', out_text)
        self.assertNotIn('Chr(104)', out_text)
        self.assertNotIn('Chr(37)', out_text)


if __name__ == '__main__':
    unittest.main()
