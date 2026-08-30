"""The textual-normalization passes: strip_carets, expand_lines, normalize_set,
collapse_blanklines, strip_comments, strip_lines. Behaviour + idempotency.
"""
import unittest

from tests._harness import call_fn, run_cli, assert_idempotent


def _fn(mod, name):
    return lambda s, **k: call_fn(mod, name, s, **k)


class TestStripCarets(unittest.TestCase):
    f = staticmethod(_fn('bat_strip_carets', 'strip_carets'))

    def test_identifier_split_removed(self):
        out, stats = self.f('p^o^w^e^r^s^h^e^l^l -c calc\r\n')
        self.assertEqual(stats['changed'], 9)
        self.assertIn('powershell -c calc', out)

    def test_caret_escaping_grammar_char_kept(self):
        out, _ = self.f('echo a^&b\r\n')
        self.assertIn('a^&b', out)   # removing it would make two commands

    def test_caret_inside_quotes_kept(self):
        out, stats = self.f('echo "a^&b"\r\n')
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 's^e^t x=1\r\n')


class TestExpandLines(unittest.TestCase):
    f = staticmethod(_fn('bat_expand_lines', 'expand_lines'))

    def test_amp_connectors_onto_own_lines(self):
        out, _ = self.f('set a=1&set b=2&&echo ok||echo fail\r\n')
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(lines, ['set a=1', '& set b=2', '&& echo ok', '|| echo fail'])

    def test_connector_in_quotes_not_split(self):
        out, stats = self.f('echo "a & b"\r\n')
        self.assertEqual(stats['changed'], 0)

    def test_block_body_is_reindented(self):
        out, _ = self.f('for %%A in (1 2) do (echo %%A\r\necho hi)\r\n')
        self.assertIn('    echo %%A', out)


class TestNormalizeSet(unittest.TestCase):
    f = staticmethod(_fn('bat_normalize_set', 'normalize_set'))

    def test_bare_set_quoted(self):
        out, stats = self.f('set X=Y\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('set "X=Y"', out)

    def test_set_with_operator_not_fused(self):
        # quoting `set X=Y` is fine; the `&echo hi` must stay a separate command
        out, _ = self.f('set X=Y&echo hi\r\n')
        self.assertIn('set "X=Y"&echo hi', out)
        self.assertNotIn('set "X=Y&echo hi"', out)   # never fuse two commands

    def test_already_quoted_untouched(self):
        src = 'set "X=Y"\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set A=1\r\nset "B=2"\r\n')


class TestCollapseBlanklines(unittest.TestCase):
    f = staticmethod(_fn('bat_collapse_blanklines', 'collapse_blanklines'))

    def test_squeeze_run_and_strip_ws_only(self):
        out, _ = self.f('a=1\r\n\r\n\r\n\r\n   \r\n\r\nb=2\r\n')
        self.assertEqual(out, 'a=1\r\n\r\nb=2\r\n')

    def test_single_blank_kept(self):
        src = 'a=1\r\n\r\nb=2\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_bare_cr_blank_run_squeezed(self):
        out, stats = self.f('x\r\r\r\r\ny\r\n')
        self.assertGreater(stats['changed'], 0)
        self.assertNotIn('\r\r\r', out)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'a\r\n\r\n\r\n\r\nb\r\n')


class TestStripComments(unittest.TestCase):
    f = staticmethod(_fn('bat_strip_comments', 'strip_comments'))

    def test_rem_line_removed(self):
        out, stats = self.f('rem junk banner\r\necho hi\r\n')
        self.assertEqual(stats.get('rem_lines_removed', stats['changed']), 1)
        self.assertNotIn('junk banner', out)

    def test_dc_prose_removed_data_kept(self):
        src = ':: short note\r\n:: ' + 'a' * 80 + '\r\necho hi\r\n'
        out, _ = self.f(src)
        self.assertNotIn('short note', out)
        self.assertIn('a' * 80, out)   # data carrier kept by default

    def test_dc_data_removed_with_flag(self):
        src = ':: ' + 'a' * 80 + '\r\necho hi\r\n'
        out, _ = self.f(src, include_data=True)
        self.assertNotIn('a' * 80, out)

    def test_rem_inside_quotes_not_a_comment(self):
        src = 'echo "rem this is data"\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 0)

    def test_annotation_block_preserved(self):
        src = ('powershell -enc AAAA\r\n'
               'rem <<<EXEC PAYLOAD BEGIN>>>\r\n'
               'rem > Write-Host hi\r\n'
               'rem <<<EXEC PAYLOAD END>>>\r\n')
        out, _ = self.f(src)
        self.assertIn('rem > Write-Host hi', out)
        self.assertIn('<<<EXEC PAYLOAD BEGIN>>>', out)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'rem a\r\necho x\r\n:: b\r\n')


class TestStripLines(unittest.TestCase):
    def test_regex_filter_cli(self):
        src = 'rem GEN 1\r\nkeep me\r\nrem GEN 2\r\n'
        out, stats = run_cli('bat_strip_lines.py', src, '--pattern', r'^\s*rem GEN', '--flags', 'i')
        self.assertNotIn('rem GEN', out)
        self.assertIn('keep me', out)
        self.assertEqual(stats['removed_lines'], 2)


if __name__ == '__main__':
    unittest.main()
