#!/usr/bin/env python3
"""Folds `%VAR:find=repl%` / `!VAR:find=repl!` (including the `:*find=repl`
prefix form) against a statically-known VAR into the literal result. The
Batch analogue of PsFold-MethodChains / PsFold-StaticStringCalls /
vbs_fold_builtin_calls -- the search/replace counterpart of
bat_fold_substrings.py's `:~start,len` extraction.

Same environment model as bat_fold_substrings.py: %-refs resolve against the
block-entry snapshot, !-refs against the live value, via the shared
simulator.
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


def fold_strsub(text: str, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    edits: list[tuple[int, int, str]] = []
    changed = 0
    by_reason: dict[str, int] = {}

    for step in simulate(tree, Env()):
        for tok in step.stmt.tokens:
            if tok.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                continue
            name, mod = _split_modifier(tok.inner or '')
            if not mod or mod.startswith('~'):
                continue
            body = mod[1:] if mod.startswith('*') else mod
            if '=' not in body:
                continue
            if tok.kind == TokenKind.BANG_CAND and not step.env.delayed_expansion:
                continue
            resolve_env = step.pct_env if tok.kind == TokenKind.PCT_VAR else step.env
            val = resolve_env.resolve_read(name)
            if val is None:
                by_reason['variable not statically resolvable'] = by_reason.get('variable not statically resolvable', 0) + 1
                continue
            r = apply_var_modifier(val, mod)
            if not r.ok:
                by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
                continue
            if '%' in r.text or '!' in r.text:
                by_reason['result contains %/! -- refused to avoid re-expansion'] = \
                    by_reason.get('result contains %/! -- refused to avoid re-expansion', 0) + 1
                continue
            edits.append((tok.start, tok.end, r.text))
            changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed, 'by_reason': by_reason}


if __name__ == '__main__':
    run_tool(fold_strsub, description='Fold %VAR:find=repl% / !VAR:find=repl! (incl. *find=repl) against a known VAR.')
