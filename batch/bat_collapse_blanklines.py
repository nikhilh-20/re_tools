#!/usr/bin/env python3
"""Cosmetic cleanup: removes lines that are only whitespace, and collapses
runs of 3+ blank lines down to one. The Batch analogue of
PsCollapse-BlankLines. Run it last, after the fold/removal passes, which
tend to leave empty lines behind.

Quote-aware: a blank-looking line that is actually inside an open multi-line
quoted string (rare in Batch, but possible via caret line-continuation
inside quotes) is never touched -- guarded by the tokenizer's per-token
in_quotes flag, the same style of protection PsCollapse-BlankLines uses for
here-strings.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool
from batdeoblib.tokenizer import tokenize, TokenKind

_WS_ONLY_RE = re.compile(r'^[ \t]+$')   # whitespace-only, i.e. non-empty but all blank
_EMPTY_RE = re.compile(r'^$')


def collapse_blanklines(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)

    # Map: line index -> protected (any token starting on that line, or its
    # terminating NEWLINE, was lexed while inside a quoted span).
    protected: set[int] = set()
    line_idx = 0
    for tok in tokens:
        if tok.in_quotes:
            protected.add(line_idx)
        if tok.kind == TokenKind.NEWLINE:
            if tok.in_quotes:
                protected.add(line_idx)
            line_idx += 1

    lines = text.splitlines(keepends=True)

    # Stage 1: delete whitespace-only (non-empty) lines outright, carrying
    # each survivor's original line index along for stage 2's protection check.
    stage1: list[tuple[str, int]] = []
    ws_removed = 0
    for i, line in enumerate(lines):
        stripped = line.rstrip('\r\n')
        if i not in protected and _WS_ONLY_RE.match(stripped):
            ws_removed += 1
            continue
        stage1.append((line, i))

    # Stage 2: squeeze runs of 3+ consecutive genuinely-empty, UNPROTECTED
    # lines to one. A protected empty line (inside an open quote span) is
    # never merged into -- or absorbed by -- a squeeze, so it always breaks
    # the run, the same as any other non-blank content would.
    out: list[str] = []
    run: list[str] = []
    squeezed_runs = 0
    squeezed_lines = 0

    def flush_run():
        nonlocal squeezed_runs, squeezed_lines
        if len(run) >= 3:
            out.append(run[0])
            squeezed_runs += 1
            squeezed_lines += len(run) - 1
        else:
            out.extend(run)
        run.clear()

    for line, orig_idx in stage1:
        stripped = line.rstrip('\r\n')
        if orig_idx not in protected and _EMPTY_RE.match(stripped):
            run.append(line)
        else:
            flush_run()
            out.append(line)
    flush_run()

    changed = ws_removed + squeezed_lines
    return ''.join(out), {
        'changed': changed,
        'whitespace_lines_removed': ws_removed,
        'blank_runs_squeezed': squeezed_runs,
    }


if __name__ == '__main__':
    run_tool(collapse_blanklines, description='Squeeze whitespace-only lines and runs of 3+ blank lines.')
