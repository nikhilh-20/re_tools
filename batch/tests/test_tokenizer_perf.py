"""H5 -- the tokenizer must be linear, not O(n^2), on large inputs.

Every newline / % / ! / comment / caret branch used to slice the tail of the
source (`re.match(..., src[pos:])`, `re.search(r'\r?\n', src[pos+1:])`), and
the % / ! branches re-scanned to the next newline on *every* delimiter. On a
multi-MB one-liner that is quadratic. Fixes: anchored matching against the whole
string with a `pos` argument; a cached per-line line-end offset; a compiled
character class for the text run instead of a Python per-char loop.

Correctness first, then a wall-clock bound. The broken form is quadratic (many
minutes at 2 MB); the fixed form is linear (a couple of seconds, most of it
Python object creation). A generous 10 s bound still separates them by orders
of magnitude while tolerating a slow/loaded machine.
"""
import time
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.tokenizer import tokenize, TokenKind

_SIZE = 2 * 1024 * 1024
_BOUND = 10.0


class TestSmallInputCorrectness(unittest.TestCase):
    def test_lone_percent_no_partner_is_unmatched(self):
        toks = tokenize('echo %undefined')
        self.assertEqual(sum(t.kind == TokenKind.PCT_UNMATCH for t in toks), 1)

    def test_matched_percent_pair_is_a_var(self):
        toks = [t for t in tokenize('%A% %B%') if t.kind == TokenKind.PCT_VAR]
        self.assertEqual([t.inner for t in toks], ['A', 'B'])

    def test_bang_pair_is_a_candidate(self):
        self.assertEqual(sum(t.kind == TokenKind.BANG_CAND for t in tokenize('!a! !b!')), 2)

    def test_quoted_text_run_is_one_token(self):
        toks = [t for t in tokenize('"aaaa bbbb cccc"') if t.kind == TokenKind.TEXT]
        self.assertEqual([t.value for t in toks], ['aaaa', 'bbbb', 'cccc'])

    def test_operators_still_split_outside_quotes(self):
        toks = tokenize('a&b&&c||d|e')
        self.assertEqual([t.value for t in toks if t.kind == TokenKind.OP], ['&', '&&', '||', '|'])

    def test_operators_are_literal_inside_quotes(self):
        toks = [t for t in tokenize('"a&b|c"') if t.kind == TokenKind.TEXT]
        self.assertEqual([t.value for t in toks], ['a&b|c'])


class TestLargeInputPerformance(unittest.TestCase):
    def _timed(self, src):
        t0 = time.perf_counter()
        toks = tokenize(src)
        return toks, time.perf_counter() - t0

    def test_many_var_pairs_on_one_line(self):
        src = '%V% ' * (_SIZE // 4)
        toks, dt = self._timed(src)
        self.assertLess(dt, _BOUND, f'{dt:.2f}s')
        self.assertGreater(sum(t.kind == TokenKind.PCT_VAR for t in toks), 0)

    def test_many_bang_pairs_on_one_line(self):
        src = '!V! ' * (_SIZE // 4)
        _toks, dt = self._timed(src)
        self.assertLess(dt, _BOUND, f'{dt:.2f}s')

    def test_many_short_lines_each_a_lone_percent(self):
        src = '%\n' * (_SIZE // 2)
        toks, dt = self._timed(src)
        self.assertLess(dt, _BOUND, f'{dt:.2f}s')
        self.assertGreater(sum(t.kind == TokenKind.PCT_UNMATCH for t in toks), 0)

    def test_one_huge_quoted_run(self):
        src = '"' + 'a' * _SIZE + '"'
        toks, dt = self._timed(src)
        self.assertLess(dt, _BOUND, f'{dt:.2f}s')
        self.assertEqual(sum(t.kind == TokenKind.TEXT for t in toks), 1)

    def test_huge_rem_line(self):
        src = 'rem ' + 'z' * _SIZE
        toks, dt = self._timed(src)
        self.assertLess(dt, _BOUND, f'{dt:.2f}s')
        self.assertEqual(sum(t.kind == TokenKind.COMMENT for t in toks), 1)


if __name__ == '__main__':
    unittest.main()
