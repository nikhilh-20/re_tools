"""Unit tests for the content-vs-terminator invariant in
vbsdeoblib.statements.StatementSpan.

Regression coverage for the bug where code_tokens() included a trailing
COLON (the statement's terminator, not its content) while already excluding
the equivalent NEWLINE terminator. Every strict grammar that inspects
code_tokens() (declarator parsers, arity/last-token checks) saw trailing
garbage on a colon-joined statement and either bailed out as malformed
(silently keeping dead code alive) or misread the shape entirely (producing
invalid output — see the Const- and If-Then-colon cases below).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import unittest

from vbsdeoblib.tokenizer import tokenize, TokenKind
from vbsdeoblib.statements import split_statements, opens_block


class TestCodeTokensStripsColonTerminator(unittest.TestCase):

    def test_colon_terminated_statement_has_no_trailing_colon(self):
        stmts = split_statements(tokenize('Dim a: a = "xy"\n'))
        first = stmts[0].code_tokens()
        self.assertEqual([t.value for t in first], ['Dim', 'a'])
        self.assertNotEqual(first[-1].kind, TokenKind.COLON)

    def test_colon_form_matches_newline_form_code_tokens(self):
        colon_stmts = split_statements(tokenize('Dim a: a = 5\nWScript.Echo a\n'))
        newline_stmts = split_statements(tokenize('Dim a\na = 5\nWScript.Echo a\n'))
        colon_shapes = [[t.value for t in s.code_tokens()] for s in colon_stmts if s.code_tokens()]
        newline_shapes = [[t.value for t in s.code_tokens()] for s in newline_stmts if s.code_tokens()]
        self.assertEqual(colon_shapes, newline_shapes)

    def test_span_byte_range_still_covers_the_colon(self):
        src = 'Dim a: a = "xy"\n'
        stmt = split_statements(tokenize(src))[0]
        self.assertEqual(src[stmt.start:stmt.end], 'Dim a:')

    def test_colon_inside_parens_is_not_a_terminator_and_not_stripped(self):
        # A single statement spanning a call whose args happen to contain a
        # colon-like construct isn't realistic VBScript, but the guard is
        # about *paren depth* at split time, not this specific shape; assert
        # split_statements never splits inside parens regardless.
        stmts = split_statements(tokenize('Foo(1, 2)\n'))
        self.assertEqual(len(stmts), 1)
        ctoks = stmts[0].code_tokens()
        self.assertEqual([t.value for t in ctoks], ['Foo', '(', '1', ',', '2', ')'])


class TestEndsWithColon(unittest.TestCase):

    def test_true_for_colon_terminated_statement(self):
        stmt = split_statements(tokenize('Dim a: a = 5\n'))[0]
        self.assertTrue(stmt.ends_with_colon)

    def test_false_for_newline_terminated_statement(self):
        stmt = split_statements(tokenize('Dim a\n'))[0]
        self.assertFalse(stmt.ends_with_colon)

    def test_false_for_eof_terminated_statement_no_trailing_newline(self):
        stmt = split_statements(tokenize('Dim a'))[0]
        self.assertFalse(stmt.ends_with_colon)

    def test_true_even_with_trailing_comment_after_colon(self):
        # A comment can't actually follow a colon that ends the *span* (the
        # comment would be part of the next span or dangling), but guard the
        # "skip WS/COMMENT from the end" walk explicitly in case tokens ever
        # get handed out of the usual split_statements shape.
        stmt = split_statements(tokenize('Dim a:  \n'))[0]
        self.assertTrue(stmt.ends_with_colon)


class TestIfThenColonBlockDetection(unittest.TestCase):
    """Regression: opens_block() (and every hand-rolled copy of the same
    'last code token is THEN' check) must recognize a colon-joined one-liner
    'If cond Then:' as opening a multi-line block. Before the fix, the
    colon was code_tokens()'s last token instead of THEN, so this was
    misread as a single-line If and produced invalid output (block header
    deleted, matching End If left dangling)."""

    def test_if_then_colon_opens_block(self):
        stmts = split_statements(tokenize('If x Then:\nWScript.Echo 1\nEnd If\n'))
        ctoks = stmts[0].code_tokens()
        self.assertTrue(opens_block(ctoks))
        self.assertEqual(ctoks[-1].upper, 'THEN')


if __name__ == '__main__':
    unittest.main()
