"""Unit and regression tests for vbs_fold_builtin_calls.py.

Covers every control flow exercised by the pipeline:
  - Replace("str@with@ats", "@", "") → folded literal (the ttaffRy-inline pattern)
  - Replace with no matching chars → still folded (Replace is pure / constant)
  - Member access .Replace(...) NOT folded (obj.Replace is not the global builtin)
  - User-defined Function shadowing a builtin name → NOT folded
  - Representative other builtins: Len, UCase, Mid, Chr via the builtin path
  - Pipeline stat regression: changed == 3 (three Replace calls after inlining)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_fold_builtin_calls as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_fold_builtin_calls.py'
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

class TestFoldBuiltinCalls(unittest.TestCase):

    # -- Replace --

    def test_replace_with_ats_folds_to_clean_string(self):
        src = 'x = Replace("WScr@i@pt@.S@@h@@e@ll", "@", "")\n'
        out, stats = tool.run(src)
        self.assertIn('"WScript.Shell"', out)
        self.assertNotIn('Replace(', out)
        self.assertEqual(stats['changed'], 1)

    def test_replace_with_no_matches_still_folds(self):
        # Replace("WScript.Shell", "@", "") → "WScript.Shell" — still a constant fold
        src = 'x = Replace("WScript.Shell", "@", "")\n'
        out, stats = tool.run(src)
        self.assertIn('"WScript.Shell"', out)
        self.assertNotIn('Replace(', out)
        self.assertEqual(stats['changed'], 1)

    def test_replace_with_multiple_ats_in_runll_string(self):
        # Exact pattern from the VBS sample after ttaffRy is inlined
        src = 'x = Replace("Ru@@n@D@@l@@l32.@@e@x@@e", "@", "")\n'
        out, stats = tool.run(src)
        self.assertIn('"RunDll32.exe"', out)
        self.assertNotIn('Replace(', out)
        self.assertEqual(stats['changed'], 1)

    def test_member_access_replace_not_folded(self):
        # obj.Replace(...) is a method call on an object, not the global Replace builtin
        src = 'x = obj.Replace("a", "b", "c")\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('obj.Replace', out)

    def test_user_defined_chr_shadow_not_folded(self):
        # A user Function named Chr hides the builtin; calls must not be folded
        src = (
            'Function Chr(n)\n'
            '    Chr = n\n'
            'End Function\n'
            'x = Chr(65)\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Chr(65)', out)

    # -- Chr --

    def test_chr_folded_via_builtin_path(self):
        src = 'x = Chr(65)\n'
        out, stats = tool.run(src)
        self.assertIn('"A"', out)
        self.assertNotIn('Chr(', out)
        self.assertEqual(stats['changed'], 1)

    # -- Len --

    def test_len_constant_string_folded(self):
        src = 'x = Len("hello")\n'
        out, stats = tool.run(src)
        self.assertIn('5', out)
        self.assertNotIn('Len(', out)
        self.assertEqual(stats['changed'], 1)

    # -- UCase / LCase --

    def test_ucase_folded(self):
        src = 'x = UCase("hello")\n'
        out, stats = tool.run(src)
        self.assertIn('"HELLO"', out)
        self.assertEqual(stats['changed'], 1)

    def test_lcase_folded(self):
        src = 'x = LCase("WORLD")\n'
        out, stats = tool.run(src)
        self.assertIn('"world"', out)
        self.assertEqual(stats['changed'], 1)

    # -- Mid --

    def test_mid_folded(self):
        src = 'x = Mid("hello", 2, 3)\n'
        out, stats = tool.run(src)
        self.assertIn('"ell"', out)
        self.assertEqual(stats['changed'], 1)

    # -- Trim --

    def test_trim_folded(self):
        src = 'x = Trim("  spaces  ")\n'
        out, stats = tool.run(src)
        self.assertIn('"spaces"', out)
        self.assertEqual(stats['changed'], 1)

    # -- Non-constant argument: not folded --

    def test_non_constant_arg_not_folded(self):
        src = 'x = Len(unknownVar)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Len(unknownVar)', out)

    # -- Idempotency --

    def test_already_folded_is_idempotent(self):
        src = 'x = "WScript.Shell"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_second_pass_on_folded_output_reports_zero(self):
        src = 'x = Replace("WScr@i@pt@.S@@h@@e@ll", "@", "")\n'
        out1, stats1 = tool.run(src)
        self.assertEqual(stats1['changed'], 1)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)

    # -- env dict pass-through --

    def test_env_dict_allows_variable_args_to_fold(self):
        src = 'x = Len(v)\n'
        out, stats = tool.run(src, env={'v': 'hello'})
        self.assertIn('5', out)
        self.assertEqual(stats['changed'], 1)

    # -- Builtin name in string is not folded --

    def test_builtin_call_inside_string_not_touched(self):
        src = 'x = "Chr(65)"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestFoldBuiltinCallsCli(unittest.TestCase):

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

    def test_basic_replace_via_cli(self):
        out, stats = self._run_cli('x = Replace("a@b@c", "@", "")\n')
        self.assertIn('"abc"', out)
        self.assertEqual(stats['changed'], 1)

    def test_cli_json_has_changed_key(self):
        _, stats = self._run_cli('x = Chr(65)\n')
        self.assertIn('changed', stats)

    @unittest.skipUnless(SAMPLE.exists(), 'VBS sample not found on Desktop')
    def test_pipeline_stat_regression_pass5(self):
        """Pipeline pass 5 is fold_builtin_calls on the inlined output.
        Baseline: changed == 3."""
        tmp = self.tmp
        p = [SAMPLE,
             tmp / 'pass1.vbs',
             tmp / 'pass2.vbs',
             tmp / 'pass3.vbs',
             tmp / 'pass4.vbs',
             tmp / 'pass5.vbs']

        scripts = [
            TOOL_DIR / 'vbs_fold_chr_calls.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            TOOL_DIR / 'vbs_propagate_constants.py',
            TOOL_DIR / 'vbs_inline_functions.py',
            SCRIPT,
        ]
        for i, script in enumerate(scripts):
            _run_script(script, p[i], p[i + 1])

        stats = _run_script(SCRIPT, p[4], p[5])
        self.assertEqual(stats['changed'], 3,
                         f'Expected changed == 3, got {stats["changed"]}')

        out_text = p[5].read_text(encoding='utf-8')
        self.assertIn('"WScript.Shell"', out_text,
                      'ttaffRy("WScr@i@pt@.S@@h@@e@ll") should fold to "WScript.Shell"')
        self.assertNotIn('Replace(', out_text,
                         'All Replace() calls should be folded to literals')


if __name__ == '__main__':
    unittest.main()
