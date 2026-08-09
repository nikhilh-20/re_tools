#!/usr/bin/env python3
"""Canonicalizes `set X=Y` (bare, unquoted) to `set "X=Y"` (quoted) where --
and ONLY where -- doing so is provably meaning-preserving.

This is NOT a purely cosmetic rewrite the way it looks: `set X=Y&Z` (bare)
runs `set X=Y` and then, as a SEPARATE command, `Z` -- the `&` is grammar-
significant. Wrapping the whole thing in quotes (`set "X=Y&Z"`) would fuse
those two into one assignment whose value literally contains `&Z`, changing
what the script does. This pass only quotes an assignment when its
right-hand side, as tokenized, contains no grammar-significant OP token
outside of quotes -- i.e. the bare form was already being treated as a
single argument with no such splitting, and wrapping it changes nothing
except making trailing-whitespace handling explicit and the read easier.
`set "X=Y"` input is already canonical and is never touched.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten, Statement


def _is_bare_set(stmt: Statement) -> tuple[int, int] | None:
    """If *stmt* is a bare `set NAME=VALUE` (no leading quote, not `/a`, `/p`),
    return (name_start, value_end) token-stream indices to quote, else None."""
    ct = stmt.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT:
        return None
    if ct[0].value.lstrip('@').upper() != 'SET':
        return None
    rest = [t for t in ct[1:]]
    if not rest:
        return None
    if rest[0].kind == TokenKind.QUOTE:
        return None   # already quoted -- canonical
    if rest[0].kind == TokenKind.TEXT and rest[0].value.startswith('/'):
        return None   # /a, /p and other switches -- out of scope

    # The assignment body must contain no grammar-significant OP token (an
    # OP token can only ever appear here if it was outside quotes AND not
    # caret-escaped, by tokenizer construction -- exactly the unsafe case).
    for t in rest:
        if t.kind == TokenKind.OP:
            return None

    if not any('=' in t.value for t in rest if t.kind == TokenKind.TEXT):
        return None   # not even a literal '=' present in plain text -- bail conservatively

    return rest[0].start, rest[-1].end


def normalize_set(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    stmts = flatten(tree)

    edits: list[tuple[int, int, str]] = []
    changed = 0
    for s in stmts:
        span = _is_bare_set(s)
        if span is None:
            continue
        start, end = span
        body = text[start:end]
        edits.append((start, end, f'"{body}"'))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(normalize_set, description='Quote bare `set NAME=VALUE` assignments where provably safe.')
