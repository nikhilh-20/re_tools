"""vbs_strip_comments — remove comment-only lines (token-stream driven).

A line that consists solely of a COMMENT token (and optional leading WS) is
deleted.  A comment after real code on the same line is preserved by default;
--include-trailing also strips those.

Analog of PsStrip-Comments.

Usage:
    python vbs_strip_comments.py --input in.vbs --output out.vbs [--include-trailing]
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool


def run(src: str, include_trailing: bool = False, **_) -> tuple[str, dict]:
    lines = src.splitlines(keepends=True)
    tokens = tokenize(src)

    # Build a set of line numbers (0-indexed) that contain real code tokens.
    # Also note which lines have *only* a comment (+ WS).
    from collections import defaultdict
    line_kinds: dict[int, set] = defaultdict(set)
    # Map each token to its line number.
    line_starts = [0]
    for ch in src:
        if ch == '\n':
            line_starts.append(line_starts[-1] + 1)

    # Simpler: find the line number of each token by counting newlines.
    newline_offsets: list[int] = []
    pos = 0
    for ch in src:
        if ch == '\n':
            newline_offsets.append(pos)
        pos += 1

    def line_of(offset: int) -> int:
        lo, hi = 0, len(newline_offsets)
        while lo < hi:
            mid = (lo + hi) // 2
            if newline_offsets[mid] < offset:
                lo = mid + 1
            else:
                hi = mid
        return lo

    for tok in tokens:
        ln = line_of(tok.start)
        if tok.kind == TokenKind.WS:
            continue
        elif tok.kind == TokenKind.COMMENT:
            line_kinds[ln].add('comment')
        elif tok.kind == TokenKind.NEWLINE:
            pass
        else:
            line_kinds[ln].add('code')

    # Decide which lines to drop.
    out_lines: list[str] = []
    removed = 0
    for ln, line in enumerate(lines):
        kinds = line_kinds.get(ln, set())
        if kinds == {'comment'}:
            # Comment-only line — drop it.
            removed += 1
            continue
        if include_trailing and 'comment' in kinds and 'code' in kinds:
            # Strip the trailing comment from this line.
            tok_on_line = [t for t in tokens if line_of(t.start) == ln]
            comment_tok = next((t for t in tok_on_line if t.kind == TokenKind.COMMENT), None)
            if comment_tok:
                # Find where the comment starts in this line.
                line_start_offset = newline_offsets[ln-1]+1 if ln > 0 else 0
                col = comment_tok.start - line_start_offset
                stripped = line[:col].rstrip() + '\n'
                out_lines.append(stripped)
                removed += 1
                continue
        out_lines.append(line)

    new_src = ''.join(out_lines)
    return new_src, {'changed': removed, 'comment_lines_removed': removed}


if __name__ == '__main__':
    run_tool(run, description="Remove comment-only lines from VBScript source",
             extra_args=[{'flags': ['--include-trailing'], 'action': 'store_true',
                          'default': False,
                          'help': 'Also strip trailing end-of-line comments'}])
