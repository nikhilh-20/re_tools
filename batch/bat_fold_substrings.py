#!/usr/bin/env python3
"""Folds `%VAR:~start[,len]%` / `!VAR:~start[,len]!` substring extraction
against a statically-known VAR into the literal result. The Batch analogue
of PsFold-CharConcat / vbs_fold_chr_calls / vbs_fold_instr_mid -- defeats
the "spell the payload out of a big random-looking seed string, one
character at a time" idiom this toolkit's own README example (a real
technique the test sample uses 100+ times) is built around.

Uses the shared flow-sensitive simulator (batdeoblib.simulate) to know each
statement's variable environment, correctly distinguishing %-refs (resolved
against the block-entry snapshot) from !-refs (resolved against the live
value) per the empirically-verified two-clock rule.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate
from batdeoblib.env import Env
from batdeoblib.expansion import apply_var_modifier, _split_modifier


def fold_substrings(text: str, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    edits: list[tuple[int, int, str]] = []
    changed = 0

    for step in simulate(tree, Env()):
        for tok in step.stmt.tokens:
            if tok.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                continue
            name, mod = _split_modifier(tok.inner or '')
            if not mod or not mod.startswith('~'):
                continue
            if tok.kind == TokenKind.BANG_CAND and not step.env.delayed_expansion:
                continue   # literal, unexpanded at runtime -- nothing to fold
            resolve_env = step.pct_env if tok.kind == TokenKind.PCT_VAR else step.env
            val = resolve_env.resolve_read(name)
            if val is None:
                continue
            r = apply_var_modifier(val, mod)
            if not r.ok:
                continue
            if '%' in r.text or '!' in r.text:
                # Inlining raw text containing % or ! at this position risks
                # it pairing with some OTHER %/! elsewhere on the line to
                # form a brand-new (unintended) expansion once re-tokenized
                # -- refuse rather than risk silently changing meaning.
                continue
            edits.append((tok.start, tok.end, r.text))
            changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(fold_substrings, description='Fold %VAR:~start,len% / !VAR:~start,len! against a known VAR.')
