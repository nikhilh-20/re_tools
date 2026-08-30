#!/usr/bin/env python3
"""Folds adjacent literal/resolved pieces into one literal. The Batch
analogue of PsFold-Strings / vbs_fold_concat -- except Batch has no explicit
concatenation operator at all: `set "X=%A%%B%literal"` concatenates purely
by JUXTAPOSITION, so "folding a concatenation" here means finding a maximal
run of TEXT / %-literal / resolvable %-var / resolvable !-var / caret-escape
tokens (bounded by a quote, whitespace, operator, or an unresolvable
expansion) and collapsing that whole run to one literal chunk, anywhere in a
statement -- not just inside `set` assignments.

Uses the same block-aware, %-vs-! two-clock resolution as
bat_fold_substrings.py, via the shared simulator.
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
from batdeoblib.expansion import apply_var_modifier, _split_modifier

_FOLDABLE = {TokenKind.TEXT, TokenKind.PCT_LIT, TokenKind.PCT_VAR, TokenKind.PCT_ARG,
             TokenKind.PCT_MODARG, TokenKind.PCT_UNMATCH, TokenKind.BANG_CAND, TokenKind.CARET_ESC}


def _resolve_one(tok, env, pct_env) -> str | None:
    if tok.kind == TokenKind.TEXT:
        return tok.value
    if tok.kind == TokenKind.PCT_LIT:
        return '%'
    if tok.kind == TokenKind.PCT_UNMATCH:
        return ''
    if tok.kind == TokenKind.CARET_ESC:
        if tok.inner in ('%', '!'):
            return None
        return tok.inner or ''
    if tok.kind == TokenKind.BANG_CAND:
        if not env.delayed_expansion:
            return None   # literal-unexpanded case is handled by NOT joining it into a fold at all
        name, mod = _split_modifier(tok.inner or '')
        val = env.resolve_read(name)
        if val is None:
            return None
        r = apply_var_modifier(val, mod)
        return r.text if r.ok else None
    if tok.kind == TokenKind.PCT_VAR:
        name, mod = _split_modifier(tok.inner or '')
        val = pct_env.resolve_read(name)
        if val is None:
            return None
        r = apply_var_modifier(val, mod)
        return r.text if r.ok else None
    if tok.kind in (TokenKind.PCT_ARG, TokenKind.PCT_MODARG):
        # positional args -- resolvable only when bound (e.g. by subroutine
        # inlining); at top level they're Unset -> '' per verified semantics.
        from batdeoblib.expansion import expand_run
        r = expand_run([tok], pct_env)
        return r.text if r.ok else None
    return None


def fold_concat(text: str, **_opts) -> tuple[str, dict]:
    tree = parse_script(tokenize(text))
    edits: list[tuple[int, int, str]] = []
    changed = 0

    for step in simulate(tree, Env()):
        toks = step.stmt.tokens
        # `set "P=%P%chunk"` -- folding the growing %P% into every accumulator
        # link is O(n^2) edit text. Treat a self-referential read as a run
        # boundary; a dedicated collapse pass is the right tool for these.
        self_targets = _statement_names_written(step.stmt)

        def _is_self_ref(t):
            return (t.kind in (TokenKind.PCT_VAR, TokenKind.BANG_CAND)
                    and (t.inner or '').split(':', 1)[0].strip().upper() in self_targets)

        i = 0
        n = len(toks)
        while i < n:
            if toks[i].kind not in _FOLDABLE or _is_self_ref(toks[i]):
                i += 1
                continue
            run_start = i
            pieces: list[str] = []
            has_expansion = False
            risky = False   # a resolved (non-literal) piece contains %/! -- see guard below
            j = i
            while j < n and toks[j].kind in _FOLDABLE and not _is_self_ref(toks[j]):
                val = _resolve_one(toks[j], step.env, step.pct_env)
                if val is None:
                    break
                if toks[j].kind != TokenKind.TEXT:
                    has_expansion = True
                    # Only a piece that came FROM a resolved expansion (a
                    # variable's actual value, dropper-controlled data) risks
                    # introducing a brand-new %/! pairing once merged and
                    # re-tokenized. A bare TEXT piece was, by construction,
                    # already an unpaired literal % or ! in the source (any
                    # char that WAS part of a real pairing would already be
                    # its own PCT_VAR/BANG_CAND/PCT_LIT token, never TEXT) --
                    # so plain-text %/! here is always safe to carry through.
                    if '%' in val or '!' in val:
                        risky = True
                pieces.append(val)
                j += 1
            if j > run_start and has_expansion and not risky:
                merged = ''.join(pieces)
                edits.append((toks[run_start].start, toks[j - 1].end, merged))
                changed += 1
            i = max(j, run_start + 1)

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(fold_concat, description='Fold adjacent literal/resolved pieces (juxtaposition concatenation) into one literal.')
