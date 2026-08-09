#!/usr/bin/env python3
"""Collapses constant `set /a` arithmetic into its numeric literal. The
Batch analogue of PsFold-Arithmetic / vbs_fold_arithmetic -- defeats a byte
value or length spelled as junk math like `set /a "x=(18+18-(13-17))+32"`.

Supports `set /a`'s full documented grammar: `+ - * / % & | ^ ~ ! << >>`,
`0x`/leading-zero-octal literals, comma-separated multi-assignment, compound
assignment (`+=` etc, which reads the variable's OWN pre-assignment value as
an implicit left operand), AND `set /a`'s own bare-identifier variable-read
syntax (`set /a "x=y+1"` reads y directly, no %/! needed -- verified
empirically against cmd.exe; an unset or non-numeric variable contributes 0,
never a hard error, matching real `set /a` behavior).

Operates at the string level rather than per-token: inside a quoted
`set /a "..."` body, commas are swept into one ordinary TEXT token by the
tokenizer (there is no dedicated comma token to split on inside quotes), so
the whole body is expanded once, then split and evaluated item-by-item on
the resulting text. One unresolvable item does not block its siblings.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate, numeric_resolver, _split_compound_assignment, _expand_mixed
from batdeoblib.env import Env
from batdeoblib.resolver import eval_arith, ArithError


def fold_arithmetic(text: str, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    edits: list[tuple[int, int, str]] = []
    changed = 0
    by_reason: dict[str, int] = {}

    for step in simulate(tree, Env()):
        ct = step.stmt.code_tokens()
        if len(ct) < 2 or ct[0].kind != TokenKind.TEXT or ct[0].value.lstrip('@').upper() != 'SET':
            continue
        rest = ct[1:]
        if not (rest[0].kind == TokenKind.TEXT and rest[0].value.upper() == '/A'):
            continue
        body = rest[1:]
        if not body:
            continue

        wraps_whole = body[0].kind == TokenKind.QUOTE and body[-1].kind == TokenKind.QUOTE and len(body) > 1
        inner = body[1:-1] if wraps_whole else body
        if not inner:
            continue

        expanded = _expand_mixed(inner, step.env, step.pct_env, step.stmt.in_block)
        if not expanded.ok:
            by_reason[expanded.reason] = by_reason.get(expanded.reason, 0) + 1
            continue

        orig_items = [p for p in expanded.text.split(',')]
        new_items: list[str] = []
        any_change = False
        for orig in orig_items:
            name, expr_full, ok = _split_compound_assignment(orig.strip())
            if not ok:
                new_items.append(orig)
                continue
            try:
                val = eval_arith(expr_full, numeric_resolver(step.env))
            except ArithError as e:
                by_reason[str(e)] = by_reason.get(str(e), 0) + 1
                new_items.append(orig)
                continue
            new_items.append(f'{name}={val}')
            any_change = True

        if not any_change:
            continue
        new_inner = ','.join(new_items)
        if new_inner == expanded.text and new_inner == ''.join(t.value for t in inner):
            continue
        edits.append((inner[0].start, inner[-1].end, new_inner))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed, 'by_reason': by_reason}


if __name__ == '__main__':
    run_tool(fold_arithmetic, description='Fold constant set /a arithmetic expressions into numeric literals.')
