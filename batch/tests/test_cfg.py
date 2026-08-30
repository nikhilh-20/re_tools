"""batdeoblib.cfg -- label table + goto/call edge extraction. Case-insensitive
labels; a leading `:` on a goto arg is optional; `if COND goto X` embeds the
keyword after the condition; a non-literal target is flagged, never guessed;
malformed input never raises.
"""
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.tokenizer import tokenize
from batdeoblib.statements import parse_script
from batdeoblib.cfg import build_cfg


def _cfg(src):
    return build_cfg(parse_script(tokenize(src)))


class TestLabels(unittest.TestCase):
    def test_label_table_normalized_uppercase(self):
        c = _cfg(':Start\r\necho x\r\n:done\r\n')
        self.assertEqual(set(c.labels), {'START', 'DONE'})

    def test_first_definition_wins(self):
        c = _cfg(':dup\r\necho a\r\n:dup\r\necho b\r\n')
        self.assertEqual(len(c.labels), 1)

    def test_label_first_word_only(self):
        c = _cfg(':loop extra text here\r\ngoto loop\r\n')
        self.assertIn('LOOP', c.labels)
        self.assertEqual([g.target for g in c.gotos], ['LOOP'])

    def test_bare_colon_does_not_crash(self):
        _cfg(':\r\necho x\r\n::\r\n')   # must not raise


class TestGotoEdges(unittest.TestCase):
    def test_literal_goto_resolves(self):
        c = _cfg('goto :real\r\n:real\r\necho hi\r\n')
        self.assertEqual([(g.target, g.is_call) for g in c.gotos], [('REAL', False)])

    def test_optional_leading_colon(self):
        c1 = _cfg('goto real\r\n:real\r\n')
        c2 = _cfg('goto :real\r\n:real\r\n')
        self.assertEqual([g.target for g in c1.gotos], [g.target for g in c2.gotos])

    def test_goto_eof_marked(self):
        c = _cfg('echo x\r\ngoto :eof\r\n')
        self.assertEqual([g.target for g in c.gotos], ['EOF'])

    def test_computed_goto_flagged_not_guessed(self):
        c = _cfg('goto %target%\r\n')
        self.assertEqual(len(c.gotos), 1)
        self.assertIsNone(c.gotos[0].target)
        self.assertIsNotNone(c.gotos[0].reason)

    def test_if_cond_goto_embeds_keyword(self):
        c = _cfg('if "1"=="1" goto win\r\n:win\r\n')
        self.assertEqual([g.target for g in c.gotos], ['WIN'])

    def test_call_label_edge(self):
        c = _cfg('call :sub\r\n:sub\r\ngoto :eof\r\n')
        self.assertTrue(any(g.is_call and g.target == 'SUB' for g in c.gotos))

    def test_call_external_program_not_an_edge(self):
        c = _cfg('call other.bat arg\r\n')
        self.assertEqual(c.gotos, [])

    def test_target_label_not_found(self):
        c = _cfg('goto :nowhere\r\n')
        self.assertIsNone(c.gotos[0].target)
        self.assertIn('not found', c.gotos[0].reason)


if __name__ == '__main__':
    unittest.main()
