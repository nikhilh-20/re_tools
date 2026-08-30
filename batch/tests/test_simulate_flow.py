"""batdeoblib.simulate -- flow sensitivity, incl. the H6a loop-body fix.

H6a: a `for` body runs many times; a `!VAR!` read that comes earlier in the
body than that iteration's write to VAR (the `set "ACC=!ACC!x"` accumulator
shape) must NOT resolve to VAR's pre-loop value. The simulator now invalidates
every name a block assigns on the live env *before* walking the body, so those
reads come back Unknown and the fold passes leave them alone.
"""
import unittest

from tests._harness import TOOL_DIR, call_fn  # noqa: F401
from batdeoblib.tokenizer import tokenize
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate
from batdeoblib.env import Env


def _steps(src):
    return list(simulate(parse_script(tokenize(src)), Env()))


def _find(src, needle):
    for st in _steps(src):
        if needle in ''.join(t.value for t in st.stmt.tokens):
            return st
    raise AssertionError(f'no statement containing {needle!r}')


class TestTopLevelReuse(unittest.TestCase):
    def test_reused_name_sees_current_value(self):
        src = 'set "M=first"\r\necho %M%\r\nset "M=second"\r\necho AGAIN %M%\r\n'
        st = _find(src, 'AGAIN')
        self.assertEqual(st.pct_env.resolve_read('M'), 'second')

    def test_empty_set_deletes(self):
        src = 'set "X=1"\r\nset "X="\r\necho %X%\r\n'
        st = _find(src, 'echo')
        self.assertEqual(st.pct_env.resolve_read('X'), '')  # Unset -> '' (foldable)


class TestBlockSnapshotVsLive(unittest.TestCase):
    def test_percent_ref_uses_block_entry_value(self):
        src = ('set "V=outer"\r\n'
               'if 1==1 (\r\n'
               'set "V=inner"\r\n'
               'echo PCT %V%\r\n'
               ')\r\n')
        st = _find(src, 'PCT')
        # %V% inside a block resolves against the block-entry snapshot
        self.assertEqual(st.pct_env.resolve_read('V'), 'outer')


class TestLoopBodyAccumulatorH6a(unittest.TestCase):
    SRC = ('setlocal EnableDelayedExpansion\r\n'
           'set "S=abcdef"\r\n'
           'set "ACC="\r\n'
           'for %%i in (1 2 3) do (set "ACC=!ACC!!S:~0,1!")\r\n'
           'echo !ACC!\r\n')

    def test_bang_accumulator_read_in_loop_is_unknown(self):
        st = _find(self.SRC, 'set "ACC=!ACC!')
        self.assertIsNone(st.env.resolve_read('ACC'),
                          'ACC must be Unknown inside its own for-body, not the pre-loop value')

    def test_propagate_constants_leaves_loop_body_intact(self):
        out, stats = call_fn('bat_propagate_constants', 'propagate_constants', self.SRC)
        self.assertEqual(stats['changed'], 0)
        self.assertIn('set "ACC=!ACC!!S:~0,1!"', out)

    def test_fold_for_loops_still_folds_the_accumulator(self):
        out, stats = call_fn('bat_fold_for_loops', 'fold_for_loops', self.SRC)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "ACC=aaa"', out)


if __name__ == '__main__':
    unittest.main()
