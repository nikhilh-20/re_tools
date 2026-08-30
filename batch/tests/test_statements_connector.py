"""batdeoblib.statements -- the Statement/Block splitter.

Batch's terminator model is NEWLINE + the connectors `& && || |`. Unlike VBS
(where a trailing COLON had to be stripped from the content view), batch
code_tokens() already excludes NEWLINE and the connector is a *separate*
field. What this locks down: a connector run splits into distinct statements
with clean code_tokens() and the right connector_before; a connector inside
quotes or a `%VAR:...%` modifier or a `(...)` block is never a split point.
"""
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, Statement, Block, flatten


def _stmts(src):
    return flatten(parse_script(tokenize(src)))


def _words(stmt):
    return [t.value for t in stmt.code_tokens()]


class TestConnectorSplitting(unittest.TestCase):
    def test_amp_splits_into_two_clean_statements(self):
        s = _stmts('echo a & echo b\r\n')
        self.assertEqual(len(s), 2)
        self.assertEqual(_words(s[0]), ['echo', 'a'])
        self.assertEqual(_words(s[1]), ['echo', 'b'])

    def test_connector_before_is_recorded_not_in_content(self):
        s = _stmts('set "X=1" && echo ok || echo fail\r\n')
        self.assertEqual([st.connector_before for st in s], [None, '&&', '||'])
        for st in s:
            self.assertNotIn('&&', _words(st))
            self.assertNotIn('||', _words(st))

    def test_connector_form_matches_newline_form(self):
        conn = [_words(x) for x in _stmts('echo a & echo b & echo c\r\n')]
        nl = [_words(x) for x in _stmts('echo a\r\necho b\r\necho c\r\n')]
        self.assertEqual(conn, nl)

    def test_amp_inside_quotes_is_not_a_split(self):
        s = _stmts('echo "a & b"\r\n')
        self.assertEqual(len(s), 1)

    def test_amp_inside_strsub_modifier_is_not_a_split(self):
        # %V:&=x% -- the & is inside the modifier, not a connector
        s = _stmts('set "V=p&q"\r\necho %V:&=-%\r\n')
        self.assertEqual(len(s), 2)

    def test_pipe_splits(self):
        s = _stmts('type f | findstr x\r\n')
        self.assertEqual(len(s), 2)
        self.assertEqual(s[1].connector_before, '|')


class TestBlocks(unittest.TestCase):
    def test_paren_block_body_split_internally_connectors_on_children(self):
        tree = parse_script(tokenize('( echo a & echo b )\r\n'))
        blocks = [n for n in tree if isinstance(n, Block)]
        self.assertEqual(len(blocks), 1)
        body = flatten(blocks[0].body)
        self.assertEqual([_words(x) for x in body], [['echo', 'a'], ['echo', 'b']])

    def test_span_covers_raw_text_including_connector_region(self):
        src = 'echo a & echo b\r\n'
        s = _stmts(src)
        # the second statement's own span starts at its first token (after the &)
        self.assertTrue(src[s[1].start:s[1].end].strip().startswith('echo b'))

    def test_unbalanced_parens_do_not_raise(self):
        parse_script(tokenize('if 1==1 ( echo x\r\n'))   # missing close
        parse_script(tokenize('echo x ) )\r\n'))          # extra close

    def test_for_do_block_group(self):
        tree = parse_script(tokenize('for %%i in (1 2 3) do (\r\necho %%i\r\n)\r\n'))
        self.assertTrue(any(isinstance(n, Block) for n in tree))


if __name__ == '__main__':
    unittest.main()
