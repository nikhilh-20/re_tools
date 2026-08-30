"""bat_unwrap_trueif -- collapse a statically-decidable `if` to the branch
that runs. Covers both TRUE (pre-existing) and FALSE (new, mirrors the
statically-false-If sub-pass vbs_remove_deadcode carries).
"""
import unittest

from tests._harness import call_fn, assert_idempotent


def _run(src):
    return call_fn('bat_unwrap_trueif', 'unwrap_trueif', src)


class TestTrue(unittest.TestCase):
    def test_true_sameline_keeps_action(self):
        out, stats = _run('if "1"=="1" echo yes\r\necho after\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertNotIn('if ', out)
        self.assertIn('echo yes', out)

    def test_true_block_lifts_body_drops_else(self):
        out, _ = _run('if 5 GTR 3 (\r\necho taken\r\n) else (\r\necho skip\r\n)\r\n')
        self.assertIn('echo taken', out)
        self.assertNotIn('echo skip', out)

    def test_defined_true(self):
        out, stats = _run('set "X=1"\r\nif defined X echo has-x\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('echo has-x', out)


class TestFalse(unittest.TestCase):
    def test_false_sameline_removed(self):
        out, stats = _run('if 5 GTR 9 goto :dead\r\necho alive\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertNotIn('goto :dead', out)
        self.assertIn('echo alive', out)

    def test_false_block_no_else_removed(self):
        out, _ = _run('if "1"=="0" (\r\necho gone\r\n)\r\necho kept\r\n')
        self.assertNotIn('echo gone', out)
        self.assertIn('echo kept', out)

    def test_false_block_with_else_keeps_else(self):
        out, _ = _run('if "a"=="b" (\r\necho no\r\n) else (\r\necho yes-else\r\n)\r\n')
        self.assertNotIn('echo no', out)
        self.assertIn('echo yes-else', out)

    def test_defined_false(self):
        out, stats = _run('if defined NEVERSET echo x\r\necho y\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertNotIn('echo x', out)
        self.assertIn('echo y', out)

    def test_negated(self):
        out, _ = _run('if not "1"=="1" echo dead\r\necho live\r\n')
        self.assertNotIn('echo dead', out)


class TestLeavesUnresolvable(unittest.TestCase):
    def test_ambient_var_left_alone(self):
        # %RANDOM% is Unknown (real at runtime, unknowable statically) -- not
        # the same as a never-assigned var, which IS provably empty.
        src = 'if "%RANDOM%"=="12345" echo maybe\r\n'
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 0)
        self.assertEqual(out, src)

    def test_exist_left_alone(self):
        src = 'if exist "C:\\foo" echo there\r\n'
        out, stats = _run(src)
        self.assertEqual(stats['changed'], 0)


class TestIdempotent(unittest.TestCase):
    def test_idempotent(self):
        assert_idempotent(self, lambda s: _run(s),
                          'if "1"=="1" (\r\necho a\r\n)\r\nif 2 LSS 1 (\r\necho b\r\n)\r\n')


if __name__ == '__main__':
    unittest.main()
