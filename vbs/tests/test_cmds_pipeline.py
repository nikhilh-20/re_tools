"""Synthetic 11-pass regression mirroring the cmds.txt pipeline order.

No real file paths — everything runs on an inline VBS source string.
All five tools from cmds.txt are exercised in their exact sequence:

  pass 1  : vbs_remove_deadcode --aggressive
  pass 2  : vbs_propagate_constants
  pass 3  : vbs_fold_concat
  pass 4  : vbs_remove_deadcode
  pass 5  : vbs_fold_split_calls
  pass 6  : vbs_fold_array_join_loops
  pass 7  : vbs_remove_deadcode
  pass 8  : vbs_propagate_constants
  pass 9  : vbs_fold_split_calls
  pass 10 : vbs_fold_array_join_loops
  pass 11 : vbs_remove_deadcode

Synthetic source design:
  - A dead self-referential accumulator (aggressively removed in pass 1)
  - A live self-append accumulator collapsed by pass 2 (propagate uses it
    as the Split arg in an assignment, so substitution fires)
  - Two concat variables whose concat is written to a third variable via
    assignment — NOT a call arg — so pass 2 substitutes them and pass 3 folds
  - A Split() call whose arg was propagated in pass 2 (folded pass 5)
  - A flow-sensitivity check: the Split() arg variable is REASSIGNED to a
    different constant immediately after the Split() call, and a second
    variable reads it post-reassignment — pass 2 must substitute the
    Split() call with the PRE-reassignment value and the second read with
    the POST-reassignment value, not conflate the two (mirrors a real
    obfuscated sample where a seed string is Split() on a placeholder
    character and then overwritten in place under the same name)
  - A For/UBound loop over the Array result (folded pass 6)
  - A second accumulator + For/UBound loop (folded passes 6/7)
  - A combined assignment referencing both loop results — substituted by pass 8
    (which is the second propagate pass), producing dead inputs for pass 11

Key ByRef-kill invariant: a call-statement argument that is a BARE identifier
(a whole argument by itself, or an array-element base) is killed, not
substituted, by the propagate tool — it may be a ByRef target. An identifier
that is merely an operand inside a larger expression argument can never be a
ByRef target and IS substituted. For substitutions to fire in this synthetic
source, all reads of constants must appear in assignment RHS positions or as
expression operands, not as bare call args.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOL_DIR  = Path(__file__).resolve().parent.parent
DEADCODE  = TOOL_DIR / 'vbs_remove_deadcode.py'
PROPAGATE = TOOL_DIR / 'vbs_propagate_constants.py'
CONCAT    = TOOL_DIR / 'vbs_fold_concat.py'
SPLIT     = TOOL_DIR / 'vbs_fold_split_calls.py'
JOIN      = TOOL_DIR / 'vbs_fold_array_join_loops.py'

_STEPS = [
    (DEADCODE,  ['--aggressive']),  # pass 1
    (PROPAGATE, []),                 # pass 2
    (CONCAT,    []),                 # pass 3
    (DEADCODE,  []),                 # pass 4
    (SPLIT,     []),                 # pass 5
    (JOIN,      []),                 # pass 6
    (DEADCODE,  []),                 # pass 7
    (PROPAGATE, []),                 # pass 8
    (SPLIT,     []),                 # pass 9
    (JOIN,      []),                 # pass 10
    (DEADCODE,  []),                 # pass 11
]

# ---------------------------------------------------------------------------
# Synthetic VBS source
# Design rule: variables read as a BARE WScript.Echo argument are KILLED
# (possible ByRef) rather than substituted. All propagation targets therefore
# use assignment RHS reads.
# ---------------------------------------------------------------------------
_SOURCE = """\
Dim deadAccum
deadAccum = ""
deadAccum = deadAccum & "junk1"
deadAccum = deadAccum & "junk2"

payload = ""
payload = payload & "part1"
payload = payload & ","
payload = payload & "part2"

Dim deadInter
deadInter = "unused_intermediate"

concatA = "foo"
concatB = "bar"
merged = concatA & concatB

arr = Split(payload, ",")
payload = "reassigned_after_split"
lateEcho = payload
result = ""
For i = 0 To UBound(arr)
    result = result & arr(i)
Next

payload2 = ""
payload2 = payload2 & "hello"

arr2 = Split(payload2, " ")
accum2 = ""
For j = 0 To UBound(arr2)
    accum2 = accum2 & arr2(j)
Next

combined = result & accum2
WScript.Echo merged
WScript.Echo combined
WScript.Echo lateEcho
"""


def _run_pass(script: Path, inp: Path, out: Path,
              extra: list | None = None) -> dict:
    cmd = [sys.executable, str(script),
           '--input', str(inp), '--output', str(out)]
    if extra:
        cmd.extend(extra)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class TestCmdsPipeline(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _run_all(self):
        p = [self.tmp / f'pass{i}.vbs' for i in range(len(_STEPS) + 1)]
        p[0].write_bytes(_SOURCE.encode('utf-8'))
        stats = []
        for i, (script, extra) in enumerate(_STEPS):
            stats.append(_run_pass(script, p[i], p[i + 1], extra))
        return stats, p

    # --- per-pass assertions ---

    def test_pass1_aggressive_removes_dead_accumulator(self):
        stats, p = self._run_all()
        self.assertGreater(stats[0]['changed'], 0)
        after = p[1].read_text(encoding='utf-8')
        self.assertNotIn('deadAccum', after)

    def test_pass1_removes_dead_intermediate_variable(self):
        _, p = self._run_all()
        after = p[1].read_text(encoding='utf-8')
        self.assertNotIn('deadInter', after)

    def test_pass2_propagate_collapses_live_accumulator(self):
        stats, p = self._run_all()
        self.assertGreater(stats[1]['changed'], 0)
        after = p[2].read_text(encoding='utf-8')
        self.assertIn('"part1,part2"', after)

    def test_pass2_substitutes_payload_into_split_arg(self):
        _, p = self._run_all()
        after = p[2].read_text(encoding='utf-8')
        self.assertIn('Split("part1,part2", ",")', after)

    def test_pass2_split_arg_uses_pre_reassignment_value(self):
        # `payload` is reassigned to "reassigned_after_split" immediately
        # after `arr = Split(payload, ",")` — the Split() call must still
        # resolve to the PRE-reassignment value ("part1,part2"), proving
        # pass 2 is flow-sensitive rather than using payload's final value.
        _, p = self._run_all()
        after = p[2].read_text(encoding='utf-8')
        self.assertIn('Split("part1,part2", ",")', after)
        self.assertNotIn('Split("reassigned_after_split"', after)

    def test_pass2_late_read_uses_post_reassignment_value(self):
        # `lateEcho = payload` occurs AFTER the reassignment, so it must
        # pick up "reassigned_after_split", not the original chain value.
        _, p = self._run_all()
        after = p[2].read_text(encoding='utf-8')
        self.assertIn('lateEcho = "reassigned_after_split"', after)

    def test_pass2_substitutes_variables_into_concat_assignment(self):
        # concatA and concatB are substituted into `merged = concatA & concatB`
        # (an assignment RHS, not a call argument — so ByRef-kill does not fire).
        _, p = self._run_all()
        after = p[2].read_text(encoding='utf-8')
        self.assertIn('"foo" & "bar"', after)

    def test_pass3_folds_concat_chain_in_assignment(self):
        # Pass 3 (fold_concat) folds `merged = "foo" & "bar"` → `merged = "foobar"`.
        stats, p = self._run_all()
        self.assertGreater(stats[2]['changed'], 0)
        after = p[3].read_text(encoding='utf-8')
        self.assertIn('"foobar"', after)

    def test_pass4_removes_dead_stores_after_propagation(self):
        # payload, concatA, concatB, payload2 all had their reads substituted
        # in pass 2 → they are dead after pass 2 → removed in pass 4.
        stats, _ = self._run_all()
        self.assertGreater(stats[3]['changed'], 0)

    def test_pass5_folds_split_call_to_array(self):
        stats, p = self._run_all()
        self.assertGreater(stats[4]['changed'], 0)
        after = p[5].read_text(encoding='utf-8')
        self.assertIn('Array("part1", "part2")', after)
        self.assertNotIn('Split(', after)

    def test_pass6_folds_array_join_loop_to_literal(self):
        stats, p = self._run_all()
        self.assertGreater(stats[5]['changed'], 0)
        after = p[6].read_text(encoding='utf-8')
        self.assertIn('"part1part2"', after)
        self.assertNotIn('For i', after)

    def test_pass7_removes_now_dead_array_vars(self):
        stats, _ = self._run_all()
        self.assertGreater(stats[6]['changed'], 0)

    def test_pass8_substitutes_loop_results_into_combined_assignment(self):
        # After pass 6, `result = "part1part2"` and `accum2 = "hello"` are plain
        # assignments.  Pass 8 (second propagate) substitutes them into the
        # assignment `combined = result & accum2` → `combined = "part1part2" & "hello"`.
        stats, p = self._run_all()
        self.assertGreater(stats[7]['changed'], 0)
        after = p[8].read_text(encoding='utf-8')
        self.assertIn('"part1part2"', after)

    def test_pass11_removes_dead_assignments_after_second_propagate(self):
        # result and accum2 had their reads substituted in pass 8 → dead → removed.
        stats, _ = self._run_all()
        self.assertGreater(stats[10]['changed'], 0)

    # --- final output assertions ---

    def test_final_no_split_calls(self):
        _, p = self._run_all()
        final = p[-1].read_text(encoding='utf-8')
        self.assertNotIn('Split(', final)

    def test_final_no_for_loops(self):
        _, p = self._run_all()
        final = p[-1].read_text(encoding='utf-8')
        self.assertNotIn('For i', final)
        self.assertNotIn('For j', final)

    def test_final_no_dead_accum_or_intermediate_vars(self):
        _, p = self._run_all()
        final = p[-1].read_text(encoding='utf-8')
        self.assertNotIn('deadAccum', final)
        self.assertNotIn('deadInter', final)

    def test_final_correct_joined_literals_in_output(self):
        # The two folded string values appear in the final output:
        # "foobar" in the merged assignment, "part1part2"/"hello" in combined.
        _, p = self._run_all()
        final = p[-1].read_text(encoding='utf-8')
        self.assertIn('"foobar"', final)
        self.assertIn('"part1part2"', final)
        self.assertIn('"hello"', final)

    def test_final_wscript_echo_preserved(self):
        _, p = self._run_all()
        final = p[-1].read_text(encoding='utf-8')
        self.assertIn('WScript.Echo', final)

    def test_final_split_arg_reassignment_never_leaks_into_split_call(self):
        # End-to-end guard: at no point across the full 11-pass pipeline
        # should the post-reassignment value ("reassigned_after_split")
        # appear as the Split() argument — it must stay confined to the
        # later read (lateEcho).
        _, p = self._run_all()
        for step in p:
            text = step.read_text(encoding='utf-8')
            self.assertNotIn('Split("reassigned_after_split"', text)


if __name__ == '__main__':
    unittest.main()
