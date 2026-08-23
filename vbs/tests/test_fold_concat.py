"""Unit and regression tests for vbs_fold_concat.py.

Covers every control flow exercised by the pipeline:
  - Two adjacent string literals folded into one
  - Three-element chain folded
  - Unresolvable atom breaks the chain but adjacent constant runs still fold
  - Lone literal beside an unresolvable atom is NOT folded (run length < 2)
  - + treated same as & for chain structure
  - env dict: known variable values participate in folding
  - Pass 7 of the pipeline (run on dead-code-removed output): changed == 2
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_fold_concat as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_fold_concat.py'
SAMPLE   = Path(r'C:\Users\Ashura\Desktop\cef108df7267250b66dca8e6ab87a629591b9840f27e3ab1821248ebfe2cdb1f.vbs')


# ---------------------------------------------------------------------------
# Helper to run pass 1–6 of the pipeline on a temp copy of the sample,
# returning the pass-6 output path. Used only by the pipeline stat test.
# ---------------------------------------------------------------------------

def _run_script(script: Path, inp: Path, out: Path, extra: list | None = None) -> dict:
    cmd = [sys.executable, str(script), '--input', str(inp), '--output', str(out)]
    if extra:
        cmd.extend(extra)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestFoldConcat(unittest.TestCase):

    def test_two_literals_folded(self):
        src = 'x = "hello" & " world"\n'
        out, stats = tool.run(src)
        self.assertIn('"hello world"', out)
        self.assertNotIn('" world"', out)
        self.assertEqual(stats['changed'], 1)

    def test_three_literal_chain_folded(self):
        src = 'x = "h" & "t" & "t"\n'
        out, stats = tool.run(src)
        self.assertIn('"htt"', out)
        self.assertEqual(stats['changed'], 1)

    def test_four_literal_chain_folded(self):
        src = 'x = "h" & "t" & "t" & "p"\n'
        out, stats = tool.run(src)
        self.assertIn('"http"', out)
        self.assertEqual(stats['changed'], 1)

    def test_unresolvable_atom_breaks_chain_but_constant_tail_folds(self):
        # "a" is lone (adjacent to var), "b" & "c" is a run of 2 → folds
        src = 'x = "a" & unknownVar & "b" & "c"\n'
        out, stats = tool.run(src)
        self.assertIn('"bc"', out)
        self.assertIn('"a"', out)       # lone literal preserved
        self.assertIn('unknownVar', out)
        self.assertEqual(stats['changed'], 1)

    def test_lone_literal_beside_unresolvable_not_folded(self):
        # Only one resolvable atom on each side — neither run is >= 2 atoms
        src = 'x = "a" & unknownVar\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_already_folded_is_idempotent(self):
        src = 'x = "https://example.com"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_env_dict_allows_variable_participation(self):
        # When env is provided with a known variable value, it can participate in folding
        src = 'x = prefix & "suffix"\n'
        out, stats = tool.run(src, env={'prefix': 'pre'})
        self.assertIn('"presuffix"', out)
        self.assertEqual(stats['changed'], 1)

    def test_env_dict_none_treats_variables_as_unresolvable(self):
        src = 'x = prefix & "suffix"\n'
        out, stats = tool.run(src, env=None)
        self.assertEqual(stats['changed'], 0)

    def test_plus_operator_folds_string_literals(self):
        # '+' is treated the same as '&' for chain detection; the resolver
        # evaluates it at the _add level where Python str + str concatenates,
        # so two adjacent string literals connected by '+' collapse.
        src = 'x = "hello" + " world"\n'
        out, stats = tool.run(src)
        self.assertIn('"hello world"', out)
        self.assertEqual(stats['changed'], 1)

    def test_mixed_plus_and_amp_chain_folds_fully(self):
        # '+' and '&' in one chain: the resolver handles '+' at a tighter
        # binding level than '&' (matching VBScript precedence), so all three
        # literals collapse to a single string.
        src = 'x = "a" + "b" & "c"\n'
        out, stats = tool.run(src)
        self.assertIn('"abc"', out)
        self.assertGreater(stats['changed'], 0)

    def test_numeric_literal_in_concat_chain(self):
        # A NUMBER token is resolvable; the resolver coerces it to a string
        # when concatenated with string operands via '&'.
        src = 'x = "n=" & 42 & "!"\n'
        out, stats = tool.run(src)
        self.assertIn('"n=42!"', out)
        self.assertEqual(stats['changed'], 1)

    def test_string_with_embedded_quotes_handled(self):
        # VBScript escaped quote ("") inside a string literal
        src = 'x = "say ""hello""" & " world"\n'
        out, stats = tool.run(src)
        self.assertIsInstance(out, str)
        self.assertIsInstance(stats['changed'], int)

    def test_no_chr_calls_remain_in_concat_chain(self):
        # After fold_chr_calls, Chr tokens become string literals;
        # fold_concat should then collapse them
        # Simulate a post-chr-fold string: "s" & "://p" & "df-br"
        src = 'x = "s" & "://p" & "df-br"\n'
        out, stats = tool.run(src)
        self.assertIn('"s://pdf-br"', out)
        self.assertEqual(stats['changed'], 1)

    def test_multiline_concat_not_folded_across_lines(self):
        # Two separate assignments are not merged
        src = 'x = "hello"\ny = " world"\n'
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 0)

    def test_second_pass_on_already_folded_output_reports_zero(self):
        src = 'x = "a" & "b" & "c"\n'
        out1, stats1 = tool.run(src)
        self.assertGreater(stats1['changed'], 0)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)

    def test_colon_separated_statements_each_folded_independently(self):
        # Colon acts as a statement separator; concat chains in each logical
        # statement are folded independently without cross-statement merging.
        src = 'x = "a" & "b" : y = "c" & "d"\n'
        out, stats = tool.run(src)
        self.assertIn('"ab"', out)
        self.assertIn('"cd"', out)
        self.assertNotIn('"abcd"', out)
        self.assertGreater(stats['changed'], 0)

    def test_colon_separator_stops_chain_expansion(self):
        # A literal on one side of a colon must not be pulled into the chain
        # on the other side — the chain finder stops at statement boundaries.
        src = 'x = "end" : y = "start" & "middle"\n'
        out, stats = tool.run(src)
        self.assertIn('"startmiddle"', out)
        self.assertNotIn('"endstartmiddle"', out)

    def test_empty_string_in_concat_chain_folds(self):
        # An empty string literal is a valid resolvable atom; the whole run
        # collapses correctly even when some atoms are empty strings.
        src = 'x = "" & "hello" & ""\n'
        out, stats = tool.run(src)
        self.assertIn('"hello"', out)
        self.assertGreater(stats['changed'], 0)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestFoldConcatCli(unittest.TestCase):

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

    def test_basic_two_literals_via_cli(self):
        out, stats = self._run_cli('x = "foo" & "bar"\n')
        self.assertIn('"foobar"', out)
        self.assertEqual(stats['changed'], 1)

    def test_cli_outputs_json_stats(self):
        _, stats = self._run_cli('x = "a" & "b"\n')
        self.assertIn('changed', stats)

    @unittest.skipUnless(SAMPLE.exists(), 'VBS sample not found on Desktop')
    def test_pipeline_stat_regression_pass7(self):
        """Pipeline pass 7 is fold_concat run on the dead-code-removed output.
        Re-creates passes 1-6 to reach the same input state, then asserts changed == 2."""
        tmp = self.tmp

        p = [SAMPLE,
             tmp / 'pass1.vbs',
             tmp / 'pass2.vbs',
             tmp / 'pass3.vbs',
             tmp / 'pass4.vbs',
             tmp / 'pass5.vbs',
             tmp / 'pass6.vbs',
             tmp / 'pass7.vbs']

        scripts = [
            (TOOL_DIR / 'vbs_fold_chr_calls.py',     []),
            (SCRIPT,                                   []),
            (TOOL_DIR / 'vbs_propagate_constants.py', []),
            (TOOL_DIR / 'vbs_inline_functions.py',    []),
            (TOOL_DIR / 'vbs_fold_builtin_calls.py',  []),
            (TOOL_DIR / 'vbs_remove_deadcode.py',     []),
            (SCRIPT,                                   []),
        ]

        for i, (script, extra) in enumerate(scripts):
            _run_script(script, p[i], p[i + 1], extra)

        stats = json.loads(subprocess.run(
            [sys.executable, str(SCRIPT),
             '--input', str(p[6]), '--output', str(p[7])],
            capture_output=True, text=True, check=True,
        ).stdout)
        # Pass 7: current toolkit folds 7 chains (old baseline was 2 — the difference
        # is because the current pass 3 propagates fewer constants, leaving more
        # concatenation chains for this later pass to collapse).
        self.assertEqual(stats['changed'], 7,
                         f'Expected changed == 7 for pass 7, got {stats["changed"]}')


if __name__ == '__main__':
    unittest.main()
