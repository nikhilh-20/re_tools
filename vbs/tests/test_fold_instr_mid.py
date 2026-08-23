"""Unit and regression tests for vbs_fold_instr_mid.py.

Covers every control flow:
  - 2-arg, 3-arg, 4-arg InStr forms (binary compare accepted; vbTextCompare rejected)
  - Subject must be a bare variable IDENT (not a literal, compound expression, or member)
  - Needle must be a non-empty string constant resolvable at analysis time
  - Mid subject must match the tracked InStr subject; pos var must be in instr_calls
  - Length arg must resolve (via resolve_const) to exactly len(needle)
  - 2-arg Mid (no length) is never folded
  - obj.Mid(...) member access is NOT folded
  - Mid inside a larger expression (not just full-statement RHS) is folded
  - Invalidation: reassigning the posvar OR the subject var drops the tracked entry
  - Let-prefixed assignment is recognised and tracked normally
  - stats dict always has changed == folded
  - Pipeline regression (pass 6 of cmds.txt): 3fefc18c sample → changed==2, folded==2
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_fold_instr_mid as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_fold_instr_mid.py'


def _run_script(script: Path, inp: Path, out: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(script), '--input', str(inp), '--output', str(out)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Class 1: _match_instr_call branches — arity and argument validity
# ---------------------------------------------------------------------------

class TestInstrCallMatching(unittest.TestCase):

    def test_2arg_form_folds(self):
        src = 'pos = InStr(myStr, "abc")\nch = Mid(myStr, pos, 3)\n'
        out, stats = tool.run(src)
        self.assertIn('"abc"', out)
        self.assertNotIn('Mid(', out)
        self.assertEqual(stats['changed'], 1)

    def test_3arg_form_folds(self):
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertNotIn('Mid(', out)
        self.assertEqual(stats['changed'], 1)

    def test_4arg_compare0_folds(self):
        src = 'pos = InStr(1, myStr, "hello", 0)\nch = Mid(myStr, pos, 5)\n'
        out, stats = tool.run(src)
        self.assertIn('"hello"', out)
        self.assertNotIn('Mid(', out)
        self.assertEqual(stats['changed'], 1)

    def test_4arg_compare1_not_tracked(self):
        # compare=1 (vbTextCompare) is rejected — Mid could return differently-cased content
        src = 'pos = InStr(1, myStr, "hello", 1)\nch = Mid(myStr, pos, 5)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Mid(', out)

    def test_4arg_vbTextCompare_not_tracked(self):
        # vbTextCompare resolves to 1 via intrinsic constants
        src = 'pos = InStr(1, myStr, "hello", vbTextCompare)\nch = Mid(myStr, pos, 5)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Mid(', out)

    def test_4arg_vbBinaryCompare_folds(self):
        # vbBinaryCompare resolves to 0 — binary compare is safe
        src = 'pos = InStr(1, myStr, "hello", vbBinaryCompare)\nch = Mid(myStr, pos, 5)\n'
        out, stats = tool.run(src)
        self.assertIn('"hello"', out)
        self.assertEqual(stats['changed'], 1)

    def test_5arg_invalid_arity_not_matched(self):
        src = 'pos = InStr(1, myStr, "s", 0, 99)\nch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_string_literal_subject_not_tracked(self):
        # Subject must be a bare IDENT, not a string literal
        src = 'pos = InStr(1, "literal", "s")\nch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_compound_subject_not_tracked(self):
        # obj.Prop is multiple tokens, not a single bare IDENT
        src = 'pos = InStr(1, obj.Prop, "s")\nch = Mid(obj.Prop, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_empty_needle_not_tracked(self):
        src = 'pos = InStr(1, myStr, "")\nch = Mid(myStr, pos, 0)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_unresolvable_needle_not_tracked(self):
        src = 'pos = InStr(1, myStr, unknownVar)\nch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)


# ---------------------------------------------------------------------------
# Class 2: _try_fold_mid decision tree
# ---------------------------------------------------------------------------

class TestMidFolding(unittest.TestCase):

    def test_subject_mismatch_not_folded(self):
        # InStr tracked myStr, but Mid uses otherStr
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(otherStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Mid(', out)

    def test_untracked_posvar_not_folded(self):
        src = 'ch = Mid(myStr, unknownPos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Mid(', out)

    def test_length_mismatch_not_folded(self):
        # needle "s" has len 1, but Mid uses 2
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 2)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_2arg_mid_never_folded(self):
        # 2-arg Mid(s, start) has no length arg — not handled
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_paren_subject_not_folded(self):
        # (myStr) is 3 tokens, not a bare IDENT
        src = 'pos = InStr(1, myStr, "s")\nch = Mid((myStr), pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_paren_posvar_not_folded(self):
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, (pos), 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_len_expression_in_third_arg_resolves_correctly(self):
        # Len("s") resolves to 1, which equals len("s") — fold should succeed
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, Len("s"))\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertEqual(stats['changed'], 1)

    def test_multichar_needle_folds(self):
        src = 'pos = InStr(1, myStr, "hello")\nch = Mid(myStr, pos, 5)\n'
        out, stats = tool.run(src)
        self.assertIn('"hello"', out)
        self.assertEqual(stats['changed'], 1)

    def test_needle_with_embedded_quote_folded_and_requoted(self):
        # VBScript "say ""hi""" is the string: say "hi" (len 8)
        src = 'pos = InStr(1, myStr, "say ""hi""")\nch = Mid(myStr, pos, 8)\n'
        out, stats = tool.run(src)
        # quote_vbs('say "hi"') == '"say ""hi"""'
        self.assertIn('"say ""hi"""', out)
        self.assertEqual(stats['changed'], 1)


# ---------------------------------------------------------------------------
# Class 3: _scan_mid_calls guards
# ---------------------------------------------------------------------------

class TestMidScanGuards(unittest.TestCase):

    def test_member_access_mid_not_folded(self):
        src = 'pos = InStr(1, myStr, "s")\nch = obj.Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('obj.Mid(', out)

    def test_mid_inside_concat_expression_folds(self):
        # Mid is not the whole RHS — it's inside a & chain
        src = 'pos = InStr(1, myStr, "s")\nx = "pre" & Mid(myStr, pos, 1) & "suf"\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertNotIn('Mid(', out)
        self.assertEqual(stats['changed'], 1)

    def test_mid_on_separate_line_from_instr_folds(self):
        # instr_calls persists across statement boundaries in the same pass
        src = 'pos = InStr(1, myBlob, "x")\nch = Mid(myBlob, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertIn('"x"', out)
        self.assertEqual(stats['changed'], 1)

    def test_mid_in_instr_subject_arg_no_fold_no_crash(self):
        # Mid appears as the subject argument of InStr — complex expression,
        # neither call results in a fold, but there must be no exception
        src = 'pos1 = InStr(1, Mid(other, x, 1), "s")\nch = Mid(myStr, pos1, 1)\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)


# ---------------------------------------------------------------------------
# Class 4: invalidation of tracked entries
# ---------------------------------------------------------------------------

class TestInvalidation(unittest.TestCase):

    def test_posvar_reassigned_kills_entry(self):
        src = (
            'pos = InStr(1, myStr, "s")\n'
            'pos = 99\n'
            'ch = Mid(myStr, pos, 1)\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('Mid(', out)

    def test_subject_reassigned_kills_entry(self):
        src = (
            'pos = InStr(1, myStr, "s")\n'
            'myStr = "newvalue"\n'
            'ch = Mid(myStr, pos, 1)\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_posvar_overwritten_with_new_instr_uses_new_needle(self):
        # Second InStr overwrites first entry — new needle wins
        src = (
            'pos = InStr(1, myStr, "s")\n'
            'pos = InStr(1, myStr, "ab")\n'
            'ch = Mid(myStr, pos, 2)\n'
        )
        out, stats = tool.run(src)
        self.assertIn('"ab"', out)
        # "s" still appears inside the unreplaced InStr call — that's expected
        self.assertNotIn('Mid(', out)
        self.assertEqual(stats['changed'], 1)

    def test_posvar_overwritten_with_rejected_instr_kills_entry(self):
        # compare=1 InStr is rejected and also evicts the prior valid entry
        src = (
            'pos = InStr(1, myStr, "s")\n'
            'pos = InStr(1, myStr, "s", 1)\n'
            'ch = Mid(myStr, pos, 1)\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_subject_reassign_kills_all_entries_for_that_subject(self):
        # Both pos1 and pos2 point to myStr — reassigning myStr evicts both
        src = (
            'pos1 = InStr(1, myStr, "s")\n'
            'pos2 = InStr(1, myStr, "ab")\n'
            'myStr = "fresh"\n'
            'ch1 = Mid(myStr, pos1, 1)\n'
            'ch2 = Mid(myStr, pos2, 2)\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)


# ---------------------------------------------------------------------------
# Class 5: special syntactic forms
# ---------------------------------------------------------------------------

class TestSpecialForms(unittest.TestCase):

    def test_let_prefix_on_instr_assignment_tracked(self):
        src = 'Let pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertEqual(stats['changed'], 1)

    def test_let_prefix_on_both_assignments_folds(self):
        src = 'Let pos = InStr(1, myStr, "s")\nLet ch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertEqual(stats['changed'], 1)

    def test_uppercase_keywords_fold(self):
        src = 'pos = INSTR(1, myStr, "s")\nch = MID(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertEqual(stats['changed'], 1)

    def test_empty_source_gives_zero_stats(self):
        out, stats = tool.run('')
        self.assertEqual(out, '')
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(stats['folded'], 0)

    def test_no_instr_pattern_gives_zero_stats(self):
        src = 'x = 42\ny = "hello"\n'
        out, stats = tool.run(src)
        self.assertEqual(out, src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotency(self):
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 1)\n'
        out1, stats1 = tool.run(src)
        self.assertEqual(stats1['changed'], 1)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)
        self.assertEqual(out2, out1)

    def test_dim_before_instr_does_not_prevent_tracking(self):
        # Dim is excluded from _match_simple_assignment and does not clear instr_calls
        src = 'Dim pos\npos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 1)\n'
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertEqual(stats['changed'], 1)


# ---------------------------------------------------------------------------
# Class 6: stats dict invariants
# ---------------------------------------------------------------------------

class TestStatsDict(unittest.TestCase):

    def test_stats_keys_present_on_fold(self):
        src = 'pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 1)\n'
        _, stats = tool.run(src)
        self.assertIn('changed', stats)
        self.assertIn('folded', stats)
        self.assertEqual(stats['changed'], stats['folded'])
        self.assertEqual(stats['changed'], 1)

    def test_stats_zero_when_nothing_folds(self):
        _, stats = tool.run('x = 42\n')
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(stats['folded'], 0)

    def test_two_independent_pairs_both_fold(self):
        src = (
            'pos1 = InStr(1, blob1, "s")\n'
            'ch1 = Mid(blob1, pos1, 1)\n'
            'pos2 = InStr(1, blob2, "ab")\n'
            'ch2 = Mid(blob2, pos2, 2)\n'
        )
        out, stats = tool.run(src)
        self.assertIn('"s"', out)
        self.assertIn('"ab"', out)
        self.assertNotIn('Mid(', out)
        self.assertEqual(stats['changed'], 2)
        self.assertEqual(stats['folded'], 2)

    def test_same_posvar_used_by_two_mid_calls_both_fold(self):
        # instr_calls entry is not consumed on a successful fold
        src = (
            'pos = InStr(1, myStr, "s")\n'
            'ch1 = Mid(myStr, pos, 1)\n'
            'ch2 = Mid(myStr, pos, 1)\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 2)
        self.assertEqual(stats['folded'], 2)
        self.assertNotIn('Mid(', out)


# ---------------------------------------------------------------------------
# Class 7: CLI interface
# ---------------------------------------------------------------------------

class TestFoldInstrMidCli(unittest.TestCase):

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

    def test_basic_fold_via_cli(self):
        out, stats = self._run_cli('pos = InStr(1, myStr, "s")\nch = Mid(myStr, pos, 1)\n')
        self.assertIn('"s"', out)
        self.assertEqual(stats['changed'], 1)

    def test_cli_json_has_changed_and_folded_keys(self):
        _, stats = self._run_cli('x = 42\n')
        self.assertIn('changed', stats)
        self.assertIn('folded', stats)

    def test_cli_reports_zero_on_no_fold_input(self):
        _, stats = self._run_cli('x = "hello"\n')
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(stats['folded'], 0)

    def test_4arg_compare0_folds_via_cli(self):
        src = 'pos = InStr(1, myStr, "hello", 0)\nch = Mid(myStr, pos, 5)\n'
        out, stats = self._run_cli(src)
        self.assertIn('"hello"', out)
        self.assertEqual(stats['changed'], 1)


# ---------------------------------------------------------------------------
# Class 8: synthetic pipeline chain — no external file dependency
# ---------------------------------------------------------------------------

# A synthetic source that exercises all 9 passes from the cmds.txt pipeline.
# blob is assigned from a non-constant call so propagate_constants never
# substitutes it, which keeps the InStr/Mid patterns intact for pass 6.
_SYNTH_9PASS_SRC = """\
' strip me 1
' strip me 2
Dim blob
blob = WScript.Arguments.Item(0)
pos1 = InStr(1, blob, "hello")
pos2 = InStr(1, blob, "world")
ch1 = Mid(blob, pos1, 5)
ch2 = Mid(blob, pos2, 5)
Dim deadVar
deadVar = "never used"
WScript.Echo ch1
WScript.Echo ch2
"""


class TestPipelineChain(unittest.TestCase):
    """Runs the full 9-pass cmds.txt pipeline on a synthetic source and
    verifies that fold_instr_mid (pass 6) folds both InStr+Mid pairs."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _run_pipeline(self):
        scripts = [
            TOOL_DIR / 'vbs_strip_comments.py',
            TOOL_DIR / 'vbs_propagate_constants.py',
            TOOL_DIR / 'vbs_fold_builtin_calls.py',
            TOOL_DIR / 'vbs_remove_deadcode.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            SCRIPT,                                      # fold_instr_mid (pass 6)
            TOOL_DIR / 'vbs_propagate_constants.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            TOOL_DIR / 'vbs_remove_deadcode.py',
        ]
        p0 = self.tmp / 'p0.vbs'
        p0.write_text(_SYNTH_9PASS_SRC, encoding='utf-8')
        paths = [p0] + [self.tmp / f'p{i+1}.vbs' for i in range(len(scripts))]
        stats_list = []
        for i, script in enumerate(scripts):
            stats_list.append(_run_script(script, paths[i], paths[i + 1]))
        return stats_list, paths

    def test_pass6_folds_both_instr_mid_pairs(self):
        stats_list, _ = self._run_pipeline()
        s = stats_list[5]  # index 5 = pass 6 (fold_instr_mid)
        self.assertEqual(s['changed'], 2,
                         f'fold_instr_mid: expected changed==2, got {s["changed"]}')
        self.assertEqual(s['folded'], 2,
                         f'fold_instr_mid: expected folded==2, got {s["folded"]}')

    def test_final_output_has_folded_literals(self):
        _, paths = self._run_pipeline()
        final = paths[-1].read_text(encoding='utf-8')
        self.assertIn('"hello"', final)
        self.assertIn('"world"', final)
        self.assertNotIn('Mid(blob,', final)
        self.assertNotIn('Mid(blob, ', final)

    def test_strip_comments_pass_removes_comment_lines(self):
        stats_list, _ = self._run_pipeline()
        s = stats_list[0]  # pass 1 = strip_comments
        self.assertGreaterEqual(s['changed'], 2)

    def test_deadcode_pass_removes_dead_var(self):
        stats_list, _ = self._run_pipeline()
        # pass 4 = remove_deadcode; deadVar is never read
        s = stats_list[3]
        self.assertGreater(s['changed'], 0)


if __name__ == '__main__':
    unittest.main()
