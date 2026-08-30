#!/usr/bin/env python3
"""Flow-sensitive constant propagation for bare `%VAR%` / `!VAR!` reads (no
`:~`/`:find=repl` modifier -- those are bat_fold_substrings.py's and
bat_fold_strsub.py's job respectively). The Batch analogue of
PsPropagate-Constants / vbs_propagate_constants: a variable name REUSED to
hold a different constant before each read is the general form that defeats
single-static-assignment inlining (bat_inline_constants.py's blind spot).

Built directly on the shared simulator (batdeoblib.simulate), which already
walks the script in source order tracking a Known/Unknown/Unset value per
variable and correctly distinguishes %-refs (block-entry snapshot) from
!-refs (live value) -- this pass is a thin consumer that substitutes
wherever that simulation proves a value.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate, _statement_names_written
from batdeoblib.env import Env


def propagate_constants(text: str, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    edits: list[tuple[int, int, str]] = []
    changed = 0

    for step in simulate(tree, Env()):
        # a `%P%` on the RHS of `set "P=%P%chunk"` is the accumulator link --
        # substituting its ever-growing value into every link is O(n^2) edit
        # text (the blow-up vbs_propagate_constants documents). Leave those to
        # a dedicated concat/collapse pass.
        self_targets = _statement_names_written(step.stmt)
        for tok in step.stmt.tokens:
            if tok.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                continue
            inner = tok.inner or ''
            if ':' in inner:
                continue   # has a modifier -- bat_fold_substrings/strsub's job
            name = inner
            if name.strip().upper() in self_targets:
                continue
            if tok.kind == TokenKind.BANG_CAND and not step.env.delayed_expansion:
                continue
            resolve_env = step.pct_env if tok.kind == TokenKind.PCT_VAR else step.env
            val = resolve_env.resolve_read(name)
            if val is None:
                continue
            if '%' in val or '!' in val:
                continue   # would risk forming a new expansion once re-inlined
            edits.append((tok.start, tok.end, val))
            changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(propagate_constants, description='Flow-sensitive substitution of bare %VAR%/!VAR! reads with their known value.')
