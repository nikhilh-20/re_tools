#!/usr/bin/env python3
"""Remove identifier-splitting carets -- the ``p^o^w^e^r^s^h^e^l^l`` trick,
the direct Batch analogue of PsStrip-Backticks' backtick-splitting removal.

Only strips a caret when BOTH the character immediately before it and the
character it escapes are word characters ([A-Za-z0-9_]) -- this is the exact
condition under which removing the caret is guaranteed to be a no-op for
cmd.exe's grammar (word characters never carry special meaning, so joining
two of them across a caret can never create or destroy a grammar token).
A caret escaping a grammar-significant character (``^&``, ``^(``, ``^%``,
``^^``, ...) is always left untouched -- removing it would change what the
line parses as, which is not this pass's job.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind

_WORD = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_')


def strip_carets(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    edits: list[tuple[int, int, str]] = []
    removed = 0

    for i, tok in enumerate(tokens):
        if tok.kind != TokenKind.CARET_ESC:
            continue
        esc_char = tok.inner or ''
        if len(esc_char) != 1 or esc_char not in _WORD:
            continue
        # find the nearest preceding non-empty token's last character
        prev_char = ''
        for j in range(i - 1, -1, -1):
            if tokens[j].value:
                prev_char = tokens[j].value[-1]
                break
        if prev_char not in _WORD:
            continue
        edits.append((tok.start, tok.end, esc_char))
        removed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': removed, 'carets_removed': removed}


if __name__ == '__main__':
    run_tool(strip_carets, description='Remove identifier-splitting carets (^) outside quotes.')
