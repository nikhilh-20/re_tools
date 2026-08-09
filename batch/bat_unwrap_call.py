#!/usr/bin/env python3
"""Resolves a statement whose PROGRAM ITSELF -- not just one of its
arguments -- is hidden behind a variable: `set "X=powershell -c calc"`
followed later by a bare `%X%` statement that invokes it. The Batch analogue
of vbs_unwrap_execute's "hidden statement" unwrapping, adapted to Batch's
actual indirection idiom (there is no `Execute "<string>"` dynamic-eval
construct in Batch the way VBScript has one -- the equivalent obfuscation is
holding a whole command line in a variable and invoking the bare reference).

Distinct from bat_propagate_constants.py (which substitutes individual
%VAR%/!VAR! reads wherever they appear) and bat_fold_concat.py (which merges
adjacent resolvable pieces): this pass specifically targets the STATEMENT-
level shape where the identity of the program being run is itself the
indirection, resolving the whole statement in one shot -- including a
chained case (`%CMD1%` resolves to literal text that is ITSELF another
`%CMD2%`-shaped reference) that per-token folding only reaches after a
second pass.
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

_MAX_ROUNDS = 4


def unwrap_call(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)

    edits: list[tuple[int, int, str]] = []
    changed = 0

    for step in simulate(tree, Env()):
        full = [t for t in step.stmt.tokens if t.kind != TokenKind.NEWLINE]
        first_code = next((t for t in full if t.kind != TokenKind.WS), None)
        if first_code is None or first_code.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
            continue

        cur_tokens = full
        resolved_text = None
        for _round in range(_MAX_ROUNDS):
            r = _expand_mixed(cur_tokens, step.env, step.pct_env, step.stmt.in_block)
            if not r.ok:
                break
            reretok = tokenize(r.text)
            still_indirect = bool(reretok) and any(
                t.kind in (TokenKind.PCT_VAR, TokenKind.BANG_CAND, TokenKind.PCT_LIT) for t in reretok[:2]
            )
            resolved_text = r.text
            if not still_indirect:
                break
            cur_tokens = reretok

        if resolved_text is None or not resolved_text.strip():
            continue
        if resolved_text == ''.join(t.value for t in full):
            continue   # no actual change
        edits.append((full[0].start, full[-1].end, resolved_text))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(unwrap_call, description='Resolve a statement whose program itself is a variable reference, to its literal form.')
