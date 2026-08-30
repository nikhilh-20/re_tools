"""Control-flow passes: unflatten_goto, inline_subroutines, resolve_indirection.
"""
import unittest

from tests._harness import call_fn, assert_idempotent


def _fn(mod, name):
    return lambda s, **k: call_fn(mod, name, s, **k)


class TestUnflattenGoto(unittest.TestCase):
    f = staticmethod(_fn('bat_unflatten_goto', 'unflatten_goto'))

    def test_straightens_one_link_per_call(self):
        src = ('@echo off\r\n'
               'goto PART_C\r\n'
               ':PART_A\r\n'
               'echo step-A\r\n'
               'exit /b 0\r\n'
               ':PART_C\r\n'
               'echo step-1\r\n'
               'goto PART_B\r\n'
               ':PART_B\r\n'
               'echo step-2\r\n'
               'goto PART_A\r\n')
        out, stats = self.f(src)
        self.assertGreater(stats['changed'], 0)
        # after one call, step-1's body is spliced where `goto PART_C` was
        self.assertLess(out.index('echo step-1'), out.index('echo step-A'))

    def test_multi_incoming_target_not_moved(self):
        src = ('goto L\r\n'
               ':other\r\n'
               'goto L\r\n'
               ':L\r\n'
               'echo shared\r\n')
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)   # L has 2 incoming edges

    def test_computed_goto_stops_it(self):
        src = 'goto %x%\r\n:a\r\necho a\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent_after_fixpoint(self):
        src = '@echo off\r\ngoto A\r\n:B\r\necho b\r\nexit /b\r\n:A\r\necho a\r\ngoto B\r\n'
        cur = src
        for _ in range(6):
            cur, st = self.f(cur)
            if st['changed'] == 0:
                break
        again, st2 = self.f(cur)
        self.assertEqual(st2['changed'], 0)
        self.assertEqual(cur, again)


class TestInlineSubroutines(unittest.TestCase):
    f = staticmethod(_fn('bat_inline_subroutines', 'inline_subroutines'))

    def test_single_call_site_inlined(self):
        src = ('call :greet hello world\r\n'
               'echo done\r\n'
               'goto :eof\r\n'
               ':greet\r\n'
               'echo arg1=%1 arg2=%2\r\n'
               'goto :eof\r\n')
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('echo arg1=hello arg2=world', out)

    def test_two_call_sites_not_inlined(self):
        src = ('call :s\r\ncall :s\r\ngoto :eof\r\n:s\r\necho hi\r\ngoto :eof\r\n')
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_embedded_early_return_not_inlined(self):
        src = ('call :s x\r\ngoto :eof\r\n:s\r\nif "%1"=="x" goto :eof\r\necho tail\r\ngoto :eof\r\n')
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        src = ('call :s 1\r\ngoto :eof\r\n:s\r\necho got %1\r\ngoto :eof\r\n')
        assert_idempotent(self, self.f, src)


class TestResolveIndirection(unittest.TestCase):
    f = staticmethod(_fn('bat_resolve_indirection', 'resolve_indirection'))

    def test_call_set_double_percent_bang(self):
        src = ('setlocal EnableDelayedExpansion\r\n'
               'set "IDX=REALNAME"\r\n'
               'set "REALNAME=secretvalue"\r\n'
               'call set "RESULT=%%!IDX!%%"\r\n')
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "RESULT=%REALNAME%"', out)
        self.assertNotIn('call set "RESULT=', out)

    def test_call_label_not_touched(self):
        src = 'call :realsub arg\r\n:realsub\r\ngoto :eof\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        src = ('setlocal EnableDelayedExpansion\r\nset "Y=A"\r\nset "A=v"\r\n'
               'call set "R=%%!Y!%%"\r\n')
        assert_idempotent(self, self.f, src)


if __name__ == '__main__':
    unittest.main()
