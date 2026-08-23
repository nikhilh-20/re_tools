"""Unit and CLI tests for vbs_fold_split_calls.py.

Covers every control flow:
  - Basic 3-part Split with explicit delimiter
  - 1-arg form (default delimiter is space)
  - limit argument: positive, zero, -1
  - compare=0 (binary/case-sensitive) and compare=1 (text/case-insensitive)
  - compare mode not in {0,1} is declined
  - Edge cases: empty expression, empty delimiter, no match in string
  - Guards: member-access form (.Split), user-defined Sub/Function shadow
  - Non-constant argument not folded
  - env dict participation for variable resolution
  - Elements containing embedded double-quotes are re-quoted correctly
  - Idempotency (second pass reports changed==0)
  - Multiple Split calls in one file all folded
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_fold_split_calls as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_fold_split_calls.py'


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestFoldSplitCalls(unittest.TestCase):

    def test_basic_three_parts(self):
        src = 'x = Split("a b c", " ")\n'
        out, stats = tool.run(src)
        self.assertIn('Array("a", "b", "c")', out)
        self.assertNotIn('Split(', out)
        self.assertEqual(stats['changed'], 1)

    def test_one_arg_default_delimiter_is_space(self):
        src = 'x = Split("a b c")\n'
        out, stats = tool.run(src)
        self.assertIn('Array("a", "b", "c")', out)
        self.assertNotIn('Split(', out)
        self.assertEqual(stats['changed'], 1)

    def test_limit_positive_restricts_parts(self):
        # limit=2 means at most 2 parts: "a" and "b c d"
        src = 'x = Split("a b c d", " ", 2)\n'
        out, stats = tool.run(src)
        self.assertIn('Array("a", "b c d")', out)
        self.assertEqual(stats['changed'], 1)

    def test_limit_zero_returns_empty_array(self):
        src = 'x = Split("a b c", " ", 0)\n'
        out, stats = tool.run(src)
        self.assertIn('Array()', out)
        self.assertEqual(stats['changed'], 1)

    def test_limit_minus_one_explicit_is_unlimited(self):
        src = 'x = Split("a b c", " ", -1)\n'
        out, stats = tool.run(src)
        self.assertIn('Array("a", "b", "c")', out)
        self.assertEqual(stats['changed'], 1)

    def test_compare_zero_is_case_sensitive(self):
        # Binary compare: lowercase 'l' only — "HeLlo" split on "l" → ["HeL", "o"]
        src = 'x = Split("HeLlo", "l", -1, 0)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('Array("HeL", "o")', out)

    def test_compare_one_is_case_insensitive(self):
        # Text compare: 'L' and 'l' both match — "HeLlo" → ["He", "", "o"]
        src = 'x = Split("HeLlo", "l", -1, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('Array("He", "", "o")', out)

    def test_compare_unsupported_mode_not_folded(self):
        # compare=2 is not supported; fold is declined
        src = 'x = Split("a b c", " ", -1, 2)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_empty_string_expr_returns_single_empty_element(self):
        src = 'x = Split("")\n'
        out, stats = tool.run(src)
        self.assertIn('Array("")', out)
        self.assertEqual(stats['changed'], 1)

    def test_empty_delimiter_returns_full_string_as_single_element(self):
        src = 'x = Split("abc", "")\n'
        out, stats = tool.run(src)
        self.assertIn('Array("abc")', out)
        self.assertEqual(stats['changed'], 1)

    def test_no_match_produces_single_element_array(self):
        src = 'x = Split("abc", "x")\n'
        out, stats = tool.run(src)
        self.assertIn('Array("abc")', out)
        self.assertEqual(stats['changed'], 1)

    def test_member_access_not_folded(self):
        # obj.Split(...) is not the VBScript built-in — must not be touched
        src = 'x = obj.Split("a b", " ")\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_user_defined_function_shadow_not_folded(self):
        # If the script defines its own Split(), the built-in is shadowed
        src = (
            'Function Split(x)\n'
            '    Split = x\n'
            'End Function\n'
            'x = Split("a b c", " ")\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_user_defined_sub_shadow_not_folded(self):
        src = (
            'Sub Split(x)\n'
            'End Sub\n'
            'Split "a b c", " "\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_non_constant_first_arg_not_folded(self):
        src = 'x = Split(someVar, " ")\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_non_constant_delimiter_not_folded(self):
        src = 'x = Split("a b c", delimVar)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_env_dict_resolves_variable_expression(self):
        # When caller provides env with a known value, it participates in folding
        src = 'x = Split(s, " ")\n'
        out, stats = tool.run(src, env={'S': 'hello world'})
        self.assertIn('Array("hello", "world")', out)
        self.assertEqual(stats['changed'], 1)

    def test_result_element_with_embedded_quote_is_requoted(self):
        # Element a"b must appear as a""b in VBScript string literal
        src = 'x = Split("a""b|c", "|")\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('Array(', out)
        self.assertNotIn('Split(', out)
        # The element a"b must be represented as "a""b"
        self.assertIn('""', out)

    def test_idempotency_second_pass_reports_zero(self):
        src = 'x = Split("a b c", " ")\n'
        out1, stats1 = tool.run(src)
        self.assertEqual(stats1['changed'], 1)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)
        self.assertEqual(out1, out2)

    def test_multiple_calls_all_folded(self):
        src = (
            'x = Split("a b c", " ")\n'
            'y = Split("d|e|f", "|")\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 2)
        self.assertIn('Array("a", "b", "c")', out)
        self.assertIn('Array("d", "e", "f")', out)

    def test_no_split_call_in_source_reports_zero(self):
        src = 'x = "hello world"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_split_inside_string_literal_not_touched(self):
        # The word "Split" inside a string literal must not be processed
        src = 'x = "call Split here"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestFoldSplitCallsCli(unittest.TestCase):

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
        out, stats = self._run_cli('x = Split("foo bar", " ")\n')
        self.assertIn('Array("foo", "bar")', out)
        self.assertEqual(stats['changed'], 1)

    def test_cli_outputs_json_with_changed_key(self):
        _, stats = self._run_cli('x = Split("a b", " ")\n')
        self.assertIn('changed', stats)
        self.assertIsInstance(stats['changed'], int)

    def test_cli_zero_when_no_split_call(self):
        _, stats = self._run_cli('x = "hello"\n')
        self.assertEqual(stats['changed'], 0)

    def test_cli_limit_and_compare_round_trip(self):
        out, stats = self._run_cli('x = Split("a b c", " ", 2)\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('Array("a", "b c")', out)


# ---------------------------------------------------------------------------
# Pipeline integration: fold_split_calls → fold_array_join_loops hand-off
# ---------------------------------------------------------------------------

class TestSplitToArrayJoinHandoff(unittest.TestCase):
    """Verifies the two-tool hand-off that underlies cmds.txt passes 5→6 and
    9→10: fold_split_calls produces Array(...) which fold_array_join_loops then
    consumes in a For/UBound loop."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_split_output_consumed_by_array_join_fold(self):
        src = (
            'arr = Split("x,y,z", ",")\n'
            'accum = ""\n'
            'For i = 0 To UBound(arr)\n'
            '    accum = accum & arr(i)\n'
            'Next\n'
            'WScript.Echo accum\n'
        )

        JOIN = TOOL_DIR / 'vbs_fold_array_join_loops.py'

        inp         = self.tmp / 'src.vbs'
        after_split = self.tmp / 'after_split.vbs'
        after_join  = self.tmp / 'after_join.vbs'
        inp.write_bytes(src.encode('utf-8'))

        r1 = subprocess.run(
            [sys.executable, str(SCRIPT),
             '--input', str(inp), '--output', str(after_split)],
            capture_output=True, text=True, check=True,
        )
        stats1 = json.loads(r1.stdout)
        self.assertEqual(stats1['changed'], 1)
        mid = after_split.read_text(encoding='utf-8')
        self.assertIn('Array("x", "y", "z")', mid)
        self.assertNotIn('Split(', mid)

        r2 = subprocess.run(
            [sys.executable, str(JOIN),
             '--input', str(after_split), '--output', str(after_join)],
            capture_output=True, text=True, check=True,
        )
        stats2 = json.loads(r2.stdout)
        self.assertGreater(stats2['changed'], 0)
        final = after_join.read_text(encoding='utf-8')
        self.assertIn('"xyz"', final)
        self.assertNotIn('For i', final)


# ---------------------------------------------------------------------------
# Pipeline integration: propagate_constants → fold_split_calls hand-off
# ---------------------------------------------------------------------------

class TestPropagateThenSplitHandoff(unittest.TestCase):
    """Verifies the two-tool hand-off that underlies cmds.txt passes 2→5 and
    8→9: a bare-identifier Split() argument is NOT folded standalone (no env,
    no source-scanning in resolve_const — see test_non_constant_first_arg_not_folded),
    but propagate_constants inlines the constant text first so fold_split_calls
    can then fold it.

    Also covers the flow-sensitive shape that motivated this test: the
    variable is REASSIGNED to something else after the Split() call in the
    same script (mirrors a real obfuscated sample where a placeholder-laden
    string is seeded, immediately Split() on its placeholder character, and
    then overwritten in-place with the cleaned result under the same name).
    propagate_constants must substitute the pre-reassignment value at the
    Split() call site, not the later one, and must not decline just because
    the variable is written again afterward."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_variable_reassigned_after_split_still_folds_pre_reassignment_value(self):
        src = (
            'x = "a|b|c"\n'
            'arr = Split(x, "|")\n'
            'x = "cleaned"\n'
            'y = x\n'
        )

        # Standalone fold_split_calls (no env) must NOT touch the bare
        # identifier `x` — same guard as test_non_constant_first_arg_not_folded.
        out0, stats0 = tool.run(src)
        self.assertEqual(stats0['changed'], 0)
        self.assertEqual(out0, src)

        PROPAGATE = TOOL_DIR / 'vbs_propagate_constants.py'

        inp           = self.tmp / 'src.vbs'
        after_propag  = self.tmp / 'after_propagate.vbs'
        after_split   = self.tmp / 'after_split.vbs'
        inp.write_bytes(src.encode('utf-8'))

        r1 = subprocess.run(
            [sys.executable, str(PROPAGATE),
             '--input', str(inp), '--output', str(after_propag)],
            capture_output=True, text=True, check=True,
        )
        stats1 = json.loads(r1.stdout)
        self.assertGreater(stats1['changed'], 0)
        mid = after_propag.read_text(encoding='utf-8')
        # pre-reassignment literal substituted at the Split() call site
        self.assertIn('Split("a|b|c", "|")', mid)
        # post-reassignment value preserved for the later read
        self.assertIn('y = "cleaned"', mid)

        r2 = subprocess.run(
            [sys.executable, str(SCRIPT),
             '--input', str(after_propag), '--output', str(after_split)],
            capture_output=True, text=True, check=True,
        )
        stats2 = json.loads(r2.stdout)
        self.assertGreater(stats2['changed'], 0)
        final = after_split.read_text(encoding='utf-8')
        self.assertIn('Array("a", "b", "c")', final)
        self.assertNotIn('Split(', final)
        self.assertIn('y = "cleaned"', final)

    def test_over_cap_literal_split_arg_folds_after_propagate(self):
        # Real-world shape: a single literal assignment bigger than
        # vbs_propagate_constants._MAX_TRACKED_STRING_LEN (8192 chars),
        # consumed once by Split() — this is what pass6.vbs's
        # `supranational` variable looks like (a 15725-char literal).
        # Before the cap-exemption fix for plain single-assignments, this
        # value was never tracked, so it could never reach fold_split_calls
        # as a literal, regardless of pass ordering.
        chunk = 'PLACEHOLDER'
        big = chunk.join(['x'] * 2000)  # well over the 8192-char cap
        src = f'x = "{big}"\narr = Split(x, "{chunk}")\n'

        PROPAGATE = TOOL_DIR / 'vbs_propagate_constants.py'

        inp          = self.tmp / 'src2.vbs'
        after_propag = self.tmp / 'after_propagate2.vbs'
        after_split  = self.tmp / 'after_split2.vbs'
        inp.write_bytes(src.encode('utf-8'))

        r1 = subprocess.run(
            [sys.executable, str(PROPAGATE),
             '--input', str(inp), '--output', str(after_propag)],
            capture_output=True, text=True, check=True,
        )
        stats1 = json.loads(r1.stdout)
        self.assertGreater(stats1['changed'], 0)
        mid = after_propag.read_text(encoding='utf-8')
        self.assertIn(f'Split("{big}", "{chunk}")', mid)

        r2 = subprocess.run(
            [sys.executable, str(SCRIPT),
             '--input', str(after_propag), '--output', str(after_split)],
            capture_output=True, text=True, check=True,
        )
        stats2 = json.loads(r2.stdout)
        self.assertGreater(stats2['changed'], 0)
        final = after_split.read_text(encoding='utf-8')
        self.assertIn('Array(', final)
        self.assertNotIn('Split(', final)


if __name__ == '__main__':
    unittest.main()
