#!/usr/bin/env python3
"""Collapses an `if` statement whose condition is statically TRUE down to
just its action/body -- the opaque-predicate counterpart to a false
condition (bat_remove_deadcode.py's job). The Batch analogue of
PsUnwrap-TrueIf / vbs_unwrap_trueif. Only the unambiguous cases: `defined`,
`exist`, string equality (`==`), and numeric comparison (EQU/NEQ/LSS/LEQ/
GTR/GEQ), each optionally `/i` (case-insensitive) and/or `not`-negated.
Complex or unresolvable conditions are left untouched.

Handles both `if` shapes:
  - same-line, no parens: `if COND action` -> `action` (the statement is
    rewritten in place).
  - parenthesized block: `if COND (body) [else (elsebody)]` -> the surviving
    block's body is lifted out, delimiters and the other branch removed.
    Safe because `(...)` groups are not scope boundaries in Batch (there is
    no block scoping at all outside setlocal/endlocal) -- lifting a block's
    body out changes nothing about where its assignments land.

`exist`/`errorlevel` conditions are always left alone (never statically
resolvable -- they depend on the filesystem / the previous command's exit
code, not on anything this toolkit can see).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, Statement, Block
from batdeoblib.simulate import simulate, _expand_mixed
from batdeoblib.env import Env, VState
from batdeoblib.resolver import eval_condition

_NUM_OPS = {'EQU', 'NEQ', 'LSS', 'LEQ', 'GTR', 'GEQ'}


def _consume_value(ct: list, i: int) -> int | None:
    """Consume one if-condition operand starting at ct[i] (already past any
    leading WS). Returns the exclusive end index, or None if ct[i] doesn't
    start a value at all."""
    n = len(ct)
    if i >= n:
        return None
    if ct[i].kind == TokenKind.QUOTE:
        j = i + 1
        while j < n and ct[j].kind != TokenKind.QUOTE:
            j += 1
        return min(j + 1, n)
    if ct[i].kind in (TokenKind.WS, TokenKind.OP):
        return None
    j = i
    while j < n and ct[j].kind not in (TokenKind.WS, TokenKind.OP):
        j += 1
    return j if j > i else None


def _skip_ws(ct: list, i: int) -> int:
    n = len(ct)
    while i < n and ct[i].kind == TokenKind.WS:
        i += 1
    return i


def _parse_if_header(ct: list) -> dict | None:
    """ct = code_tokens (WS excluded!) of the leading `if [/i] [not]` --
    caller passes the FULL token list (WS included) sliced from just after
    the `if` keyword; returns a dict describing where the condition starts/
    ends and whether it's negated/case-insensitive, or None if this isn't a
    recognizable condition shape."""
    i = _skip_ws(ct, 0)
    case_insensitive = False
    negated = False
    if i < len(ct) and ct[i].kind == TokenKind.TEXT and ct[i].value.upper() == '/I':
        case_insensitive = True
        i = _skip_ws(ct, i + 1)
    if i < len(ct) and ct[i].kind == TokenKind.TEXT and ct[i].value.upper() == 'NOT':
        negated = True
        i = _skip_ws(ct, i + 1)
    cond_start = i
    if i >= len(ct):
        return None

    if ct[i].kind == TokenKind.TEXT and ct[i].value.upper() in ('DEFINED', 'EXIST', 'ERRORLEVEL'):
        kw = ct[i].value.upper()
        j = _skip_ws(ct, i + 1)
        end = _consume_value(ct, j)
        if end is None:
            return None
        if kw != 'DEFINED':
            return None   # exist/errorlevel never statically resolvable -- not this pass's job
        return {'kind': 'defined', 'start': cond_start, 'end': end,
                'name_tokens': ct[j:end], 'negated': negated, 'ci': case_insensitive}

    v1_end = _consume_value(ct, i)
    if v1_end is None:
        return None
    j = _skip_ws(ct, v1_end)
    if j < len(ct) and ct[j].kind == TokenKind.OP and ct[j].value == '=' and \
       j + 1 < len(ct) and ct[j + 1].kind == TokenKind.OP and ct[j + 1].value == '=':
        # shouldn't normally happen (tokenizer keeps "==" only if it were an
        # OP token, but '=' isn't in _OP1 -- '==' actually lexes as TEXT)
        pass
    if j < len(ct) and ct[j].kind == TokenKind.TEXT and ct[j].value == '==':
        op_end = j + 1
        k = _skip_ws(ct, op_end)
        v2_end = _consume_value(ct, k)
        if v2_end is None:
            return None
        return {'kind': 'streq', 'start': cond_start, 'end': v2_end,
                'lhs': ct[i:v1_end], 'rhs': ct[k:v2_end], 'negated': negated, 'ci': case_insensitive}
    if j < len(ct) and ct[j].kind == TokenKind.TEXT and ct[j].value.upper() in _NUM_OPS:
        op = ct[j].value.upper()
        op_end = j + 1
        k = _skip_ws(ct, op_end)
        v2_end = _consume_value(ct, k)
        if v2_end is None:
            return None
        return {'kind': 'numcmp', 'op': op, 'start': cond_start, 'end': v2_end,
                'lhs': ct[i:v1_end], 'rhs': ct[k:v2_end], 'negated': negated, 'ci': case_insensitive}
    return None


def _resolve_header(header: dict, env, pct_env, in_block: bool) -> bool | None:
    if header['kind'] == 'defined':
        name_r = _expand_mixed(header['name_tokens'], env, pct_env, in_block)
        if not name_r.ok:
            return None
        # Use the raw KNOWN/UNSET/UNKNOWN state directly, not resolve_read():
        # resolve_read() returns '' for BOTH an unset variable and (after
        # simulate.py's empty-assignment-deletes-the-variable fix) a
        # never-non-empty one -- exactly right for %/! expansion, but
        # `defined` needs the state itself, not the expanded value, to tell
        # "never assigned" apart from "unresolvable at analysis time".
        state = env.get(name_r.text.strip('"')).state
        if state == VState.UNKNOWN:
            return None
        result = state == VState.KNOWN
    else:
        lhs_r = _expand_mixed(header['lhs'], env, pct_env, in_block)
        rhs_r = _expand_mixed(header['rhs'], env, pct_env, in_block)
        if not lhs_r.ok or not rhs_r.ok:
            return None
        op = '==' if header['kind'] == 'streq' else header['op']
        cond_text = f'{lhs_r.text} {op} {rhs_r.text}'
        r = eval_condition(cond_text, case_insensitive=header['ci'])
        if r.value is None:
            return None
        result = r.value
    return (not result) if header['negated'] else result


def unwrap_trueif(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    env_before: dict[int, tuple] = {}
    for step in simulate(tree, Env()):
        env_before[id(step.stmt)] = (step.env, step.pct_env)

    edits: list[tuple[int, int, str]] = []
    changed = 0

    def is_if_stmt(s: Statement) -> bool:
        ct = s.code_tokens()
        return bool(ct) and ct[0].kind == TokenKind.TEXT and ct[0].value.lstrip('@').upper() == 'IF'

    def scan(nodes: list):
        nonlocal changed
        i = 0
        while i < len(nodes):
            node = nodes[i]
            if isinstance(node, Statement) and is_if_stmt(node):
                envs = env_before.get(id(node))
                if envs is not None:
                    env, pct_env = envs
                    after_if = [t for t in node.tokens[1:] if t.kind != TokenKind.NEWLINE]
                    header = _parse_if_header(after_if)
                    if header is not None:
                        truth = _resolve_header(header, env, pct_env, node.in_block)
                        if truth is True:
                            # Form B: next sibling is a Block (parenthesized body)
                            if i + 1 < len(nodes) and isinstance(nodes[i + 1], Block):
                                body_block = nodes[i + 1]
                                start = node.start
                                end = body_block.end
                                skip_to = i + 2
                                # optional `else (...)` right after -- drop it too
                                if skip_to < len(nodes) and isinstance(nodes[skip_to], Statement) and \
                                   nodes[skip_to].code_tokens() and \
                                   nodes[skip_to].code_tokens()[0].value.upper() == 'ELSE' and \
                                   skip_to + 1 < len(nodes) and isinstance(nodes[skip_to + 1], Block):
                                    end = nodes[skip_to + 1].end
                                    skip_to += 2
                                inner = body_block.raw(text)[1:-1]
                                edits.append((start, end, inner))
                                changed += 1
                                i = skip_to
                                continue
                            else:
                                # Form A: same-line action follows the condition
                                # tail (after_if[header['end']:]) inside this
                                # SAME statement.
                                action_tokens = after_if[header['end']:]
                                action_text = ''.join(t.value for t in action_tokens).lstrip()
                                if action_text:
                                    body_tokens = [t for t in node.tokens if t.kind != TokenKind.NEWLINE]
                                    edits.append((body_tokens[0].start, body_tokens[-1].end, action_text))
                                    changed += 1
            if isinstance(node, Block):
                scan(node.body)
            i += 1

    scan(tree)
    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(unwrap_trueif, description='Collapse a statically-true if statement down to just its action/body.')
