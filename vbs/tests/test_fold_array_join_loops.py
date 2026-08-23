"""Unit, CLI, and synthetic-pipeline tests for vbs_fold_array_join_loops.py.

Covers every control flow exercised by _one_pass and its helpers:
  - Basic canonical fold (accum = "" + For/UBound loop → joined literal)
  - Non-empty accumulator initializer is prepended to the join
  - Non-zero start index slices the array elements
  - Explicit Step 1 is accepted
  - Step != 1 prevents the fold
  - Non-constant Step prevents the fold
  - Body using '+' instead of '&' is still folded
  - Body with more than one statement prevents the fold
  - Body with wrong shape (not accum = accum OP arr(i)) prevents the fold
  - UBound with extra argument prevents the fold
  - Nearest array writer is not a literal Array() call — prevents the fold
  - For loop is the very first real statement (pos==0 guard) — prevents the fold
  - Array element that is not a constant prevents the fold
  - Empty Array() with empty init folds to empty string
  - Elements containing embedded double-quotes are re-quoted
  - Idempotency (second pass reports changed==0)
  - Two independent loops in one file are both folded
  - Synthetic pipeline: propagate → fold_split → fold_array_join → deadcode
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_fold_array_join_loops as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_fold_array_join_loops.py'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pass(script: Path, inp: Path, out: Path, extra: list | None = None) -> dict:
    cmd = [sys.executable, str(script), '--input', str(inp), '--output', str(out)]
    if extra:
        cmd.extend(extra)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# Minimal canonical pattern — easy to copy/tweak in each test
_CANONICAL = """\
arr = Array("hello", " ", "world")
accum = ""
For i = 0 To UBound(arr)
    accum = accum & arr(i)
Next
"""

# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestFoldArrayJoinLoops(unittest.TestCase):

    def test_basic_join_produces_concatenated_literal(self):
        out, stats = tool.run(_CANONICAL)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "hello world"', out)
        self.assertNotIn('For ', out)
        self.assertNotIn('Next', out)

    def test_nonempty_init_is_prepended(self):
        src = (
            'arr = Array("world")\n'
            'accum = "hello "\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "hello world"', out)
        self.assertNotIn('For ', out)

    def test_start_index_nonzero_slices_elements(self):
        # For i = 2 To UBound(arr) — only elements at index 2+ are joined
        src = (
            'arr = Array("x", "y", "z")\n'
            'accum = ""\n'
            'For i = 2 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "z"', out)

    def test_explicit_step_one_is_accepted(self):
        src = (
            'arr = Array("a", "b")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr) Step 1\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "ab"', out)

    def test_step_not_one_not_folded(self):
        src = (
            'arr = Array("a", "b", "c")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr) Step 2\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_step_nonconstant_not_folded(self):
        src = (
            'arr = Array("a", "b")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr) Step n\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_plus_operator_in_body_is_accepted(self):
        src = (
            'arr = Array("a", "b")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum + arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "ab"', out)

    def test_body_extra_statement_not_folded(self):
        src = (
            'arr = Array("a", "b")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            '    x = 1\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_body_appends_constant_not_folded(self):
        # accum = accum & "literal" does not match the exact 8-token shape
        src = (
            'arr = Array("a", "b")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & "x"\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_body_mismatched_array_name_not_folded(self):
        # Body uses a different array name than the header
        src = (
            'arr = Array("a", "b")\n'
            'other = Array("x")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & other(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_ubound_with_extra_arg_not_folded(self):
        # UBound(arr, 1) has 5 tokens inside For header — not the exact 4-token shape
        src = (
            'arr = Array("a", "b")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr, 1)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_array_reassigned_to_non_literal_not_folded(self):
        # The nearest write to arr before the For is not a literal Array()
        src = (
            'arr = Array("a", "b")\n'
            'arr = someFunc()\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_for_is_first_real_statement_not_folded(self):
        # pos==0 guard: no room for an init statement before the For
        src = (
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_nonconstant_array_element_not_folded(self):
        src = (
            'arr = Array("a", someVar)\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_empty_array_with_empty_init_folds_to_empty_string(self):
        # Array() has no elements; loop body never runs; accum stays as init value
        src = (
            'arr = Array()\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = ""', out)
        self.assertNotIn('For ', out)

    def test_result_element_with_embedded_quote_is_requoted(self):
        # Array element a"b must appear as a""b in the resulting string literal
        src = (
            'arr = Array("a""b", "c")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        # The joined value is a"bc, which must be quoted as "a""bc"
        self.assertIn('"a""bc"', out)

    def test_idempotency_second_pass_reports_zero(self):
        out1, stats1 = tool.run(_CANONICAL)
        self.assertEqual(stats1['changed'], 1)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)
        self.assertEqual(out1, out2)

    def test_two_independent_loops_both_folded(self):
        src = (
            'arr1 = Array("a", "b")\n'
            'accum1 = ""\n'
            'For i = 0 To UBound(arr1)\n'
            '    accum1 = accum1 & arr1(i)\n'
            'Next\n'
            'arr2 = Array("x", "y")\n'
            'accum2 = ""\n'
            'For j = 0 To UBound(arr2)\n'
            '    accum2 = accum2 & arr2(j)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 2)
        self.assertIn('accum1 = "ab"', out)
        self.assertIn('accum2 = "xy"', out)
        self.assertNotIn('For ', out)

    def test_numeric_array_elements_are_stringified(self):
        # Array(1, 2, 3) elements should be converted to strings and joined
        src = (
            'arr = Array(1, 2, 3)\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "123"', out)

    def test_init_statement_must_be_constant_assign(self):
        # If the statement immediately before the For is not a const assign to accum,
        # the fold is declined (different variable name)
        src = (
            'arr = Array("a", "b")\n'
            'other = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_no_for_loop_in_source_reports_zero(self):
        src = 'x = "hello"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_accum_used_after_loop_still_folds(self):
        # The fold replaces the init+loop with a single 'accum = <joined>'
        # assignment, so any use of accum after the loop still sees the
        # correct final value — the downstream read is not disrupted.
        src = (
            'arr = Array("hello", " ", "world")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
            'WScript.Echo accum\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('"hello world"', out)
        self.assertNotIn('For i', out)
        self.assertIn('WScript.Echo accum', out)

    def test_array_elements_resolved_via_builtin(self):
        # Array elements that are pure-builtin calls (e.g. Chr()) are
        # resolved by resolve_const when _find_array_literal reads each
        # element, so the join still produces the correct string.
        src = (
            'arr = Array(Chr(72), Chr(101), Chr(108), Chr(108), Chr(111))\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "Hello"', out)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestFoldArrayJoinLoopsCli(unittest.TestCase):

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

    def test_cli_basic_fold(self):
        out, stats = self._run_cli(_CANONICAL)
        self.assertIn('accum = "hello world"', out)
        self.assertEqual(stats['changed'], 1)

    def test_cli_outputs_json_with_changed_key(self):
        _, stats = self._run_cli(_CANONICAL)
        self.assertIn('changed', stats)
        self.assertIsInstance(stats['changed'], int)

    def test_cli_zero_when_no_for_loop(self):
        _, stats = self._run_cli('x = "hello"\n')
        self.assertEqual(stats['changed'], 0)

    def test_cli_nonempty_init_preserved(self):
        src = (
            'arr = Array("world")\n'
            'accum = "hello "\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
        )
        out, stats = self._run_cli(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('accum = "hello world"', out)


# ---------------------------------------------------------------------------
# Synthetic pipeline test: propagate → fold_split → fold_array_join → deadcode
# ---------------------------------------------------------------------------

# This source exercises the exact obfuscation pattern fold_split_calls and
# fold_array_join_loops were designed to dismantle:
#   1. A string constant is propagated into a Split() call.
#   2. Split() folds to Array(...).
#   3. The For/UBound loop folds to a single literal.
#   4. Dead-code removal cleans up the now-unused intermediate variables.
_SYNTH_PIPELINE_SRC = """\
strA = "hello world foo"
arr = Split(strA, " ")
accum = ""
For i = 0 To UBound(arr)
    accum = accum & arr(i)
Next
WScript.Echo accum
"""

_PIPELINE_SCRIPTS = [
    (TOOL_DIR / 'vbs_propagate_constants.py', []),
    (TOOL_DIR / 'vbs_fold_split_calls.py',    []),
    (SCRIPT,                                    []),
    (TOOL_DIR / 'vbs_remove_deadcode.py',      []),
]


class TestSyntheticSplitJoinPipeline(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _run_pipeline(self):
        paths = [self.tmp / f'pass{i}.vbs' for i in range(len(_PIPELINE_SCRIPTS) + 1)]
        paths[0].write_bytes(_SYNTH_PIPELINE_SRC.encode('utf-8'))
        stats_list = []
        for i, (script, extra) in enumerate(_PIPELINE_SCRIPTS):
            stats_list.append(_run_pass(script, paths[i], paths[i + 1], extra))
        final = paths[-1].read_text(encoding='utf-8')
        return stats_list, final

    def test_pass1_propagate_substitutes_strA(self):
        stats_list, _ = self._run_pipeline()
        # strA = "hello world foo" is substituted into Split(strA, " ")
        self.assertGreaterEqual(stats_list[0]['changed'], 1)

    def test_pass2_fold_split_folds_call(self):
        stats_list, _ = self._run_pipeline()
        # Split("hello world foo", " ") → Array("hello", "world", "foo")
        self.assertEqual(stats_list[1]['changed'], 1)

    def test_pass3_fold_array_join_folds_loop(self):
        stats_list, _ = self._run_pipeline()
        # For/UBound loop → accum = "helloworldfoo"
        self.assertEqual(stats_list[2]['changed'], 1)

    def test_pass4_deadcode_removes_intermediates(self):
        stats_list, _ = self._run_pipeline()
        # strA and arr are now dead (no remaining read sites)
        self.assertGreaterEqual(stats_list[3]['changed'], 1)

    def test_final_output_has_joined_literal(self):
        _, final = self._run_pipeline()
        self.assertIn('"helloworldfoo"', final)

    def test_final_output_has_no_split_call(self):
        _, final = self._run_pipeline()
        self.assertNotIn('Split(', final)

    def test_final_output_has_no_for_loop(self):
        _, final = self._run_pipeline()
        self.assertNotIn('For ', final)

    def test_final_output_preserves_wscript_echo(self):
        _, final = self._run_pipeline()
        self.assertIn('WScript.Echo', final)

    def test_final_output_has_no_dead_intermediate_vars(self):
        _, final = self._run_pipeline()
        # strA and arr should have been removed as dead variables
        self.assertNotIn('strA', final)
        self.assertNotIn('\narr =', final)


if __name__ == '__main__':
    unittest.main()
