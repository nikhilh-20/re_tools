#!/usr/bin/env python3
"""Inlines a `call :label args...` subroutine at its call site when there is
exactly ONE call site for that label anywhere in the script. The Batch
analogue of vbs_inline_functions. Substitutes `%1`-`%9`/`%*` in the body
with the literal arguments from the call site and drops the wrapper.

Scoped to the safe, unambiguous shape: the subroutine's body (from the
label to the next label or end of file) has at most ONE exit point --
either a trailing `goto :eof` / `exit /b` as its last statement, or no
explicit terminator at all (falls off the end, which is an implicit
return). A body with an EARLY return (a `goto :eof`/`exit /b` that is NOT
the last statement -- i.e. real branching return paths) is left untouched:
correctly inlining that would require redirecting each early exit to a
fresh label placed after the inlined code, which is a real transformation
this pass doesn't attempt rather than risk getting subtly wrong.

Only a literal `call :label` (not `call :label` reached through variable
indirection, and not a label targeted by any `goto` as well as this one
`call` -- inlining would then duplicate code a jump also depends on landing
on) is eligible.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten, Statement
from batdeoblib.cfg import build_cfg


def _first_word(s: Statement) -> str | None:
    ct = s.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT:
        return None
    return ct[0].value.lstrip('@').upper()


def _label_name(s: Statement) -> str | None:
    ct = s.code_tokens()
    if len(ct) == 1 and ct[0].kind == TokenKind.LABEL:
        return (ct[0].inner or '').upper().split()[0] if (ct[0].inner or '').strip() else None
    return None


def _is_return_stmt(s: Statement) -> bool:
    """True when *s* is ENTIRELY a return (`exit ...` or `goto :eof`) as its
    own whole statement -- the safe, unambiguous case this pass inlines."""
    ct = s.code_tokens()
    if not ct:
        return False
    w = _first_word(s)
    if w == 'EXIT':
        return True
    if w == 'GOTO':
        rest = ct[1:]
        target = next((t for t in rest if t.kind in (TokenKind.TEXT, TokenKind.LABEL)), None)
        if target is None:
            return False
        name = (target.inner if target.kind == TokenKind.LABEL else target.value).upper().lstrip(':')
        return name == 'EOF'
    return False


def _contains_embedded_return(s: Statement) -> bool:
    """True when a return marker (GOTO :EOF or EXIT) appears ANYWHERE in
    *s* WITHOUT *s* being a pure, whole-statement return itself -- e.g.
    `if "%1"=="x" goto :eof`. Mirrors cfg.py's same-line `if COND goto X`
    detection: an if-condition never legitimately contains these keywords
    unquoted on its own, so any occurrence signals a real early-return
    branch this pass must refuse rather than silently inline (a `goto :eof`
    means something completely different once it's no longer inside its
    original subroutine -- it would jump to the end of the whole script)."""
    if _is_return_stmt(s):
        return False
    ct = s.code_tokens()
    for i, t in enumerate(ct):
        if t.kind != TokenKind.TEXT or t.in_quotes:
            continue
        word = t.value.upper()
        if word == 'EXIT':
            return True
        if word == 'GOTO':
            target = next((x for x in ct[i + 1:] if x.kind in (TokenKind.TEXT, TokenKind.LABEL)), None)
            if target is not None:
                name = (target.inner if target.kind == TokenKind.LABEL else target.value).upper().lstrip(':')
                if name == 'EOF':
                    return True
    return False


def _split_call_args(text_after_label: str) -> list[str]:
    """Split whitespace-separated call arguments, respecting double quotes."""
    args: list[str] = []
    cur = []
    in_q = False
    for ch in text_after_label:
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
            continue
        if ch in ' \t' and not in_q:
            if cur:
                args.append(''.join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        args.append(''.join(cur))
    return args


def inline_subroutines(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    stmts = flatten(tree)
    n = len(stmts)
    cfg = build_cfg(tree)

    call_targets: dict[str, list] = {}
    goto_targets: set[str] = set()
    for g in cfg.gotos:
        if not g.target or g.target == 'EOF':
            continue
        if g.is_call:
            call_targets.setdefault(g.target, []).append(g)
        else:
            goto_targets.add(g.target)

    label_at: dict[int, str] = {}
    for i, s in enumerate(stmts):
        name = _label_name(s)
        if name:
            label_at[i] = name

    edits: list[tuple[int, int, str]] = []
    changed = 0

    for label, calls in call_targets.items():
        if len(calls) != 1:
            continue
        if label in goto_targets:
            continue   # also reached by a plain goto -- not exclusively a call-site body
        target_idx = cfg.label_index(label)
        if target_idx is None:
            continue
        body_end = target_idx + 1
        while body_end < n and label_at.get(body_end) is None:
            body_end += 1
        body_stmts = [s for s in stmts[target_idx + 1:body_end] if s.code_tokens()]
        if not body_stmts:
            continue

        return_positions = [k for k, s in enumerate(body_stmts) if _is_return_stmt(s)]
        if return_positions and return_positions != [len(body_stmts) - 1]:
            continue   # an early return -- out of scope, leave untouched
        if any(_contains_embedded_return(s) for s in body_stmts):
            continue   # e.g. `if "%1"=="x" goto :eof` -- a real early-return branch
        effective_body = body_stmts[:-1] if return_positions else body_stmts

        call_stmt = calls[0].stmt
        # Full token list (WS preserved!) minus NEWLINE -- code_tokens()
        # strips whitespace entirely, which would silently glue adjacent
        # arguments together when reconstructing text from it.
        full = [t for t in call_stmt.tokens if t.kind != TokenKind.NEWLINE]
        lbl_idx = next((i for i, t in enumerate(full)
                         if t.kind == TokenKind.LABEL or (t.kind == TokenKind.TEXT and t.value.startswith(':'))), None)
        if lbl_idx is None:
            continue
        arg_tokens = full[lbl_idx + 1:]
        args_text = ''.join(t.value for t in arg_tokens).strip()
        args = _split_call_args(args_text)

        # Substitute %1..%9/%* in the body with the call-site literal args.
        body_edits: list[tuple[int, int, str]] = []
        ok = True
        for s in effective_body:
            for t in s.tokens:
                if t.kind == TokenKind.PCT_ARG:
                    if t.inner == '*':
                        val = ' '.join(args)
                    else:
                        idx = int(t.inner) - 1
                        val = args[idx] if 0 <= idx < len(args) else ''
                    if '%' in val or '!' in val:
                        ok = False
                        break
                    body_edits.append((t.start, t.end, val))
            if not ok:
                break
        if not ok:
            continue

        body_lo = effective_body[0].tokens[0].start
        body_hi_tok_list = [t for t in effective_body[-1].tokens if t.kind != TokenKind.NEWLINE]
        body_hi = body_hi_tok_list[-1].end if body_hi_tok_list else effective_body[-1].tokens[-1].end
        body_text = text[body_lo:body_hi]
        # apply arg substitutions to the body text (offsets relative to original source)
        local_edits = [(s - body_lo, e - body_lo, v) for s, e, v in body_edits]
        for s_, e_, v_ in sorted(local_edits, key=lambda x: x[0], reverse=True):
            body_text = body_text[:s_] + v_ + body_text[e_:]

        call_body_tokens = [t for t in call_stmt.tokens if t.kind != TokenKind.NEWLINE]
        edits.append((call_body_tokens[0].start, call_body_tokens[-1].end, body_text))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(inline_subroutines, description='Inline a single-call-site subroutine at its call site.')
