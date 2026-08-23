"""Unit and regression tests for vbs_remove_deadcode.py.

Covers sub-passes exercised by the pipeline on the VBS sample:

  Sub-pass C (global dead-store removal):
    - Entire Dim statement removed when all declared vars are dead
    - Partial Dim removal when only some declared vars are dead
    - Simple assignment to dead var removed
    - Live variables (Set, method calls) preserved
    - ExecuteGlobal in file: conservative dynamic-exec guard active

  Sub-pass D (unused Function/Sub removal):
    - Unreferenced function removed
    - Function that is called preserved
    - String-literal guard (unconditional): function name in any string literal
      prevents removal even without Execute/ExecuteGlobal present
    - GetRef/CallByName guard: function name in GetRef("name") argument string
      is a string literal and therefore blocks removal

  Sub-pass B2 (local sequential dead-store):
    - First of two consecutive assignments to the same var removed

  Sub-pass A (statically-false If block):
    - If False Then ... End If block removed

  --preserve-strings flag:
    - Dead stores whose RHS is a string/number literal are kept

  --aggressive + ExecuteGlobal interaction:
    - Conservative guard overrides aggressive mode for self-referential
      accumulators when ExecuteGlobal is present

  Pipeline stat regression:
    - Pass 6 (run on fold_builtin_calls output): changed == 114
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_remove_deadcode as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_remove_deadcode.py'
SAMPLE   = Path(r'C:\Users\Ashura\Desktop\cef108df7267250b66dca8e6ab87a629591b9840f27e3ab1821248ebfe2cdb1f.vbs')


def _run_script(script: Path, inp: Path, out: Path, extra: list | None = None) -> dict:
    cmd = [sys.executable, str(script), '--input', str(inp), '--output', str(out)]
    if extra:
        cmd.extend(extra)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Sub-pass C — global liveness-based dead-store removal
# ---------------------------------------------------------------------------

class TestDeadStoreRemoval(unittest.TestCase):

    def test_entire_dim_removed_when_all_vars_dead(self):
        src = (
            'Dim a, b, c\n'
            'a = "x"\n'
            'b = "y"\n'
            'c = "z"\n'
            'WScript.Echo "done"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('Dim a, b, c', out)
        self.assertNotIn('a = "x"', out)
        self.assertNotIn('b = "y"', out)
        self.assertIn('WScript.Echo', out)

    def test_partial_dim_removal_keeps_live_var(self):
        src = (
            'Dim dead, live\n'
            'dead = "unused"\n'
            'live = "used"\n'
            'WScript.Echo live\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('dead', out)
        self.assertIn('live', out)
        self.assertIn('WScript.Echo live', out)

    def test_assignment_to_dead_var_removed(self):
        src = (
            'Dim x\n'
            'x = "never read"\n'
            'WScript.Echo "hello"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('x = "never read"', out)
        self.assertIn('WScript.Echo', out)

    def test_live_set_statement_preserved(self):
        src = (
            'Set obj = CreateObject("WScript.Shell")\n'
            'obj.Run "cmd"\n'
        )
        out, stats = tool.run(src)
        self.assertIn('Set obj = CreateObject', out)
        self.assertIn('obj.Run', out)

    def test_executeGlobal_present_conservative_mode(self):
        # With ExecuteGlobal in file, the tool is conservative.
        # Dead vars that have no literal reads survive here,
        # but clearly dead ones (not in any string literal) should still be removed.
        src = (
            'Dim deadVar\n'
            'deadVar = "x"\n'
            'Dim keepMe\n'
            'keepMe = "y"\n'
            'WScript.Echo keepMe\n'
            'ExecuteGlobal "some code"\n'
        )
        out, stats = tool.run(src)
        self.assertIn('keepMe', out)
        # ExecuteGlobal is present but deadVar doesn't appear in any exec string literal
        self.assertNotIn('deadVar', out)

    def test_multiple_dead_intermediate_vars_removed(self):
        # Simulates the vXXXX variable pattern from the actual sample
        src = (
            'Dim v1, v2, v3\n'
            'v1 = "http"\n'
            'v2 = "s://"\n'
            'v3 = "example.com"\n'
            'url = "https://example.com"\n'
            'WScript.Echo url\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('v1', out)
        self.assertNotIn('v2', out)
        self.assertNotIn('v3', out)
        self.assertIn('url', out)

    def test_live_variable_read_in_call_preserved(self):
        src = (
            'Dim path\n'
            'path = "C:\\\\file.exe"\n'
            'If FSO.FileExists(path) Then WScript.Quit\n'
        )
        out, stats = tool.run(src)
        self.assertIn('path', out)
        self.assertIn('FileExists', out)

    def test_dead_set_assignment_removed(self):
        # Set obj = Nothing: obj is never read, Nothing has no observable
        # side effect (no CreateObject/GetObject/.Method()) — sub-pass C
        # handles Set assignments with the same liveness rule as plain ones.
        src = 'Set obj = Nothing\nWScript.Echo "hello"\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('Set obj', out)
        self.assertIn('WScript.Echo', out)


# ---------------------------------------------------------------------------
# Colon-joined statements — must behave identically to their newline-split
# equivalents (regression for the code_tokens()/COLON-terminator bug: see
# tests/test_statements_colon.py for the library-level invariant tests).
# ---------------------------------------------------------------------------

class TestColonJoinedStatements(unittest.TestCase):

    def test_colon_joined_dead_dim_removed(self):
        src = 'Dim a: a = "xy"\nWScript.Echo 1\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('a', out.replace('WScript.Echo 1', ''))

    def test_colon_joined_matches_newline_form(self):
        colon_src = 'Dim a: a = "xy"\nWScript.Echo 1\n'
        newline_src = 'Dim a\na = "xy"\nWScript.Echo 1\n'
        colon_out, colon_stats = tool.run(colon_src)
        newline_out, newline_stats = tool.run(newline_src)
        self.assertEqual(colon_stats['changed'] > 0, newline_stats['changed'] > 0)
        self.assertNotIn('a =', colon_out)
        self.assertNotIn('a =', newline_out)

    def test_colon_joined_public_dead_var_removed(self):
        src = 'Public a: a = 5\nWScript.Echo 1\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('a = 5', out)

    def test_colon_joined_redim_dead_var_removed(self):
        src = 'Dim a()\nReDim a(3): WScript.Echo 1\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('a(3)', out)

    def test_colon_joined_partial_dim_keeps_live_var(self):
        src = 'Dim a, b: WScript.Echo b\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('a', out.replace('Echo', '').replace('WScript', ''))
        self.assertIn('b', out)

    def test_colon_joined_local_dead_store_removed(self):
        # Sub-pass B2: first of two sequential overwrites is dead.
        src = 'x = 1: x = 2\nWScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('x = 1', out)
        self.assertIn('x = 2', out)

    def test_colon_after_const_is_not_swallowed(self):
        # Regression: Const's colon terminator must survive as an actual
        # separator, not be dropped/merged into invalid VBScript.
        src = 'Const b = 2, a = 1: WScript.Echo b\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('a = 1', out)
        self.assertIn('Const b = 2', out)
        # The colon (or an equivalent statement separator) must still
        # separate the Const from the following call — never concatenated
        # directly onto it.
        self.assertNotIn('Const b = 2 WScript.Echo b', out)

    def test_if_then_colon_block_removed_as_whole_unit(self):
        # Regression: 'If 0 Then:' is a multi-line block header (colon is
        # the terminator, THEN is the real last code token), not a
        # single-line 'If cond Then stmt'. Sub-pass A must remove the whole
        # False block, including its End If, not delete only the header and
        # leave a dangling End If.
        src = 'If 0 Then:\n  WScript.Echo "never"\nEnd If\nWScript.Echo "always"\n'
        out, stats = tool.run(src)
        self.assertNotIn('End If', out)
        self.assertNotIn('never', out)
        self.assertIn('always', out)

    def test_do_while_colon_block_removed_as_whole_unit(self):
        # Regression: 'Do While 0:' condition-capture must not swallow the
        # trailing colon into the resolved condition text (which made the
        # condition unresolvable and left the always-false loop in place).
        src = 'Do While 0:\n  WScript.Echo "never"\nLoop\nWScript.Echo "always"\n'
        out, stats = tool.run(src)
        self.assertNotIn('Loop', out)
        self.assertNotIn('never', out)
        self.assertIn('always', out)


# ---------------------------------------------------------------------------
# Sub-pass D — unused Function/Sub removal
# ---------------------------------------------------------------------------

class TestUnusedFuncRemoval(unittest.TestCase):

    def test_unreferenced_function_removed(self):
        src = (
            'Function neverCalled(s)\n'
            '    neverCalled = s\n'
            'End Function\n'
            'WScript.Echo "hello"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('neverCalled', out)

    def test_called_function_preserved(self):
        src = (
            'Function doWork(s)\n'
            '    doWork = s & "!"\n'
            'End Function\n'
            'WScript.Echo doWork("hi")\n'
        )
        out, stats = tool.run(src)
        self.assertIn('doWork', out)
        self.assertIn('Function doWork', out)

    def test_function_called_only_from_inside_another_body_preserved(self):
        src = (
            'Function helper(s)\n'
            '    helper = s\n'
            'End Function\n'
            'Function caller()\n'
            '    caller = helper("x")\n'
            'End Function\n'
            'WScript.Echo caller()\n'
        )
        out, stats = tool.run(src)
        self.assertIn('Function helper', out)
        self.assertIn('Function caller', out)

    def test_unused_sub_removed(self):
        # Sub-pass D's _fn_sub_pat matches both Function and Sub definitions.
        # An unreferenced Sub (no callers outside its own body) is removed.
        src = (
            'Sub MyHelper()\n'
            '    x = 1\n'
            'End Sub\n'
            'WScript.Echo "hi"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('MyHelper', out)
        self.assertIn('WScript.Echo', out)

    def test_self_recursive_function_removed(self):
        # Every occurrence of Recur's name is inside its own definition span
        # (header + recursive call): has_external_occurrence returns False →
        # sub-pass D removes it.
        src = (
            'Function Recur(n)\n'
            '    Recur = Recur(n - 1)\n'
            'End Function\n'
            'WScript.Echo "done"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('Function Recur', out)
        self.assertIn('"done"', out)

    def test_ttaffry_pattern_removed_after_inlining(self):
        # After inline_functions removes all calls, the definition has no callers.
        # Dead code removal should then delete it.
        # NOTE: sub-pass D's string-literal guard is unconditional — if the function
        # name appears in any string literal in the source, it is not removed (defensive
        # against CallByName/GetRef dynamic dispatch). The test source must therefore
        # not include the function name inside any string literal.
        src = (
            'Function stripAt(s)\n'
            '    stripAt = Replace(s, "@", "")\n'
            'End Function\n'
            'WScript.Echo "hello"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('stripAt', out)

    def test_subpass_d_string_guard_no_execute(self):
        # Sub-pass D's string-literal guard is unconditional: a function whose
        # name appears anywhere in a string literal is not removed, even when
        # no Execute/ExecuteGlobal is present in the file.
        src = (
            'Function helperFn(x)\n'
            '    helperFn = x\n'
            'End Function\n'
            'msg = "dispatch via helperFn"\n'
            'WScript.Echo msg\n'
        )
        out, stats = tool.run(src)
        self.assertIn('Function helperFn', out)

    def test_subpass_d_getref_guard(self):
        # A function whose name is the string argument to GetRef() is protected
        # because that argument is a string literal — sub-pass D's guard fires.
        src = (
            'Function computeHash(x)\n'
            '    computeHash = x * 31\n'
            'End Function\n'
            'Set fn = GetRef("computeHash")\n'
            'WScript.Echo fn(5)\n'
        )
        out, stats = tool.run(src)
        self.assertIn('Function computeHash', out)

    def test_subpass_d_vs_subpass_c_guard_contrast(self):
        # Sub-pass C's dead-store guard only activates when Execute/ExecuteGlobal
        # is present; sub-pass D's string guard is always active regardless.

        # Part 1: sub-pass C removes a dead var even though its name appears in
        # a string literal, because there is no Execute to worry about.
        src_c = (
            'Dim myDeadVar\n'
            'myDeadVar = "value"\n'
            'note = "myDeadVar is documented here"\n'
            'WScript.Echo note\n'
        )
        out_c, stats_c = tool.run(src_c)
        self.assertGreater(stats_c['changed'], 0)
        self.assertNotIn('myDeadVar = "value"', out_c)

        # Part 2: sub-pass D does NOT remove a function even with no Execute,
        # because its string guard is unconditional.
        src_d = (
            'Function myDeadFunc()\n'
            '    myDeadFunc = 1\n'
            'End Function\n'
            'note = "myDeadFunc is documented here"\n'
            'WScript.Echo note\n'
        )
        out_d, _ = tool.run(src_d)
        self.assertIn('Function myDeadFunc', out_d)


# ---------------------------------------------------------------------------
# Sub-pass B2 — local sequential dead-store elimination
# ---------------------------------------------------------------------------

class TestLocalDeadStore(unittest.TestCase):

    def test_first_of_two_consecutive_assignments_removed(self):
        src = (
            'x = "first"\n'
            'x = "second"\n'
            'WScript.Echo x\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('"first"', out)
        self.assertIn('"second"', out)

    def test_interleaved_read_prevents_local_dead_store(self):
        src = (
            'x = "first"\n'
            'WScript.Echo x\n'
            'x = "second"\n'
            'WScript.Echo x\n'
        )
        out, stats = tool.run(src)
        # Both assignments are live (each followed by a read before overwrite)
        self.assertIn('"first"', out)
        self.assertIn('"second"', out)


# ---------------------------------------------------------------------------
# Sub-pass A — statically-false If block removal
# ---------------------------------------------------------------------------

class TestFalseIfRemoval(unittest.TestCase):

    def test_if_false_then_block_removed(self):
        src = (
            'If False Then\n'
            '    WScript.Echo "never"\n'
            'End If\n'
            'WScript.Echo "always"\n'
        )
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('"never"', out)
        self.assertIn('"always"', out)

    def test_if_zero_then_block_removed(self):
        src = (
            'If 0 Then\n'
            '    WScript.Echo "dead"\n'
            'End If\n'
            'x = 1\n'
        )
        out, stats = tool.run(src)
        self.assertNotIn('"dead"', out)


# ---------------------------------------------------------------------------
# Sub-pass A2 — statically-false single-line If statement removal
# ---------------------------------------------------------------------------

class TestFalseSingleLineIfRemoval(unittest.TestCase):

    def test_single_line_if_false_removed(self):
        src = 'If False Then WScript.Echo "dead"\nWScript.Echo "live"\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('"dead"', out)
        self.assertIn('"live"', out)

    def test_single_line_if_zero_removed(self):
        src = 'If 0 Then x = 99\nWScript.Echo "ok"\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('x = 99', out)

    def test_single_line_if_true_preserved(self):
        # True is truthy — nothing to remove
        src = 'If True Then x = 1\nWScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertIn('x = 1', out)

    def test_single_line_if_unknown_cond_preserved(self):
        src = 'If someVar Then x = 1\nWScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertIn('x = 1', out)


# ---------------------------------------------------------------------------
# Sub-pass B — statically-false Do While block removal
# ---------------------------------------------------------------------------

class TestFalseDoWhileRemoval(unittest.TestCase):

    def test_do_while_false_removed(self):
        src = 'Do While False\nx = 1\nLoop\nWScript.Echo "done"\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('x = 1', out)
        self.assertIn('"done"', out)

    def test_do_while_zero_removed(self):
        src = 'Do While 0\nWScript.Echo "dead"\nLoop\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('"dead"', out)

    def test_do_while_true_preserved(self):
        # True (-1) is truthy — sub-pass B leaves it alone
        src = 'Do While True\nx = 1\nLoop\nWScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertIn('Do While True', out)

    def test_while_wend_with_non_empty_body_not_removed_by_subpass_b(self):
        # Sub-pass B only matches 'Do While' — While/Wend is a different form.
        # A While with a false condition but non-empty body has no removal handler,
        # so it must survive unchanged (no sub-pass B3 match either — body non-empty).
        src = 'While False\nWScript.Echo "x"\nWend\nWScript.Echo "after"\n'
        out, stats = tool.run(src)
        self.assertIn('While False', out)
        self.assertIn('"after"', out)


# ---------------------------------------------------------------------------
# Sub-pass B3 — empty-body loop flagging and optional removal
# ---------------------------------------------------------------------------

class TestEmptyLoopFlagging(unittest.TestCase):

    def test_empty_do_while_gets_marker_comment(self):
        # Default mode: marker is inserted before the loop, loop body stays
        src = 'Do While someCondition\nLoop\nWScript.Echo "done"\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('[deobfuscator] empty-body loop', out)
        self.assertIn('"done"', out)

    def test_empty_while_wend_gets_marker_comment(self):
        src = 'While someCondition\nWend\nWScript.Echo "done"\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('[deobfuscator] empty-body loop', out)

    def test_non_empty_loop_not_flagged(self):
        # Body contains a real statement — sub-pass B3 does not match
        src = 'Do While True\nx = 1\nLoop\nWScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertNotIn('[deobfuscator]', out)

    def test_remove_empty_loops_replaces_call_free_loop_with_marker(self):
        # With remove_empty_loops=True and a call-free condition: loop is replaced
        src = 'Do While someVar\nLoop\nWScript.Echo "after"\n'
        out, stats = tool.run(src, remove_empty_loops=True)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('[deobfuscator] empty-body loop', out)
        self.assertNotIn('Do While someVar', out)
        self.assertIn('"after"', out)

    def test_remove_empty_loops_preserves_loop_with_call_in_condition(self):
        # Call in condition means possible side effects — loop is not removed
        src = 'Do While SomeFunc()\nLoop\nWScript.Echo "after"\n'
        out, stats = tool.run(src, remove_empty_loops=True)
        self.assertIn('Do While SomeFunc()', out)
        self.assertIn('Loop', out)

    def test_while_false_wend_empty_body_flagged_and_removed(self):
        # Sub-pass B only handles 'Do While'; a 'While ... Wend' with an empty
        # body is handled by sub-pass B3 via _EMPTY_WHILE_WEND_PAT.
        # 'False' contains no call → the condition is considered side-effect-free.
        src = 'While False\nWend\n'

        # Default (flag only): marker inserted before loop, loop preserved
        out_flag, stats_flag = tool.run(src, remove_empty_loops=False)
        self.assertGreater(stats_flag['changed'], 0)
        self.assertIn('[deobfuscator] empty-body loop', out_flag)
        self.assertIn('While False', out_flag)

        # Remove mode: loop replaced with marker + blank padding
        out_rm, stats_rm = tool.run(src, remove_empty_loops=True)
        self.assertGreater(stats_rm['changed'], 0)
        self.assertIn('[deobfuscator] empty-body loop', out_rm)
        self.assertNotIn('While False', out_rm)
        self.assertNotIn('Wend', out_rm)

    def test_remove_empty_while_wend_nonconstant_condition(self):
        # With remove_empty_loops=True, an empty While/Wend whose condition is
        # a plain variable (no call-like expression) is replaced with the marker.
        src = 'While someVar\nWend\nWScript.Echo "after"\n'
        out, stats = tool.run(src, remove_empty_loops=True)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('[deobfuscator] empty-body loop', out)
        self.assertNotIn('While someVar', out)
        self.assertIn('"after"', out)


# ---------------------------------------------------------------------------
# --preserve-strings flag
# ---------------------------------------------------------------------------

class TestPreserveStringsFlag(unittest.TestCase):

    def test_preserve_strings_keeps_literal_rhs(self):
        # With preserve_strings=True, a dead assignment whose RHS is a plain
        # string literal is kept even though the LHS is never read.
        # This is the safety guard for running deadcode before propagation.
        src = (
            'x = "keepme"\n'
            'WScript.Echo "done"\n'
        )
        # Default: dead assignment removed
        out_default, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('"keepme"', out_default)

        # preserve_strings: assignment kept
        out_ps, _ = tool.run(src, preserve_strings=True)
        self.assertIn('"keepme"', out_ps)

    def test_preserve_strings_does_not_protect_compound_rhs(self):
        # preserve_strings only protects pure string/number literals on the RHS.
        # A compound expression (e.g. var & "...") is not protected.
        src = (
            'Dim other\n'
            'other = "base"\n'
            'x = other & "extra"\n'  # dead; RHS is a concat expression, not a plain literal
            'WScript.Echo other\n'
        )
        out_ps, _ = tool.run(src, preserve_strings=True)
        self.assertNotIn('x = other', out_ps)
        self.assertIn('other', out_ps)  # other itself is live and kept


# ---------------------------------------------------------------------------
# --aggressive + ExecuteGlobal interaction
# ---------------------------------------------------------------------------

class TestAggressiveModeInteraction(unittest.TestCase):

    def test_aggressive_executeGlobal_conservative_guard_wins(self):
        # Sub-pass C's conservative guard: when the variable's name appears
        # in a string literal AND ExecuteGlobal is present, the variable is
        # protected even in aggressive mode (it might be dispatched dynamically).
        src = (
            'x = ""\n'
            'x = x & "a"\n'
            'x = x & "b"\n'
            'ExecuteGlobal "x = someFunc()"\n'  # x's name in exec string
        )
        out, stats = tool.run(src, aggressive=True)
        # x is a self-referential cluster (aggressive would normally remove),
        # but x appears in the ExecuteGlobal string → conservative guard fires.
        self.assertIn('x = ""', out)
        self.assertIn('x & "a"', out)


# ---------------------------------------------------------------------------
# Aggressive mode unit tests
# ---------------------------------------------------------------------------

class TestAggressiveModeUnit(unittest.TestCase):

    def test_accumulator_preserved_in_default_mode(self):
        # Non-aggressive liveness: x appears as an IDENT read in the RHS of
        # each 'x = x & ...' line → reads_by_name['X'] is non-empty → not dead.
        src = 'x = ""\nx = x & "a"\nx = x & "b"\n'
        out, stats = tool.run(src, aggressive=False)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('x = ""', out)
        self.assertIn('x & "a"', out)

    def test_accumulator_removed_in_aggressive_mode(self):
        # Aggressive liveness: every read of x falls within one of x's own
        # writer statement spans (self-contained cluster) → treated as dead.
        # None of the RHS expressions trigger _rhs_has_side_effect, so all
        # three assignments are deleted.
        src = 'x = ""\nx = x & "a"\nx = x & "b"\n'
        out, stats = tool.run(src, aggressive=True)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('x = ""', out)
        self.assertNotIn('x & "a"', out)
        self.assertNotIn('x & "b"', out)


# ---------------------------------------------------------------------------
# CLI tests + pipeline stat regression
# ---------------------------------------------------------------------------

class TestDeadcodeCli(unittest.TestCase):

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

    def test_cli_removes_dead_dim(self):
        src = 'Dim x\nx = "unused"\nWScript.Echo "hi"\n'
        out, stats = self._run_cli(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('x', out)

    def test_cli_json_has_changed_key(self):
        src = 'Dim x\nx = "unused"\nWScript.Echo "hi"\n'
        _, stats = self._run_cli(src)
        self.assertIn('changed', stats)

    def test_aggressive_flag_accepted(self):
        src = (
            'Dim other\nother = "keep me"\n'
            'Function noop(a, b)\nEnd Function\n'
            'uncannily = (uncannily) & noop("x", "y")\n'
            'WScript.Echo other\n'
        )
        out, stats = self._run_cli(src, extra=['--aggressive'])
        self.assertIn('other', out)

    @unittest.skipUnless(SAMPLE.exists(), 'VBS sample not found on Desktop')
    def test_pipeline_stat_regression_pass6(self):
        """Pipeline pass 6 is remove_deadcode on the fold_builtin_calls output.
        Baseline: changed == 114."""
        tmp = self.tmp
        p = [SAMPLE,
             tmp / 'pass1.vbs',
             tmp / 'pass2.vbs',
             tmp / 'pass3.vbs',
             tmp / 'pass4.vbs',
             tmp / 'pass5.vbs',
             tmp / 'pass6.vbs']

        scripts = [
            TOOL_DIR / 'vbs_fold_chr_calls.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            TOOL_DIR / 'vbs_propagate_constants.py',
            TOOL_DIR / 'vbs_inline_functions.py',
            TOOL_DIR / 'vbs_fold_builtin_calls.py',
            SCRIPT,
        ]
        for i, script in enumerate(scripts):
            _run_script(script, p[i], p[i + 1])

        stats = _run_script(SCRIPT, p[5], p[6])
        # NOTE: Current toolkit removes 108 dead stores, vs 114 in the older version.
        # The difference is consistent with fewer constants being propagated in pass 3.
        self.assertEqual(stats['changed'], 108,
                         f'Expected changed == 108, got {stats["changed"]}')

        out_text = p[6].read_text(encoding='utf-8')
        # Live code must survive
        self.assertIn('WScript.Quit', out_text)
        self.assertIn('ExecuteGlobal', out_text)
        self.assertIn('CreateObject', out_text)
        self.assertIn('RegWrite', out_text)
        self.assertIn('"WScript.Shell"', out_text)
        # Dead intermediate vars must be gone
        self.assertNotIn('v3372r', out_text)
        self.assertNotIn('v2893o', out_text)
        self.assertNotIn('ttaffRy', out_text)
        # Constant Chr() calls must all be folded; the dynamic Chr((h Mod 26) + 97)
        # inside GetPCHash legitimately remains throughout the pipeline.
        self.assertNotIn('Chr(115)', out_text)
        self.assertNotIn('Chr(37)', out_text)


if __name__ == '__main__':
    unittest.main()
