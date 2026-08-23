"""Unit and end-to-end tests for vbs_propagate_constants.py.

Covers the bugs found while investigating why a real obfuscated sample's
1840-link self-append accumulator (`v7773 = v7773 & "chunk"`, repeated) was
not folding *at all* — verified as a whole-file no-op (`run()` returned
`{'changed': 0}`, byte-identical output), not a partial fold:

  1. The file used `\\r\\r\\n` line endings. The tokenizer's NEWLINE pattern
     (`\\r?\\n`) never matched a bare `\\r`, so every line leaked a stray
     UNKNOWN token into the following statement's rhs tokens, which made
     resolve_const reject *every* expression in the file as having
     "leftover tokens" — nothing folded anywhere, silently. See
     tests/test_tokenizer_newlines.py for the tokenizer-level fix; the
     `test_crcrlf_*` cases here are the propagation-level regression proof.
  2. `f v,v`-style calls (VBScript's default ByRef argument passing) were
     substituted at the call site but never killed afterward, so a later
     read of the same variable folded to its stale *pre-call* value instead
     of being left alone — a wrong answer, not just a missed fold.
  3. The real chain's *seed statement alone* already resolves to 8468 chars
     — over `_MAX_TRACKED_STRING_LEN` (8192) — so the old design killed the
     variable before a single append link was ever seen: zero edits across
     all 1840 links. The cap existed because `_substitute` re-embeds a
     tracked variable's *entire current value* as the replacement text at
     every read, so folding a chain link-by-link made total edit bytes
     O(chain_length^2) (a naive per-link fold of this sample's chain would
     emit ~682 MB of replacement text). `vbs_propagate_constants.py` now
     detects a maximal run of self-append links to one variable and
     collapses the *whole run* into a single edit — `X = "<final literal>"`
     — so cost is O(final value length) once, independent of chain length.
     `_MAX_TRACKED_STRING_LEN` still governs whether a collapsed value is
     worth keeping in `env` for downstream reads elsewhere, and still fully
     applies to the fallback per-statement path and to in-block chains
     (deliberately out of scope for collapsing — see
     `_apply_inblock_assignment`). A separate absolute bound,
     `_MAX_COLLAPSED_VALUE_LEN`, guards the run-collapse scan itself against
     a self-squaring chain (`x = x & x`, which doubles every link
     regardless of chain length).

     This also required fixing a real quadratic/memory landmine in the
     shared tokenizer (vbsdeoblib/tokenizer.py): its STRING regex,
     `"(?:[^"]|"")*"`, forces the engine to choose between two alternatives
     at every character of the string body, which measured as O(n^2) — a
     32 MB single-line string literal took ~17s to tokenize and a 64 MB one
     raised MemoryError outright. A collapsed chain's final literal gets
     written back into the source and re-tokenized on every subsequent
     pass, so this landmine sat directly in the fix's path (not just this
     sample's — any tool tokenizing any sufficiently large string literal
     hit it). Rewriting the pattern as `"[^"]*(?:""[^"]*)*"` (runs of
     non-quote characters separated by literal "" escapes, matching
     identically on every case tested) fixed it for the whole toolkit: the
     same 64 MB literal now tokenizes in well under a tenth of a second.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import vbs_propagate_constants as M

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = TOOL_DIR / 'vbs_propagate_constants.py'

# Minimal character-shift decoder matching the real sample's pattern:
# Sub f(je, tv) reads je and writes the decoded string back through tv
# (ByRef by VBScript's default argument-passing convention).
DECODER = (
    'Sub f(je,tv)\r\n'
    'Dim r,h,jq,cs,dd\r\n'
    'jq = &H700 - 32\r\n'
    'r = ""\r\n'
    'For h = 1 To Len(je)\r\n'
    'cs = Mid(je,h,1)\r\n'
    'dd = AscW(cs) - jq\r\n'
    'r = r & ChrW(dd)\r\n'
    'Next\r\n'
    'tv = r\r\n'
    'End Sub\r\n'
)


def _run(src: str) -> tuple[str, dict]:
    return M.run(src)


class TestBasicPropagation(unittest.TestCase):
    def test_simple_constant_read(self):
        out, stats = _run('x = 5\r\ny = x\r\n')
        self.assertEqual(out, 'x = 5\r\ny = 5\r\n')
        self.assertEqual(stats['changed'], 1)


class TestSelfAppendAccumulator(unittest.TestCase):
    def test_single_chunk_fully_collapses(self):
        """A resolved self-append run collapses to its final value in one
        edit — even a single-link run, since the collapse rule has no
        chain-length threshold (see _MIN_APPEND_LINKS_TO_COLLAPSE)."""
        out, stats = _run('x = ""\r\nx = x & "hello"\r\n')
        self.assertEqual(out, 'x = "hello"\r\n')
        self.assertEqual(stats['changed'], 1)

    def test_multi_chunk_fully_collapses_in_one_edit(self):
        src = 'x = ""\r\nx = x & "a"\r\nx = x & "b"\r\nx = x & "c"\r\n'
        out, stats = _run(src)
        self.assertEqual(out, 'x = "abc"\r\n')
        # the whole 4-statement run replaces as ONE edit, not one per link.
        self.assertEqual(stats['changed'], 1)

    def test_read_immediately_after_collapsed_run(self):
        out, stats = _run('x = ""\r\nx = x & "a"\r\nx = x & "b"\r\ny = x\r\n')
        self.assertEqual(out, 'x = "ab"\r\ny = "ab"\r\n')

    def test_decoy_seed_chain_still_collapses(self):
        """The decoy-seed idiom (acc = neverBoundName, defeating a naive
        first-assignment heuristic — see _resolve_bare_undeclared) still
        collapses correctly when followed by real append links."""
        src = ('acc = neverBoundName\r\n'
               'acc = acc & "b"\r\n'
               'acc = acc & "c"\r\n'
               'y = acc\r\n')
        out, stats = _run(src)
        self.assertEqual(out, 'acc = "bc"\r\ny = "bc"\r\n')

    def test_edits_count_does_not_scale_with_chain_length(self):
        """Direct proof the O(chain_length^2) edit-size bug is fixed: a
        500-link chain must cost the same one collapsed edit as a 50-link
        chain, not scale with link count."""
        def build(n):
            return 'x = ""\r\n' + 'x = x & "z"\r\n' * n
        _, stats_50 = _run(build(50))
        _, stats_500 = _run(build(500))
        self.assertEqual(stats_50['changed'], 1)
        self.assertEqual(stats_500['changed'], 1)

    def test_self_squaring_chain_is_growth_guarded(self):
        """x = x & x doubles the accumulator every link regardless of chain
        length — a flat final-size cap alone can't distinguish this from a
        legitimate linear chain once the flat cap stops being the
        determining factor for clean runs. _MAX_COLLAPSED_VALUE_LEN bounds
        it: exponential growth crosses any bound within a handful of
        doublings, so the scan stops almost immediately (and must not hang
        or exhaust memory doing so)."""
        src = 'x = "a"\r\n' + 'x = x & x\r\n' * 40
        t0 = time.time()
        out, stats = _run(src)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5, 'growth guard should stop the scan almost immediately')
        self.assertLess(len(out), 100 * 1024 * 1024)
        self.assertEqual(stats['changed'], 1)
        # links beyond the guarded prefix are left as bare, unfolded chain
        # statements (the fallback per-statement path, same as any other
        # run that stops partway).
        self.assertIn('x = x & x', out)


class TestCrCrLfRegression(unittest.TestCase):
    """The exact defect that hid v7773's chain: \\r\\r\\n line endings must
    not prevent folding."""

    def test_self_append_chain_folds_with_crcrlf_endings(self):
        src = 'x = ""\r\r\nx = x & "a"\r\r\nx = x & "b"\r\r\n'
        out, stats = _run(src)
        self.assertEqual(out, 'x = "ab"\r\r\n')
        self.assertEqual(stats['changed'], 1)

    def test_basic_propagation_unaffected_by_crcrlf(self):
        out, stats = _run('x = 5\r\r\ny = x\r\r\n')
        self.assertGreater(stats['changed'], 0)
        self.assertIn('y = 5', out)

    def test_longer_crcrlf_chain_still_fully_collapses(self):
        """A \\r\\r\\n line ending makes split_statements emit extra
        code-less spans between every pair of real statements (see
        _next_code_stmt) — this is the actual shape of the real sample's
        chain. A run several links long must still collapse into one edit,
        not just the 2-link case above."""
        src = 'x = ""\r\r\n' + ''.join(f'x = x & "{c}"\r\r\n' for c in 'abcde')
        out, stats = _run(src)
        self.assertEqual(out, 'x = "abcde"\r\r\n')
        self.assertEqual(stats['changed'], 1)


class TestByRefCallKill(unittest.TestCase):
    """VBScript Sub/Function arguments are ByRef by default: a variable
    passed into a call may be overwritten by the callee, so folding its
    pre-call value into a later read would be wrong."""

    def test_read_after_byref_call_is_not_folded(self):
        src = DECODER + (
            'Dim v\r\n'
            'v = ""\r\n'
            'v = v & "ENCODED"\r\n'
            'f v,v\r\n'
            'WScript.Echo v\r\n'
        )
        out, _ = _run(src)
        # The chain itself still folds — now into one collapsed literal...
        self.assertIn('v = "ENCODED"', out)
        # ...but the call site and the read after it must keep the bare
        # identifier: substituting either would assume f() didn't rewrite v.
        self.assertIn('f v,v', out)
        self.assertIn('WScript.Echo v', out)
        self.assertNotIn('"ENCODED"', out.split('WScript.Echo')[-1])

    def test_stable_across_multiple_passes(self):
        """Regression guard for a design pitfall: killing a call argument
        but still substituting its text at the call site loses the kill
        signal on the *next* pass (re-tokenizing the now-literal call site
        finds no identifier left to re-derive the kill from), letting the
        stale value leak back in. Running run() (which loops passes until
        a fixed point) must not regress once that fixed point is reached."""
        src = DECODER + (
            'Dim v\r\n'
            'v = ""\r\n'
            'v = v & "ENCODED"\r\n'
            'f v,v\r\n'
            'WScript.Echo v\r\n'
        )
        out1, _ = _run(src)
        out2, stats2 = _run(out1)
        self.assertEqual(out1, out2)
        self.assertEqual(stats2['changed'], 0)
        self.assertIn('WScript.Echo v', out2)


class TestCallStmtPositionClassification(unittest.TestCase):
    """VBScript can only bind a procedure argument ByRef when the argument
    expression IS a bare variable (or array-element) reference — the callee
    needs an address, which only exists for a name the caller already has
    storage for. An identifier that is merely an operand inside a larger
    expression argument is evaluated to a value before the call and can
    never be written back through, so it is safe to substitute (unlike the
    bare-argument case covered by TestByRefCallKill)."""

    def test_expression_operand_is_folded_and_stays_live(self):
        src = ('a = "foo"\r\n'
               'b = "bar"\r\n'
               'WScript.Echo a & b\r\n'
               'c = a\r\n')
        out, stats = _run(src)
        self.assertIn('WScript.Echo "foo" & "bar"', out)
        # a was never a ByRef target here (it's an operand of &, not a bare
        # argument), so a later read of it still folds too.
        self.assertIn('c = "foo"', out)
        self.assertGreater(stats['changed'], 0)

    def test_bare_arg_beside_expression_arg_only_bare_one_killed(self):
        src = ('a = "A"\r\n'
               'b = "B"\r\n'
               'c = "C"\r\n'
               'obj.Method a, b & c\r\n'
               'd = a\r\n')
        out, _ = _run(src)
        # a is a bare whole argument -> possible ByRef -> left untouched and
        # killed (later read of a stays bare).
        self.assertIn('obj.Method a,', out)
        self.assertIn('d = a', out)
        # b and c are operands of & -> substituted.
        self.assertIn('"B" & "C"', out)

    def test_array_element_arg_kills_base_folds_subscript(self):
        src = ('i = 2\r\n'
               'Foo arr(i)\r\n')
        out, _ = _run(src)
        # The subscript i is a plain read (evaluated before the call);
        # the array base `arr` might still be written back through by Foo,
        # so it must remain a bare identifier in the source.
        self.assertIn('Foo arr(2)', out)

    def test_call_with_parens_is_byref_bare_parens_is_byval(self):
        src_call = ('x = "a"\r\n'
                     'Call Foo(x)\r\n'
                     'y = x\r\n')
        out, _ = _run(src_call)
        # Call Foo(x): parens are the argument list -> x may be ByRef.
        self.assertIn('Call Foo(x)', out)
        self.assertIn('y = x', out)

        src_bare = ('x = "a"\r\n'
                     'Foo (x)\r\n')
        out2, _ = _run(src_bare)
        # Foo (x): the bare parens force ByVal evaluation of the expression
        # before the call -> no ByRef target is possible -> x folds.
        self.assertIn('Foo ("a")', out2)

    def test_element_assignment_folds_subscript_and_rhs(self):
        src = ('i = 2\r\n'
               'v = "z"\r\n'
               'arr(i) = v\r\n')
        out, _ = _run(src)
        self.assertIn('arr(2) = "z"', out)

    def test_member_name_never_substituted(self):
        # A tracked variable named the same as a property/method must never
        # be substituted into a `.member` position.
        src = ('Count = "5"\r\n'
               'obj.Count = 1\r\n'
               'y = Count\r\n')
        out, _ = _run(src)
        self.assertIn('obj.Count = 1', out)
        self.assertIn('y = "5"', out)

    def test_expression_operand_substitution_is_stable(self):
        """Same design pitfall as TestByRefCallKill.test_stable_across_multiple_passes,
        from the opposite direction: a position that IS substituted (and
        therefore never killed) must not need a second pass to settle."""
        src = ('a = "foo"\r\n'
               'WScript.Echo a & "b"\r\n'
               'c = a\r\n')
        out1, _ = _run(src)
        out2, stats2 = _run(out1)
        self.assertEqual(out1, out2)
        self.assertEqual(stats2['changed'], 0)


class TestAccumulatorCap(unittest.TestCase):
    """A collapsed self-append run's final value size never determines
    whether the *collapse itself* happens — that's unconditional (see
    test_long_well_behaved_chain_fully_collapses), nor whether the
    resolved value is worth *keeping in env* for separate downstream
    reads elsewhere in the file (see test_downstream_read_substitutes) —
    there is no per-value size cap at all any more. Growth is instead
    guarded structurally (_count_self_refs: >= 2 occurrences of the LHS in
    its own RHS means the value multiplies every evaluation and is refused
    regardless of size) plus one cumulative per-pass resource ceiling
    (_SubstitutionBudget, reusing _MAX_COLLAPSED_VALUE_LEN) for growth
    patterns structure can't see (a chain spread across distinct names).
    See TestSelfReferentialGrowthGuard for that part of the model."""

    def test_long_well_behaved_chain_fully_collapses(self):
        chunk = 'x' * 500
        n_chunks = 30  # 30*500 = 15000 chars, comfortably over the old cap
        src = 'Dim big\r\nbig = ""\r\n'
        src += ''.join(f'big = big & "{chunk}"\r\n' for _ in range(n_chunks))

        out, stats = _run(src)
        self.assertEqual(out, f'Dim big\r\nbig = "{chunk * n_chunks}"\r\n')
        # collapsing the whole run costs one edit, not one per link — this
        # is what keeps total edit size from scaling with chain length.
        self.assertEqual(stats['changed'], 1)

    def test_short_chain_unaffected_by_cap(self):
        """A short accumulator elsewhere in the same file must still fold
        fully even though a long chain lives in the same file."""
        chunk = 'x' * 500
        n_chunks = 30
        src = 'Dim big, short\r\nbig = ""\r\n'
        src += ''.join(f'big = big & "{chunk}"\r\n' for _ in range(n_chunks))
        src += 'short = ""\r\nshort = short & "hi"\r\ny = short\r\n'

        out, _ = _run(src)
        self.assertIn('y = "hi"', out)

    def test_downstream_read_substitutes(self):
        """A collapsed value with no per-value size cap left to trip: a
        further, separate read of the resolved value gets substituted
        regardless of the value's size, as long as the cumulative per-pass
        budget (far larger than this synthetic file could ever spend)
        isn't exhausted. This is the direct behavioral fix for the
        reported bug — a large one-shot/collapsed constant consumed a
        couple of times downstream must resolve, not stay permanently
        bare just because it's large."""
        chunk = 'x' * 500
        n_chunks = 30  # collapses to a 15000-char value
        src = 'big = ""\r\n'
        src += ''.join(f'big = big & "{chunk}"\r\n' for _ in range(n_chunks))
        src += 'y1 = big\r\ny2 = big\r\n'  # two separate downstream reads

        out, _ = _run(src)
        self.assertIn(f'big = "{chunk * n_chunks}"', out)
        self.assertIn(f'y1 = "{chunk * n_chunks}"', out)
        self.assertIn(f'y2 = "{chunk * n_chunks}"', out)


class TestSelfReferentialGrowthGuard(unittest.TestCase):
    """Growth is guarded structurally, not by size: _count_self_refs counts
    how many times an assignment's own LHS appears in its RHS. 0 or 1 can
    only ADD source-bounded text per evaluation (the legitimate
    accumulator shape); 2 or more MULTIPLIES the value every evaluation
    (x = x & x doubles), which compounds without bound regardless of
    magnitude, so it's refused unconditionally rather than judged by
    size — this is what replaced the old flat/size-relative caps for the
    genuinely pathological case."""

    def test_self_squaring_chain_is_not_tracked(self):
        """x = x & x has 2 occurrences of x in its own RHS — refused
        outright, not just capped once large. The chain must stay bounded
        and fast (the existing _MAX_COLLAPSED_VALUE_LEN scan guard still
        independently bounds the batch-collapse attempt itself; this test
        additionally confirms the remaining, un-collapsed links never
        resolve either, via the same 'not tracked' effect the old cap
        provided for this one specific case)."""
        src = 'x = "a"\r\n' + 'x = x & x\r\n' * 40
        t0 = time.time()
        out, stats = _run(src)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5)
        self.assertLess(len(out), 100 * 1024 * 1024)
        # links beyond whatever the scan guard collapsed stay bare
        self.assertIn('x = x & x', out)

    def test_single_self_reference_still_folds(self):
        """x = x & "b" has exactly 1 occurrence of x in its own RHS — the
        ordinary accumulator shape, which must keep folding normally (the
        >= 2 threshold must not over-tighten and block this)."""
        out, stats = _run('x = ""\r\nx = x & "b"\r\n')
        self.assertEqual(out, 'x = "b"\r\n')
        self.assertEqual(stats['changed'], 1)


class TestLargeSingleLiteralNotCapped(unittest.TestCase):
    """A plain (non-chain) top-level literal assignment over the old flat
    _MAX_TRACKED_STRING_LEN must still be tracked/substituted at a later
    read — there is no per-value size cap any more (see
    TestAccumulatorCap and TestSelfReferentialGrowthGuard for the
    structural-check-plus-cumulative-budget model that replaced it, applied
    uniformly to both the chain-collapse and plain-assignment paths).
    Mirrors the real-world idiom that motivated this fix: a large
    placeholder-padded blob assigned once, then consumed once by
    Split()."""

    def test_over_cap_literal_substituted_into_later_read(self):
        big = 'A' * (M._MAX_TRACKED_STRING_LEN + 500)
        out, stats = _run(f'x = "{big}"\r\ny = x\r\n')
        self.assertIn(f'y = "{big}"', out)
        self.assertGreater(stats['changed'], 0)

    def test_over_cap_literal_substituted_into_split_arg(self):
        # Mirrors the real pass6.vbs idiom: a large placeholder-padded
        # literal assigned once, then consumed by Split().
        chunk = 'PLACEHOLDER'
        big = chunk.join(['x'] * 2000)   # well over the 8192-char cap
        out, stats = _run(f'x = "{big}"\r\narr = Split(x, "{chunk}")\r\n')
        self.assertIn(f'Split("{big}", "{chunk}")', out)


class TestBlockDepthConservatism(unittest.TestCase):
    """Regression guards for existing conservative behavior — the new
    changes must not loosen these."""

    def test_assignment_inside_if_block_is_killed_not_folded(self):
        src = 'x = 5\r\nIf True Then\r\nx = 6\r\nEnd If\r\ny = x\r\n'
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('y = x', out)

    def test_straight_line_if_block_tracks_local_constant(self):
        src = 'If True Then\r\na = 5\r\nb = a\r\nEnd If\r\n'
        out, stats = _run(src)
        self.assertEqual(out, 'If True Then\r\na = 5\r\nb = 5\r\nEnd If\r\n')
        self.assertEqual(stats['changed'], 1)

    def test_loop_body_prekill_stops_read_before_write_fold(self):
        src = 'x = 1\r\nFor i = 1 To 3\r\ny = x\r\nx = 2\r\nNext\r\n'
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('y = x', out)

    def test_self_append_chain_inside_loop_is_not_collapsed(self):
        """Run-collapsing is only wired into the top-level (block_depth==0)
        branch — a chain whose append links live inside a loop body must
        stay fully conservative, same as before this fix existed."""
        src = ('x = ""\r\n'
               'For i = 1 To 3\r\n'
               'x = x & "a"\r\n'
               'Next\r\n'
               'y = x\r\n')
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('y = x', out)

    def test_for_loop_counter_not_folded_to_preseed_value(self):
        """A variable assigned before a For loop that also serves as the loop
        counter must not have its pre-loop value substituted inside the loop
        body — the For statement kills x from env before the body is visited."""
        src = (
            'x = 5\r\n'
            'For x = 1 To 3\r\n'
            '    WScript.Echo x\r\n'
            'Next\r\n'
        )
        out, stats = _run(src)
        self.assertNotIn('WScript.Echo 5', out)
        self.assertIn('WScript.Echo x', out)


class TestRunInterruption(unittest.TestCase):
    """A self-append run must stop exactly at the first statement that
    breaks its shape, collapsing only the valid prefix — the rest of the
    file is then handled by the normal per-statement path, unaffected."""

    def test_run_broken_by_unrelated_statement(self):
        src = ('x = ""\r\n'
               'x = x & "a"\r\n'
               'WScript.Echo "hi"\r\n'
               'x = x & "b"\r\n'
               'y = x\r\n')
        out, stats = _run(src)
        self.assertEqual(
            out,
            'x = "a"\r\nWScript.Echo "hi"\r\nx = "a" & "b"\r\ny = "ab"\r\n')

    def test_run_broken_by_nonconstant_link(self):
        """A link whose RHS references something unresolvable ends the run
        at that point — the valid prefix still collapses, and the breaking
        statement's write kills the variable (an unresolvable link makes
        its value unknowable going forward), so a later read stays bare."""
        src = ('x = ""\r\n'
               'x = x & "a"\r\n'
               'x = x & SomeUnknownFunc()\r\n'
               'y = x\r\n')
        out, stats = _run(src)
        self.assertEqual(
            out,
            'x = "a"\r\nx = "a" & SomeUnknownFunc()\r\ny = x\r\n')


class TestIntrinsicConstants(unittest.TestCase):
    def test_vbcrlf_substitutes_after_tokenizer_change(self):
        out, stats = _run('x = "a" & vbCrLf & "b"\r\ny = x\r\n')
        self.assertGreater(stats['changed'], 0)
        self.assertIn('y = "a\r\nb"', out)

    def test_vbnullstring_substituted_as_empty_string(self):
        # vbNullString resolves to "" (empty string). The substitution fires in
        # an assignment RHS (not a call stmt, which would kill the variable).
        src = 'x = vbNullString\r\ny = x\r\n'
        out, stats = _run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('y = ""', out)

    def test_vbtab_substituted(self):
        # vbTab resolves to Chr(9) — a tab character.
        # Use an assignment read (not WScript.Echo) so the variable is not killed
        # before substitution fires (call stmts kill ByRef args without substituting).
        src = 'sep = vbTab\r\ny = sep\r\n'
        out, stats = _run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('y = sep', out)   # bare identifier replaced
        self.assertIn('\t', out)           # tab character embedded in the output


class TestNumericAndBooleanPropagation(unittest.TestCase):
    """Numeric and boolean constant propagation paths are less-deeply tested
    than the string accumulator path — these fill that gap."""

    def test_numeric_constant_substituted_into_arithmetic(self):
        # The propagator substitutes the identifier token (n → 10) in the
        # arithmetic RHS, but does NOT further evaluate the resulting expression
        # (10 * 2 stays as-is; only the resolver-updated env entry is folded).
        src = 'n = 10\r\nresult = n * 2\r\n'
        out, stats = _run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('result = 10 * 2', out)   # token substituted, expr not evaluated

    def test_boolean_true_propagated_as_minus_one(self):
        # VBScript True == -1 (integer); resolver returns the Python int -1
        # which format_number serialises as "-1".
        src = 'flag = True\r\nx = flag\r\n'
        out, stats = _run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('x = -1', out)

    def test_boolean_false_propagated_as_zero(self):
        src = 'flag = False\r\nx = flag\r\n'
        out, stats = _run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('x = 0', out)


class TestEndToEndCli(unittest.TestCase):
    """Drives the real CLI script via subprocess, reproducing the shape of
    the sample that surfaced these bugs: a decoder Sub, a short accumulator
    that should fold fully, a long accumulator that should partially fold
    and then stop, a ByRef decode call, and a read after the call."""

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
        return out.read_text(encoding='utf-8'), stats

    def test_v7773_shaped_pattern(self):
        chunk = 'x' * 500
        n_long_chunks = 30
        src = DECODER
        src += 'Dim short, long_v\r\n'
        src += 'short = ""\r\nshort = short & "hi"\r\n'
        src += 'f short,short\r\n'
        src += 'long_v = ""\r\n'
        src += ''.join(f'long_v = long_v & "{chunk}"\r\n' for _ in range(n_long_chunks))
        src += 'f long_v,long_v\r\n'
        src += 'If True Then\r\n'
        src += 'WScript.Echo short\r\n'
        src += 'WScript.Echo long_v\r\n'
        src += 'End If\r\n'

        out, stats = self._run_cli(src)
        self.assertGreater(stats['changed'], 0)
        # short accumulator: call args stay bare (ByRef, unknown post-call),
        # and the chain build itself is now one collapsed literal.
        self.assertIn('short = "hi"', out)
        self.assertIn('f short,short', out)
        # the long chain now fully collapses into one literal too — this is
        # the reported bug's fix: it must no longer be left partially
        # unresolved, and no O(n^2) blowup should occur getting there.
        self.assertIn(f'long_v = "{chunk * n_long_chunks}"', out)
        self.assertNotIn('long_v = long_v &', out)
        self.assertIn('f long_v,long_v', out)
        self.assertIn('WScript.Echo long_v', out)


class TestLargeChainPerformance(unittest.TestCase):
    """A scaled-down stand-in for the real ~1840-link/1-2KB-chunk sample,
    sized to stay fast and non-flaky in CI while still proving the fix
    handles a long chain through the real CLI path, not just via the
    in-process run() helper."""

    def test_large_chain_collapses_quickly_via_cli(self):
        chunk = 'q' * 200
        n_chunks = 2000
        src = 'big = ""\r\n' + ''.join(f'big = big & "{chunk}"\r\n' for _ in range(n_chunks))

        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / 'in.vbs'
            out_path = Path(tmp) / 'out.vbs'
            inp.write_bytes(src.encode('utf-8'))
            t0 = time.time()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--input', str(inp), '--output', str(out_path)],
                capture_output=True, text=True, check=True, timeout=30)
            elapsed = time.time() - t0
            stats = json.loads(result.stdout)
            with open(out_path, encoding='utf-8', newline='') as fh:
                out = fh.read()

        self.assertEqual(out, f'big = "{chunk * n_chunks}"\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertLess(elapsed, 10)


if __name__ == '__main__':
    unittest.main()
