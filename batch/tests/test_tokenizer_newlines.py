"""H4 -- non-standard line endings must not crash or poison the parse.

The newline branch was `m = re.match(r'\r?\n', src[pos:]); end = pos + m.end()`.
A lone '\r' (old-Mac) or the leading '\r' of a doubled '\r\r\n' never matched
`\r?\n`, so `m` was None and `m.end()` raised AttributeError -- breaking the
tokenizer's documented "Never raises" contract. Fix: NEWLINE = `\r\n|\r|\n`;
a '\r\r\n' yields two NEWLINE tokens.
"""
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script


def _nl_values(src):
    return [t.value for t in tokenize(src) if t.kind == TokenKind.NEWLINE]


def _kinds(tokens):
    return [t.kind for t in tokens]


class TestNewlineTokenizing(unittest.TestCase):
    def test_crcrlf_no_unknown_tokens(self):
        toks = tokenize('set "a=1"\r\r\nset "b=2"\r\r\n')
        self.assertNotIn(TokenKind.UNKNOWN, _kinds(toks))

    def test_crcrlf_two_newline_tokens_per_line_end(self):
        self.assertEqual(_nl_values('a=1\r\r\nb=a\r\r\n'), ['\r', '\r\n', '\r', '\r\n'])

    def test_bare_cr_alone_is_newline_not_unknown(self):
        toks = tokenize('set "a=1"\rset "b=2"\r')
        self.assertNotIn(TokenKind.UNKNOWN, _kinds(toks))
        self.assertEqual(_nl_values('a=1\rb=a\r'), ['\r', '\r'])

    def test_plain_crlf_unaffected(self):
        self.assertEqual(_nl_values('a=1\r\nb=2\r\n'), ['\r\n', '\r\n'])

    def test_plain_lf_unaffected(self):
        self.assertEqual(_nl_values('a=1\nb=2\n'), ['\n', '\n'])

    def test_tokenize_never_raises_on_lone_cr_at_eof(self):
        tokenize('echo hi\r')  # must not raise

    def test_caret_before_bare_cr_is_line_continuation(self):
        toks = tokenize('echo a^\rb\r')
        self.assertIn(TokenKind.LINECONT, _kinds(toks))


class TestStatementParityAcrossLineEndings(unittest.TestCase):
    def test_crcrlf_and_crlf_carry_identical_code(self):
        crlf = parse_script(tokenize('set "a=1"\r\nset "b=%a%"\r\n'))
        crcrlf = parse_script(tokenize('set "a=1"\r\r\nset "b=%a%"\r\r\n'))

        def code(tree):
            out = []
            for node in tree:
                toks = getattr(node, 'tokens', None)
                if toks is None:
                    continue
                words = [t.value for t in node.code_tokens()]
                if words:
                    out.append(words)
            return out

        self.assertEqual(code(crlf), code(crcrlf))


if __name__ == '__main__':
    unittest.main()
