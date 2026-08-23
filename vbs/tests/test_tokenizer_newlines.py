"""Unit tests for vbsdeoblib.tokenizer's handling of non-standard line
endings.

Regression coverage for the bug where a stray/duplicated \\r before \\r\\n
(seen in a real obfuscated sample — every line terminated \\r\\r\\n instead
of plain \\r\\n) fell through to an UNKNOWN token, since the NEWLINE pattern
was `\\r?\\n` and never matched a bare \\r. That stray UNKNOWN token became
the last token of the *preceding* statement's code_tokens(), which
resolve_const's parser then rejected as "leftover tokens, not a pure
expression" for every single statement in the file — constant propagation
silently folded nothing anywhere, with no error reported.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import unittest

from vbsdeoblib.tokenizer import tokenize, TokenKind
from vbsdeoblib.statements import split_statements


class TestBareCrNewline(unittest.TestCase):
    def test_crcrlf_produces_no_unknown_tokens(self):
        src = 'a = 1\r\r\nb = a\r\r\n'
        toks = tokenize(src)
        unknown = [t for t in toks if t.kind == TokenKind.UNKNOWN]
        self.assertEqual(unknown, [])

    def test_crcrlf_produces_two_newline_tokens_per_line_end(self):
        src = 'a = 1\r\r\nb = a\r\r\n'
        toks = tokenize(src)
        newlines = [t for t in toks if t.kind == TokenKind.NEWLINE]
        # One '\r' + one '\r\n' per line ending, two line endings total.
        self.assertEqual(len(newlines), 4)
        self.assertEqual([t.value for t in newlines], ['\r', '\r\n', '\r', '\r\n'])

    def test_bare_cr_alone_is_a_newline_not_unknown(self):
        # Old-Mac-style line ending: a lone \r with no following \n at all.
        src = 'a = 1\rb = a\r'
        toks = tokenize(src)
        unknown = [t for t in toks if t.kind == TokenKind.UNKNOWN]
        self.assertEqual(unknown, [])
        newlines = [t for t in toks if t.kind == TokenKind.NEWLINE]
        self.assertEqual([t.value for t in newlines], ['\r', '\r'])

    def test_plain_crlf_unaffected(self):
        src = 'a = 1\r\nb = a\r\n'
        toks = tokenize(src)
        newlines = [t for t in toks if t.kind == TokenKind.NEWLINE]
        self.assertEqual([t.value for t in newlines], ['\r\n', '\r\n'])
        self.assertEqual([t for t in toks if t.kind == TokenKind.UNKNOWN], [])

    def test_plain_lf_unaffected(self):
        src = 'a = 1\nb = a\n'
        toks = tokenize(src)
        newlines = [t for t in toks if t.kind == TokenKind.NEWLINE]
        self.assertEqual([t.value for t in newlines], ['\n', '\n'])
        self.assertEqual([t for t in toks if t.kind == TokenKind.UNKNOWN], [])

    def test_split_statements_same_shape_for_crcrlf_and_crlf(self):
        """The extra NEWLINE token from a \\r\\r\\n line ending produces one
        extra *empty* statement span per line (the lone \\r's own NEWLINE
        token, with no code tokens) — harmless, since every caller already
        does `if not ctoks: continue`. Once those empty spans are filtered
        out the same way callers do, the two token streams must carry
        identical code content in identical order."""
        src_crlf = 'a = 1\r\nb = a\r\nc = b\r\n'
        src_crcrlf = 'a = 1\r\r\nb = a\r\r\nc = b\r\r\n'

        stmts_crlf = split_statements(tokenize(src_crlf))
        stmts_crcrlf = split_statements(tokenize(src_crcrlf))

        code_crlf = [[t.value for t in s.code_tokens()] for s in stmts_crlf]
        code_crcrlf = [[t.value for t in s.code_tokens()] for s in stmts_crcrlf]
        code_crcrlf_nonempty = [c for c in code_crcrlf if c]
        self.assertEqual(code_crlf, code_crcrlf_nonempty)


if __name__ == '__main__':
    unittest.main()
