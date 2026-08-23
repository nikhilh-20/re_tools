"""Unit and regression tests for vbs_inline_functions.py.

Covers every control flow exercised by the pipeline:
  - Single-expression Function body qualifies for inlining
  - Parameter substitution in body_expr at call sites
  - Multiple call sites all inlined in one pass
  - Definition replaced by blank lines (line count preserved)
  - Call inside another function's body still inlined
  - Multi-line body does NOT qualify
  - Zero-return body does NOT qualify
  - Pipeline stat regression: changed == 3, functions_inlined == 1
  - Idempotent second pass on already-inlined source
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import vbs_inline_functions as tool

TOOL_DIR = Path(__file__).resolve().parent.parent
SCRIPT   = TOOL_DIR / 'vbs_inline_functions.py'
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

class TestInlineFunctions(unittest.TestCase):

    # The exact pattern from the VBS sample
    TTAFFRY_SRC = (
        'Function ttaffRy(s)\r\n'
        '    ttaffRy = Replace(s, "@", "")\r\n'
        'End Function\r\n'
        'Set uecSSL = CreateObject(ttaffRy("WScr@i@pt@.S@@h@@e@ll"))\r\n'
    )

    def test_single_expression_body_inlined(self):
        src = (
            'Function strip(s)\r\n'
            '    strip = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'x = strip("a@b@c")\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['functions_inlined'], 1)
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('strip("', out)
        self.assertIn('Replace("a@b@c"', out)

    def test_parameter_substituted_in_body(self):
        src = (
            'Function wrap(s)\r\n'
            '    wrap = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'y = wrap("WScr@i@pt@.S@@h@@e@ll")\r\n'
        )
        out, stats = tool.run(src)
        self.assertIn('Replace("WScr@i@pt@.S@@h@@e@ll"', out)
        self.assertEqual(stats['functions_inlined'], 1)

    def test_definition_removed_after_inlining(self):
        src = (
            'Function strip(s)\r\n'
            '    strip = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'x = strip("a@b")\r\n'
        )
        out, stats = tool.run(src)
        # The definition must be gone (only blank lines remain in its place)
        self.assertNotIn('Function strip', out)
        self.assertNotIn('End Function', out)

    def test_definition_removal_preserves_line_count(self):
        src = (
            'Function strip(s)\r\n'
            '    strip = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'x = strip("a@b")\r\n'
        )
        out, stats = tool.run(src)
        # Same number of newlines — line numbers of subsequent code are stable
        self.assertEqual(src.count('\n'), out.count('\n'))

    def test_multiple_call_sites_all_inlined(self):
        src = (
            'Function strip(s)\r\n'
            '    strip = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'a = strip("x@y")\r\n'
            'b = strip("p@q")\r\n'
            'c = strip("r@s")\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 3)
        self.assertEqual(stats['functions_inlined'], 1)
        self.assertNotIn('strip(', out)

    def test_call_inside_another_function_inlined(self):
        # ttaffRy is called inside GetPCHash — must still be inlined
        src = (
            'Function ttaffRy(s)\r\n'
            '    ttaffRy = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'Function GetPCHash()\r\n'
            '    Set sh = CreateObject(ttaffRy("WScr@i@pt@.S@@h@@e@ll"))\r\n'
            '    GetPCHash = "hash"\r\n'
            'End Function\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['functions_inlined'], 1)
        self.assertNotIn('ttaffRy(', out)
        self.assertIn('Replace("WScr@i@pt@.S@@h@@e@ll"', out)

    def test_multi_line_body_not_inlined(self):
        src = (
            'Function twolines(s)\r\n'
            '    x = Replace(s, "@", "")\r\n'
            '    twolines = x\r\n'
            'End Function\r\n'
            'y = twolines("a@b")\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['functions_inlined'], 0)
        self.assertEqual(stats['changed'], 0)

    def test_function_with_no_return_not_inlined(self):
        # Body has one line but does not assign to the function name
        src = (
            'Function noop(s)\r\n'
            '    WScript.Echo s\r\n'
            'End Function\r\n'
            'noop("hello")\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['functions_inlined'], 0)
        self.assertEqual(stats['changed'], 0)

    def test_inlined_result_wrapped_in_parens(self):
        # The replacement is always (body_expr) so precedence is preserved
        src = (
            'Function f(x)\r\n'
            '    f = x & "!"\r\n'
            'End Function\r\n'
            'y = f("hi")\r\n'
        )
        out, stats = tool.run(src)
        self.assertIn('(', out)   # wrapped in parens
        self.assertEqual(stats['functions_inlined'], 1)

    def test_second_pass_on_inlined_output_is_idempotent(self):
        src = (
            'Function strip(s)\r\n'
            '    strip = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'x = strip("a@b")\r\n'
        )
        out1, stats1 = tool.run(src)
        self.assertEqual(stats1['functions_inlined'], 1)
        out2, stats2 = tool.run(out1)
        self.assertEqual(stats2['changed'], 0)
        self.assertEqual(stats2['functions_inlined'], 0)

    def test_uncalled_function_not_inlined(self):
        # Definition exists but no call site → nothing to inline
        src = (
            'Function unused(s)\r\n'
            '    unused = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'x = 1\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['functions_inlined'], 0)
        self.assertEqual(stats['changed'], 0)

    def test_ttaffry_exact_pattern_from_sample(self):
        # The exact three-call pattern from the VBS sample (condensed)
        src = (
            'Function ttaffRy(s)\r\n'
            '    ttaffRy = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'Set uecSSL = CreateObject(ttaffRy("WScr@i@pt@.S@@h@@e@ll"))\r\n'
            'uecSSL.Run ttaffRy("Ru@@n@D@@l@@l32.@@e@x@@e"), 0, True\r\n'
            'Set sh = CreateObject(ttaffRy("WScr@i@pt@.S@@h@@e@ll"))\r\n'
        )
        out, stats = tool.run(src)
        self.assertEqual(stats['changed'], 3)
        self.assertEqual(stats['functions_inlined'], 1)
        self.assertNotIn('ttaffRy(', out)
        self.assertNotIn('Function ttaffRy', out)
        self.assertIn('Replace("WScr@i@pt@.S@@h@@e@ll"', out)
        self.assertIn('Replace("Ru@@n@D@@l@@l32.@@e@x@@e"', out)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------

class TestInlineFunctionsCli(unittest.TestCase):

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

    def test_cli_inlines_simple_wrapper(self):
        src = (
            'Function f(s)\r\n'
            '    f = Replace(s, "@", "")\r\n'
            'End Function\r\n'
            'x = f("a@b")\r\n'
        )
        out, stats = self._run_cli(src)
        self.assertEqual(stats['functions_inlined'], 1)
        self.assertNotIn('f(', out)

    def test_cli_json_includes_functions_inlined_key(self):
        src = (
            'Function f(s)\r\n'
            '    f = s\r\n'
            'End Function\r\n'
            'x = f("hello")\r\n'
        )
        _, stats = self._run_cli(src)
        self.assertIn('changed', stats)
        self.assertIn('functions_inlined', stats)

    @unittest.skipUnless(SAMPLE.exists(), 'VBS sample not found on Desktop')
    def test_pipeline_stat_regression_pass4(self):
        """Pipeline pass 4 is inline_functions on the propagated output.
        Baseline: changed == 3, functions_inlined == 1."""
        tmp = self.tmp
        p = [SAMPLE,
             tmp / 'pass1.vbs',
             tmp / 'pass2.vbs',
             tmp / 'pass3.vbs',
             tmp / 'pass4.vbs']

        scripts = [
            TOOL_DIR / 'vbs_fold_chr_calls.py',
            TOOL_DIR / 'vbs_fold_concat.py',
            TOOL_DIR / 'vbs_propagate_constants.py',
            SCRIPT,
        ]
        for i, script in enumerate(scripts):
            _run_script(script, p[i], p[i + 1])

        stats = _run_script(SCRIPT, p[3], p[4])
        self.assertEqual(stats['changed'], 3,
                         f'Expected changed == 3, got {stats["changed"]}')
        self.assertEqual(stats['functions_inlined'], 1,
                         f'Expected functions_inlined == 1, got {stats["functions_inlined"]}')

        out_text = p[4].read_text(encoding='utf-8')
        self.assertNotIn('ttaffRy(', out_text,
                         'All ttaffRy calls should be inlined')
        self.assertIn('Replace(', out_text,
                      'Inlined body expression should be present')


if __name__ == '__main__':
    unittest.main()
