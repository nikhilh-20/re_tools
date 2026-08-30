"""bat_remove_deadcode -- unreachable code, dead stores, and the C3 fix.

C3: an unresolved goto target (a computed `goto %VAR%`) used to void the
ENTIRE reachability analysis. Now it degrades locally -- every label is seeded
as a possible landing site (a sound over-approximation) and code still dead
under that assumption is removed, with `reachability_degraded:true` reported.
"""
import unittest

from tests._harness import call_fn, assert_idempotent


def _run(src, **kw):
    return call_fn('bat_remove_deadcode', 'remove_deadcode', src, **kw)


class TestUnreachable(unittest.TestCase):
    def test_code_after_unconditional_goto_removed(self):
        src = ('@echo off\r\n'
               'goto :skip\r\n'
               'echo never runs\r\n'
               ':skip\r\n'
               'echo after\r\n')
        out, stats = _run(src)
        self.assertEqual(stats['unreachable_removed'], 1)
        self.assertNotIn('never runs', out)
        self.assertIn('echo after', out)

    def test_code_after_exit_b_removed(self):
        src = 'echo one\r\nexit /b 0\r\necho two\r\n'
        out, stats = _run(src)
        self.assertNotIn('echo two', out)

    def test_orphan_label_block_removed(self):
        src = ('echo main\r\n'
               'goto :done\r\n'
               ':orphan\r\n'
               'echo orphaned\r\n'
               ':done\r\n'
               'echo end\r\n')
        out, _ = _run(src)
        self.assertNotIn('orphaned', out)


class TestDeadStores(unittest.TestCase):
    def test_never_read_set_removed(self):
        src = 'set "junk=abc"\r\nset "used=1"\r\necho %used%\r\n'
        out, stats = _run(src)
        self.assertGreaterEqual(stats['dead_stores_removed'], 1)
        self.assertNotIn('junk', out)
        self.assertIn('set "used=1"', out)

    def test_var_read_inside_quoted_child_command_is_protected(self):
        src = ('set "PAYLOAD=whoami"\r\n'
               'powershell -Command "iex $env:PAYLOAD"\r\n')
        out, _ = _run(src)
        self.assertIn('PAYLOAD', out)  # child process reads it by name


class TestComputedGotoDegradesLocally(unittest.TestCase):
    SRC = ('@echo off\r\n'
           'set "target=%1"\r\n'
           'goto %target%\r\n'
           'echo unreachable past the computed jump\r\n'
           ':realstart\r\n'
           'echo hello\r\n'
           'goto :done\r\n'
           'echo dead block no inbound edge\r\n'
           'echo still dead\r\n'
           ':done\r\n'
           'echo bye\r\n')

    def test_does_not_refuse_globally(self):
        out, stats = _run(self.SRC)
        self.assertNotIn('reason', stats)
        self.assertTrue(stats.get('reachability_degraded'))
        self.assertGreaterEqual(stats['unresolved_goto_targets'], 1)
        self.assertGreater(stats['changed'], 0)

    def test_removes_code_dead_under_every_landing(self):
        out, _ = _run(self.SRC)
        self.assertNotIn('unreachable past the computed jump', out)
        self.assertNotIn('dead block no inbound edge', out)
        self.assertNotIn('still dead', out)

    def test_keeps_label_reachable_code(self):
        out, _ = _run(self.SRC)
        self.assertIn('echo hello', out)   # :realstart could be the landing site
        self.assertIn('echo bye', out)


class TestIdempotent(unittest.TestCase):
    def test_idempotent(self):
        src = ('@echo off\r\ngoto :skip\r\necho x\r\n:skip\r\nset "d=1"\r\necho done\r\n')
        assert_idempotent(self, lambda s, **k: _run(s, **k), src)


if __name__ == '__main__':
    unittest.main()
