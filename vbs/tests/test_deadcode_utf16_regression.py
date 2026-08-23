"""End-to-end regression test for the UTF-16 input bug found while
deobfuscating a real sample: vbs_remove_deadcode.py --aggressive reported
`changed: 0` on a UTF-16LE-with-BOM file because vbsdeoblib/io.py read every
--input as utf-8-sig. Reproduces the exact pattern from that sample (an
empty-body helper function fed by a self-referential accumulator chain,
never read outside its own writer lines) and drives the real CLI script via
subprocess, matching what a user actually runs.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
DEADCODE_SCRIPT = TOOL_DIR / 'vbs_remove_deadcode.py'

# Minimal repro of the real-world pattern: a no-op stub function, called only
# by a chain that reads-and-rewrites its own accumulator variable, which is
# never read anywhere else in the file.
REPRO_SRC = (
    'Dim other\r\n'
    'other = "keep me"\r\n'
    'Function noop(a, b)\r\n'
    'End Function\r\n'
    'uncannily = (uncannily) & noop("x", "y")\r\n'
    'uncannily = (uncannily) & noop("x", "y")\r\n'
    'uncannily = (uncannily) & noop("x", "y")\r\n'
    'WScript.Echo other\r\n'
)


def _run_deadcode(input_path: Path, output_path: Path, *, aggressive: bool) -> dict:
    cmd = [sys.executable, str(DEADCODE_SCRIPT), '--input', str(input_path),
           '--output', str(output_path)]
    if aggressive:
        cmd.append('--aggressive')
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class TestDeadcodeUtf16Regression(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_utf16_le_bom_input_is_cleaned_with_aggressive(self):
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(b'\xff\xfe' + REPRO_SRC.encode('utf-16-le'))

        stats = _run_deadcode(inp, out, aggressive=True)
        cleaned = out.read_text(encoding='utf-8')

        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('uncannily', cleaned)
        self.assertNotIn('noop', cleaned)
        self.assertIn('other', cleaned)  # the live variable must survive

    def test_utf8_input_parity_with_utf16_input(self):
        """The fix must not change behavior for the already-working UTF-8
        case: same source, plain UTF-8, should clean identically."""
        inp_utf8 = self.tmp / 'in_utf8.vbs'
        out_utf8 = self.tmp / 'out_utf8.vbs'
        inp_utf8.write_bytes(REPRO_SRC.encode('utf-8'))
        _run_deadcode(inp_utf8, out_utf8, aggressive=True)

        inp_utf16 = self.tmp / 'in_utf16.vbs'
        out_utf16 = self.tmp / 'out_utf16.vbs'
        inp_utf16.write_bytes(b'\xff\xfe' + REPRO_SRC.encode('utf-16-le'))
        _run_deadcode(inp_utf16, out_utf16, aggressive=True)

        self.assertEqual(out_utf8.read_text(encoding='utf-8'),
                          out_utf16.read_text(encoding='utf-8'))

    def test_utf16_le_bom_input_without_aggressive_leaves_accumulator(self):
        """Without --aggressive, the self-referential accumulator chain is
        documented as live (a real read exists on every line) — the fix
        must only repair decoding, not change that liveness semantics."""
        inp = self.tmp / 'in.vbs'
        out = self.tmp / 'out.vbs'
        inp.write_bytes(b'\xff\xfe' + REPRO_SRC.encode('utf-16-le'))

        _run_deadcode(inp, out, aggressive=False)
        cleaned = out.read_text(encoding='utf-8')

        self.assertIn('uncannily', cleaned)
        self.assertIn('noop', cleaned)


if __name__ == '__main__':
    unittest.main()
