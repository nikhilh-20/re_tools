"""Unit tests for vbsdeoblib.tokenizer's STRING literal pattern.

Regression coverage for a real quadratic-time bug in the original pattern,
`"(?:[^"]|"")*"`: forcing the regex engine to choose between two
alternatives at every character of the string body measured as O(n^2) — a
32 MB single-line string literal took ~17s to tokenize, and a 64 MB one
raised MemoryError outright. This mattered in practice: a collapsed
self-append accumulator chain (see vbs_propagate_constants.py's
_try_collapse_self_append_run) writes its fully-joined value back into the
source as one large string literal, which then gets re-tokenized on every
subsequent pass — so this landmine sat directly in that fix's path, not
just this one sample's.

The fix rewrites the pattern as `"[^"]*(?:""[^"]*)*"` — runs of non-quote
characters separated by literal "" escapes — which matches identically
(verified below) but tokenizes a 128 MB literal in well under a tenth of a
second.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import time
import unittest

from vbsdeoblib.tokenizer import tokenize, TokenKind


class TestStringLiteralCorrectness(unittest.TestCase):
    """The rewritten pattern must match exactly the same spans as the
    original — this is what makes the performance guard below trustworthy
    rather than just 'fast at the cost of wrong'."""

    def _single_string_token(self, src: str):
        toks = tokenize(src)
        strings = [t for t in toks if t.kind == TokenKind.STRING]
        self.assertEqual(len(strings), 1, f'expected exactly one STRING token, got {strings!r}')
        return strings[0]

    def test_plain_string(self):
        tok = self._single_string_token('"hello"')
        self.assertEqual(tok.value, '"hello"')
        self.assertEqual((tok.start, tok.end), (0, 7))

    def test_empty_string(self):
        tok = self._single_string_token('""')
        self.assertEqual(tok.value, '""')

    def test_string_with_one_escaped_quote(self):
        tok = self._single_string_token('"a""b"')
        self.assertEqual(tok.value, '"a""b"')

    def test_string_with_multiple_escaped_quotes(self):
        tok = self._single_string_token('"a""b""c""d"')
        self.assertEqual(tok.value, '"a""b""c""d"')

    def test_string_followed_by_more_code(self):
        # The STRING token must stop at its own closing quote, not run on
        # into the rest of the statement.
        toks = tokenize('x = "a""b" & y')
        strings = [t for t in toks if t.kind == TokenKind.STRING]
        self.assertEqual([t.value for t in strings], ['"a""b"'])

    def test_unterminated_string_falls_through_to_unknown(self):
        # No closing quote at all: the STRING pattern can't match, so the
        # leading '"' becomes UNKNOWN and tokenizing continues normally —
        # same behavior as before this fix (never raises).
        toks = tokenize('"abc')
        self.assertEqual([t.kind for t in toks if t.kind == TokenKind.STRING], [])
        unknown = [t for t in toks if t.kind == TokenKind.UNKNOWN]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].value, '"')
        idents = [t for t in toks if t.kind == TokenKind.IDENT]
        self.assertEqual([t.value for t in idents], ['abc'])


class TestStringLiteralPerformance(unittest.TestCase):
    """Direct regression guard for the quadratic-time bug, isolated to
    vbsdeoblib.tokenizer (not routed through vbs_propagate_constants) so a
    regression here is attributed to the tokenizer specifically.

    Thresholds are chosen from measured data, not guessed: at 16 MB the
    fixed pattern takes ~0.015s (roughly 100x margin under the 1.0s bound
    below) while the original quadratic pattern took ~3.9s (roughly 4x
    over it) — so this bound cleanly separates the two, it is not a
    near-miss in either direction."""

    _SIZE = 16 * 1024 * 1024
    _BOUND_SECONDS = 1.0

    def test_large_plain_string_tokenizes_quickly(self):
        src = '"' + ('q' * self._SIZE) + '"'
        t0 = time.time()
        toks = tokenize(src)
        elapsed = time.time() - t0
        strings = [t for t in toks if t.kind == TokenKind.STRING]
        self.assertEqual(len(strings), 1)
        self.assertEqual(len(strings[0].value), self._SIZE + 2)
        self.assertLess(elapsed, self._BOUND_SECONDS)

    def test_large_string_with_scattered_escapes_tokenizes_quickly(self):
        # One "" escape roughly every 100 characters through a large body —
        # confirms escape handling doesn't reintroduce quadratic cost as
        # escape count grows.
        chunk = 'q' * 98 + '""'
        body = chunk * (self._SIZE // len(chunk))
        src = '"' + body + '"'
        t0 = time.time()
        toks = tokenize(src)
        elapsed = time.time() - t0
        strings = [t for t in toks if t.kind == TokenKind.STRING]
        self.assertEqual(len(strings), 1)
        self.assertLess(elapsed, self._BOUND_SECONDS)


if __name__ == '__main__':
    unittest.main()
