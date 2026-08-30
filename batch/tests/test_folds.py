"""The core expansion-folding passes: substrings, strsub, concat, arithmetic,
for-loops, propagate. One file, one class per pass -- the shapes each must
fold and the shapes each must refuse.
"""
import unittest

from tests._harness import call_fn, assert_idempotent


def _fn(mod, name):
    return lambda s, **k: call_fn(mod, name, s, **k)


class TestFoldSubstrings(unittest.TestCase):
    f = staticmethod(_fn('bat_fold_substrings', 'fold_substrings'))

    def test_char_harvest(self):
        out, stats = self.f('set "S=XYZabcdefTARGETxyz"\r\nset "D=%S:~9,6%"\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "D=TARGET"', out)

    def test_negative_start(self):
        out, _ = self.f('set "S=abcdefgh"\r\necho %S:~-3%\r\n')
        self.assertIn('echo fgh', out)

    def test_delayed_expansion_bang_form(self):
        out, stats = self.f('setlocal EnableDelayedExpansion\r\nset "S=hello"\r\nset "D=!S:~1,3!"\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "D=ell"', out)

    def test_ambient_var_refused(self):
        # %RANDOM% is Unknown (real at runtime, unknowable statically); a
        # never-assigned non-ambient var, by contrast, IS provably empty.
        src = 'echo %RANDOM:~0,2%\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set "S=abcdef"\r\necho %S:~2,2%\r\n')


class TestFoldStrsub(unittest.TestCase):
    f = staticmethod(_fn('bat_fold_strsub', 'fold_strsub'))

    def test_replace_all(self):
        out, stats = self.f('set "T=a.b.c"\r\necho %T:.=_%\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('echo a_b_c', out)

    def test_star_prefix(self):
        out, _ = self.f('set "T=foo.bar.baz"\r\necho %T:*.=X%\r\n')
        self.assertIn('echo Xbar.baz', out)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set "T=x-y-z"\r\necho %T:-=+%\r\n')


class TestFoldConcat(unittest.TestCase):
    f = staticmethod(_fn('bat_fold_concat', 'fold_concat'))

    def test_juxtaposition(self):
        out, stats = self.f('set "A=Hello"\r\nset "B=World"\r\nset "C=%A%%B%!"\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "C=HelloWorld!"', out)

    def test_undefined_var_folds_to_empty_keyword_defrag(self):
        # R%UNDEF%em  -> rem  (undefined non-ambient var is provably '')
        out, stats = self.f('R%QWZ9%em this is now a comment\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('Rem this is now a comment', out)

    def test_ambient_var_not_emptied(self):
        src = 'echo %SystemRoot%\\x %APPDATA%\\y\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set "A=1"\r\nset "B=2"\r\necho %A%%B%done\r\n')


class TestFoldArithmetic(unittest.TestCase):
    f = staticmethod(_fn('bat_fold_arithmetic', 'fold_arithmetic'))

    def test_constant_expr(self):
        out, stats = self.f('set /a "x=(18+18-(13-17))+32"\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set /a "x=72"', out)

    def test_bare_identifier_read(self):
        out, _ = self.f('set "y=4"\r\nset /a "z=y+1"\r\n')
        self.assertIn('set /a "z=5"', out)

    def test_div_by_zero_refused_not_crash(self):
        src = 'set /a "q=5/0"\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set /a "n=2*3+1"\r\n')


class TestFoldForLoops(unittest.TestCase):
    f = staticmethod(_fn('bat_fold_for_loops', 'fold_for_loops'))

    def test_accumulator_in_list(self):
        out, stats = self.f('setlocal EnableDelayedExpansion\r\nset "ACC="\r\n'
                            'for %%i in (a b c) do (set "ACC=!ACC!%%i")\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "ACC=abc"', out)

    def test_for_l_range(self):
        out, stats = self.f('setlocal EnableDelayedExpansion\r\nset "S=abcdefgh"\r\n'
                            'set "ACC="\r\nfor /l %%i in (0,1,4) do (set "ACC=!ACC!!S:~%%i,1!")\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "ACC=abcde"', out)

    def test_non_integer_bounds_refused(self):
        src = 'set "ACC="\r\nfor /l %%i in (0,1,%DYN%) do (set "ACC=!ACC!%%i")\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)


class TestPropagateConstants(unittest.TestCase):
    f = staticmethod(_fn('bat_propagate_constants', 'propagate_constants'))

    def test_reused_name_flow_sensitive(self):
        out, _ = self.f('set "M=first"\r\necho %M%\r\nset "M=second"\r\necho %M%\r\n')
        self.assertIn('echo first', out)
        self.assertIn('echo second', out)

    def test_modifier_ref_left_for_fold_substrings(self):
        src = 'set "S=abc"\r\necho %S:~0,1%\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_unresolvable_value_not_substituted(self):
        # X derives from an ambient (Unknown) var -> its own reads stay put
        src = 'set "X=%RANDOM%-tag"\r\necho %X%\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set "A=x"\r\necho %A% %A%\r\n')


if __name__ == '__main__':
    unittest.main()
