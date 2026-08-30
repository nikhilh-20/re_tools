"""H6b -- a long `set "P=%P%<chunk>"` self-append chain must not make the fold
passes blow up. VBS's analogue (vbs_propagate_constants) needed a run-collapse +
a structural refuse on `x = x & x`; batch's passes don't rewrite growing values
into every link the same way, so this is a guard-rail test: confirm the whole
chain still finishes fast through the real CLI.
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests._harness import TOOL_DIR


def _run(script, src, timeout, *extra):
    with tempfile.TemporaryDirectory() as d:
        inp, out = Path(d) / 'i.cmd', Path(d) / 'o.cmd'
        inp.write_bytes(src.encode('utf-8'))
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, str(TOOL_DIR / script),
                            '--input', str(inp), '--output', str(out), *extra],
                           capture_output=True, text=True, timeout=timeout)
        return r, out.read_bytes() if out.exists() else b'', time.perf_counter() - t0


class TestLongSelfAppendChain(unittest.TestCase):
    N = 2000
    CHUNK = 'q' * 60

    def _chain_src(self):
        s = 'set "P="\r\n'
        for _ in range(self.N):
            s += f'set "P=%P%{self.CHUNK}"\r\n'
        s += 'echo %P%\r\n'
        return s

    def test_fold_concat_finishes_fast(self):
        r, _out, dt = _run('bat_fold_concat.py', self._chain_src(), 40)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertLess(dt, 25.0, f'{dt:.1f}s for a {self.N}-link chain')

    def test_propagate_constants_finishes_fast(self):
        r, out, dt = _run('bat_propagate_constants.py', self._chain_src(), 40)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertLess(dt, 25.0, f'{dt:.1f}s')
        self.assertLess(len(out), 50 * 1024 * 1024, 'output grew explosively')

    def test_unbounded_self_squaring_is_bounded(self):
        # set "X=%X%%X%" doubles every eval -- must not run away
        src = 'set "X=ab"\r\n' + 'set "X=%X%%X%"\r\n' * 30 + 'echo %X%\r\n'
        r, out, dt = _run('bat_fold_concat.py', src, 30)
        self.assertLess(dt, 15.0, f'{dt:.1f}s')
        self.assertLess(len(out), 50 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
