#!/usr/bin/env python3
"""Inlines variables assigned EXACTLY ONCE (anywhere in the script) with a
constant, at unconditional top level, then removes the now-dead assignment.
The Batch analogue of PsInline-Constants / vbs_inline_constants -- the
simple single-static-assignment case (`set "path=calc.exe"` used later as
`%path%`). A variable reassigned more than once is bat_propagate_constants.py's
job instead (which substitutes reads but, correctly, never deletes any of
the several assignments that give it meaning).

`--max-uses N` caps how many read sites get inlined (0/unset = unlimited,
matching PsInline-Constants' -MaxUses convention). The assignment is deleted
only when every one of its reads was actually inlined (an uncapped read left
behind would make the deletion a real behavior change, not a cleanup).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten, Statement
from batdeoblib.simulate import simulate, _quote_stripped_set_value
from batdeoblib.env import Env


def inline_constants(text: str, *, max_uses: int = 0, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    stmts = flatten(tree)

    assign_count: dict[str, int] = {}
    single_assign_stmt: dict[str, Statement] = {}
    single_assign_value: dict[str, str] = {}

    for step in simulate(tree, Env()):
        ct = step.stmt.code_tokens()
        if not ct or ct[0].kind != TokenKind.TEXT or ct[0].value.lstrip('@').upper() != 'SET':
            continue
        rest = ct[1:]
        if rest and rest[0].kind == TokenKind.TEXT and rest[0].value.startswith('/'):
            continue
        r = _quote_stripped_set_value(step.stmt, step.env, step.pct_env)
        if r is None:
            continue
        name, expanded = r
        key = name.upper()
        assign_count[key] = assign_count.get(key, 0) + 1
        if assign_count[key] == 1 and not step.stmt.in_block and expanded.ok:
            single_assign_stmt[key] = step.stmt
            single_assign_value[key] = expanded.text
        else:
            single_assign_stmt.pop(key, None)
            single_assign_value.pop(key, None)

    eligible = {k: v for k, v in single_assign_value.items() if assign_count.get(k) == 1}

    edits: list[tuple[int, int, str]] = []
    changed = 0
    assignments_removed = 0
    uses_per_var: dict[str, int] = {}

    # Count every read site up front (whether or not it ends up inlined) so
    # deletion eligibility can be a simple count comparison afterward --
    # re-scanning tokens post-hoc would see the ORIGINAL (pre-edit) tokens,
    # since queued edits aren't applied to the tree until the very end.
    total_reads: dict[str, int] = {}
    for s in stmts:
        for tok in s.tokens:
            if tok.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                continue
            key = (tok.inner or '').split(':', 1)[0].upper()
            if key in eligible and s is not single_assign_stmt.get(key):
                total_reads[key] = total_reads.get(key, 0) + 1

    for s in stmts:
        for tok in s.tokens:
            if tok.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                continue
            inner = tok.inner or ''
            name = inner.split(':', 1)[0]
            key = name.upper()
            if key not in eligible:
                continue
            if s is single_assign_stmt.get(key):
                continue   # don't touch the assignment's own RHS reference to itself (n/a in practice, but safe)
            if ':' in inner:
                continue   # has a modifier -- leave for bat_fold_substrings/strsub, which resolve it just as well
            val = eligible[key]
            if '%' in val or '!' in val:
                continue
            if max_uses and uses_per_var.get(key, 0) >= max_uses:
                continue
            edits.append((tok.start, tok.end, val))
            uses_per_var[key] = uses_per_var.get(key, 0) + 1
            changed += 1

    # Only delete an assignment once EVERY read of it (regardless of reason
    # -- modifier present, or capped by --max-uses) was actually inlined.
    for key, total in total_reads.items():
        if uses_per_var.get(key, 0) != total:
            continue
        stmt = single_assign_stmt[key]
        body_tokens = [t for t in stmt.tokens if t.kind != TokenKind.NEWLINE]
        if not body_tokens:
            continue
        edits.append((body_tokens[0].start, body_tokens[-1].end, ''))
        assignments_removed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed, 'assignments_removed': assignments_removed}


if __name__ == '__main__':
    run_tool(
        inline_constants,
        description='Inline single-static-assignment constant variables and remove the dead assignment.',
        extra_args=[{'flags': ['--max-uses'], 'type': int, 'default': 0, 'dest': 'max_uses'}],
    )
