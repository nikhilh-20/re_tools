"""H3 -- batdeoblib.io.apply_edits: single-pass rebuild.

The old form did `src = src[:s] + r + src[e:]` once per edit -- O(edits x
filesize). On a multi-MB dropper with thousands of fold sites that is
quadratic. The rebuild is one left-to-right pass; genuinely overlapping edits
raise instead of silently nesting.
"""
import time
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.io import apply_edits


class TestCorrectness(unittest.TestCase):
    def test_no_edits_returns_source(self):
        self.assertEqual(apply_edits('abcdef', []), 'abcdef')

    def test_single_edit(self):
        self.assertEqual(apply_edits('abcdef', [(2, 4, 'XY')]), 'abXYef')

    def test_multiple_non_overlapping_unsorted(self):
        src = 'the quick brown fox'
        edits = [(10, 15, 'red'), (0, 3, 'THE'), (4, 9, 'slow')]
        self.assertEqual(apply_edits(src, edits), 'THE slow red fox')

    def test_adjacent_edits_ok(self):
        self.assertEqual(apply_edits('abcd', [(1, 2, 'X'), (2, 3, 'Y')]), 'aXYd')

    def test_zero_width_insert(self):
        self.assertEqual(apply_edits('abc', [(1, 1, 'INS')]), 'aINSbc')

    def test_deletion(self):
        self.assertEqual(apply_edits('abcdef', [(2, 4, '')]), 'abef')

    def test_partial_overlap_raises(self):
        with self.assertRaises(ValueError):
            apply_edits('abcdef', [(1, 4, 'X'), (2, 5, 'Y')])

    def test_nested_edit_outer_wins(self):
        # a pass that inlines %A%%B% into an assignment and then deletes the
        # whole now-dead assignment: the contained edits are dropped.
        src = 'set "X=%A%%B%"'
        edits = [(0, len(src), ''), (7, 10, 'foo'), (10, 13, 'bar')]
        self.assertEqual(apply_edits(src, edits), '')

    def test_exact_duplicate_edits_deduped(self):
        self.assertEqual(apply_edits('abcdef', [(2, 4, 'X'), (2, 4, 'X')]), 'abXef')

    def test_matches_naive_reference_on_random_edits(self):
        import random
        rng = random.Random(1234)
        src = ''.join(chr(0x61 + (i % 26)) for i in range(5000))
        # non-overlapping spans, left to right
        edits, cur = [], 0
        while cur < len(src) - 10:
            gap = rng.randint(1, 8)
            span = rng.randint(0, 4)
            s = cur + gap
            e = s + span
            edits.append((s, e, f'<{len(edits)}>'))
            cur = e
        rng.shuffle(edits)
        # naive right-to-left reference
        ref = src
        for s, e, r in sorted(edits, key=lambda x: x[0], reverse=True):
            ref = ref[:s] + r + ref[e:]
        self.assertEqual(apply_edits(src, edits), ref)


class TestPerformance(unittest.TestCase):
    def test_many_edits_on_large_string_is_fast(self):
        src = 'x' * (4 * 1024 * 1024)
        edits = [(i, i + 1, 'y') for i in range(0, 100_000 * 20, 20)]
        t0 = time.perf_counter()
        out = apply_edits(src, edits)
        dt = time.perf_counter() - t0
        self.assertEqual(len(out), len(src))
        self.assertLess(dt, 1.0, f'{dt:.2f}s for 100k edits on 4MB')


if __name__ == '__main__':
    unittest.main()
