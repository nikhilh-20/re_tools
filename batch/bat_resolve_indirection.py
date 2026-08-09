#!/usr/bin/env python3
"""Resolves `call`-mediated indirect variable reads -- the Batch analogue of
PsResolve-Reflection (MITRE ATT&CK T1027.007-style indirection). Exposes
what a `call set "X=%%!Y!%%"` idiom is actually reading: `%%` collapses to a
literal `%` and `!Y!` delayed-expands to Y's current value (say, REALNAME)
in the FIRST expansion round every statement gets; the `call` prefix then
triggers a documented SECOND percent-expansion round that reads `%REALNAME%`
-- Y's value used AS a variable NAME, not as data. This pass computes that
first round directly (it does not need `call`'s second round to do so -- it
already knows exactly what round 2 would see) and rewrites the statement to
the plain, direct form with `call` and the %%/! wrapping removed, e.g.
`set "X=%REALNAME%"`. It does NOT also inline REALNAME's own value -- that
direct reference is now ordinary literal source text, picked up naturally by
bat_fold_concat.py / bat_inline_constants.py on a later pass, the same
"expose, don't over-reach" division PsResolve-Reflection uses.

Only fires when the statement's own tokens (right after `call`) contain at
least one PCT_LIT (%%) or BANG_CAND (!...!) -- the raw material `call`'s
extra round is actually needed to unwrap. A `call` with no such material
(calling an external label/program/command) is left untouched.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate, _expand_mixed
from batdeoblib.env import Env


def resolve_indirection(text: str, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    edits: list[tuple[int, int, str]] = []
    changed = 0

    for step in simulate(tree, Env()):
        full = [t for t in step.stmt.tokens if t.kind != TokenKind.NEWLINE]
        if not full or full[0].kind != TokenKind.TEXT or full[0].value.lstrip('@').upper() != 'CALL':
            continue
        # Skip "call" and exactly the run of whitespace immediately after
        # it -- everything else (including inner whitespace) is preserved
        # by expanding the FULL token slice, not code_tokens() (which
        # strips WS and would silently collapse spacing in the rewrite).
        j = 1
        while j < len(full) and full[j].kind == TokenKind.WS:
            j += 1
        rest = full[j:]
        if not rest or not any(t.kind in (TokenKind.PCT_LIT, TokenKind.BANG_CAND) for t in rest):
            continue
        # `call :label ...` is a real subroutine invocation, not the
        # %%/!-collapsing indirection idiom -- removing `call` here would
        # change behavior (":label" alone isn't a runnable command), not
        # just clean up spelling. Leave it untouched.
        if rest[0].kind == TokenKind.LABEL or (rest[0].kind == TokenKind.TEXT and rest[0].value.startswith(':')):
            continue

        r = _expand_mixed(rest, step.env, step.pct_env, step.stmt.in_block)
        if not r.ok:
            continue
        if not r.text.strip():
            continue

        # Remove the now-unnecessary `call` wrapper too: round-1 already
        # exposed the direct %NAME% reference, which resolves correctly on
        # its own without `call`'s extra round.
        start = full[0].start
        end = rest[-1].end
        edits.append((start, end, r.text))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(resolve_indirection, description='Resolve call-mediated indirect variable reads to their direct form.')
