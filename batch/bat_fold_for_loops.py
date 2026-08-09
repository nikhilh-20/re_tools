#!/usr/bin/env python3
"""Folds the accumulator idiom built on a statically-enumerable `for` loop:

    set "ACC="
    for /l %%i in (0,1,4) do ( set "ACC=!ACC!<fragment using %%i>" )

or

    for %%i in (a b c) do ( set "ACC=!ACC!%%i" )

into a single `set "ACC=<fully computed literal>"`, removing the loop. The
Batch analogue of PsFold-ArrayJoins / vbs_fold_array_join_loops -- malware
uses this to keep a long payload as a numeric range or word list instead of
one quoted blob.

Scope: `for /l` (numeric start,step,end, all literal or resolvable) and
plain `for %%V in (item item ...)` (space-separated literal items). `for /f`
tokenizing is not attempted by this pass -- out of scope, refuses cleanly.
The loop is folded ONLY when every iteration's fragment is provably
resolvable (each iteration re-simulated with %%V bound to that iteration's
value and ACC bound to the accumulation so far); any single unresolvable
iteration refuses the WHOLE loop rather than emitting a partial result.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind, BatToken
from batdeoblib.statements import parse_script, Statement, Block, flatten
from batdeoblib.simulate import simulate, _expand_mixed
from batdeoblib.env import Env

_FOR_HEAD_RE = re.compile(r'(?i)^for\s+(/l\s+)?%%(\w+)\s+in\s*$')
_DO_RE = re.compile(r'(?i)^do\s*$')
_MAX_ITERATIONS = 4096


def _stmt_text(s: Statement) -> str:
    return ''.join(t.value for t in s.tokens if t.kind != TokenKind.NEWLINE).strip()


def _split_items(raw: str) -> list[str] | None:
    """Split a plain `for %%V in (...)` item list on whitespace, respecting
    double-quoted items. Returns None if unbalanced quotes make this unsafe."""
    items: list[str] = []
    cur = []
    in_q = False
    for ch in raw:
        if ch == '"':
            in_q = not in_q
            continue
        if ch in ' \t' and not in_q:
            if cur:
                items.append(''.join(cur))
                cur = []
            continue
        cur.append(ch)
    if in_q:
        return None
    if cur:
        items.append(''.join(cur))
    return items


def _find_single_set_stmt(body_nodes: list) -> Statement | None:
    stmts = [s for s in flatten(body_nodes) if _stmt_text(s)]
    if len(stmts) != 1:
        return None
    s = stmts[0]
    ct = s.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT or ct[0].value.upper() != 'SET':
        return None
    return s


def _acc_name_and_value_tokens(stmt: Statement):
    ct = stmt.code_tokens()
    rest = ct[1:]
    if not rest:
        return None
    wraps = rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
    body = rest[1:-1] if wraps else rest
    eq_i = None
    for i, t in enumerate(body):
        if t.kind == TokenKind.TEXT and '=' in t.value:
            eq_i = i
            break
    if eq_i is None:
        return None
    name_tok = body[eq_i]
    eq_pos = name_tok.value.index('=')
    name = (''.join(t.value for t in body[:eq_i]) + name_tok.value[:eq_pos]).strip()
    if not name:
        return None
    rhs = body[eq_i + 1:]
    rest_of_name_tok_value = name_tok.value[eq_pos + 1:]
    return name, rest_of_name_tok_value, rhs


def _rewrite_forvar_tokens(tokens: list, loopvar: str, value: str) -> list:
    """The tokenizer has no notion of `for`-loop metavariables -- `%%A` in a
    batch FILE lexes as PCT_LIT('%%') + TEXT('A') (see tokenizer.py's
    docstring: this reinterpretation is deliberately deferred to whichever
    pass actually knows a `for %%A in (...) do` declared A as a loop var).
    This is that pass. Rewrites a PCT_LIT immediately followed by a TEXT
    token whose value starts with *loopvar* (case-sensitive, matching
    cmd.exe) into a single literal TEXT token holding *value*, splitting off
    any remaining characters of that TEXT token as ordinary trailing text.
    Everything else passes through unchanged for normal expansion.

    Also handles `%%loopvar` occurring INSIDE a PCT_VAR/BANG_CAND token's own
    modifier text (`!S:~%%i,1!` -- the char-harvesting-by-index idiom): the
    whole `!...!`/`%...%` there is already one token by the time the
    tokenizer is done, so this substitutes within its `.inner` string
    directly rather than at the outer token-stream level.
    """
    marker = '%%' + loopvar
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if (t.kind == TokenKind.PCT_LIT and i + 1 < n
                and tokens[i + 1].kind == TokenKind.TEXT
                and tokens[i + 1].value[:1] == loopvar):
            nxt = tokens[i + 1]
            out.append(BatToken(TokenKind.TEXT, value, t.start, nxt.start + 1, t.in_quotes))
            remainder = nxt.value[1:]
            if remainder:
                out.append(BatToken(TokenKind.TEXT, remainder, nxt.start + 1, nxt.end, nxt.in_quotes))
            i += 2
            continue
        if t.kind in (TokenKind.PCT_VAR, TokenKind.BANG_CAND) and t.inner and marker in t.inner:
            new_inner = t.inner.replace(marker, value)
            out.append(BatToken(t.kind, t.value, t.start, t.end, t.in_quotes, new_inner))
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def _try_fold_group(nodes: list, i: int, text: str, pre_env: Env) -> tuple[int, str, dict] | None:
    """Try to fold a for/do accumulator group starting at nodes[i]. Returns
    (end_index_exclusive, folded_text, stats) or None if this position
    isn't such a group (or it is, but can't be resolved)."""
    if i + 3 >= len(nodes):
        return None
    n0, n1, n2, n3 = nodes[i], nodes[i + 1], nodes[i + 2], nodes[i + 3]
    if not (isinstance(n0, Statement) and isinstance(n1, Block)
            and isinstance(n2, Statement) and isinstance(n3, Block)):
        return None
    m = _FOR_HEAD_RE.match(_stmt_text(n0))
    if not m or not _DO_RE.match(_stmt_text(n2)):
        return None
    is_l = bool(m.group(1))
    loopvar = m.group(2)

    set_stmt = _find_single_set_stmt(n3.body)
    if set_stmt is None:
        return None
    parsed = _acc_name_and_value_tokens(set_stmt)
    if parsed is None:
        return None
    acc_name, name_tok_tail, rhs_tokens = parsed

    acc_initial = pre_env.resolve_read(acc_name)
    if acc_initial is None:
        return None   # ACC's starting value isn't statically known -- refuse

    item_text = n1.raw(text)[1:-1]   # strip the surrounding ( )
    if is_l:
        parts = [p.strip() for p in item_text.split(',')]
        if len(parts) != 3:
            return None
        try:
            start, step, stop = (int(p) for p in parts)
        except ValueError:
            return None
        if step == 0:
            return None
        values = []
        v = start
        count = 0
        while (step > 0 and v <= stop) or (step < 0 and v >= stop):
            values.append(str(v))
            v += step
            count += 1
            if count > _MAX_ITERATIONS:
                return None
    else:
        values = _split_items(item_text)
        if values is None or not values:
            return None
        if len(values) > _MAX_ITERATIONS:
            return None

    acc = acc_initial
    for val in values:
        iter_env = Env(delayed_expansion=pre_env.delayed_expansion)
        iter_env.restore(pre_env.snapshot())
        iter_env.set_known(acc_name, acc)
        rewritten = _rewrite_forvar_tokens(rhs_tokens, loopvar, val)
        r = _expand_mixed(rewritten, iter_env, iter_env, in_block=False)
        if not r.ok:
            return None
        new_acc = name_tok_tail + r.text
        if '%' in new_acc or '!' in new_acc:
            return None   # would risk forming a new expansion once re-inlined
        acc = new_acc

    folded = f'set "{acc_name}={acc}"'
    return i + 4, folded, {'iterations': len(values), 'form': '/l' if is_l else 'in'}


def fold_for_loops(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)

    # Build a map from top-level (and nested) Statement/Block sequences to a
    # pre-loop env snapshot by walking simulate() once and remembering the
    # env just before each Statement object (identity-keyed).
    env_before: dict[int, Env] = {}
    for step in simulate(tree, Env()):
        e = Env(delayed_expansion=step.env.delayed_expansion)
        e.restore(step.env.snapshot())
        env_before[id(step.stmt)] = e

    edits: list[tuple[int, int, str]] = []
    changed = 0
    total_iterations = 0

    def scan(nodes: list):
        nonlocal changed, total_iterations
        i = 0
        while i < len(nodes):
            node = nodes[i]
            if isinstance(node, Statement):
                pre = env_before.get(id(node))
                if pre is not None:
                    result = _try_fold_group(nodes, i, text, pre)
                    if result is not None:
                        end_i, folded, stats = result
                        start = nodes[i].start
                        end = nodes[end_i - 1].end
                        edits.append((start, end, folded))
                        changed += 1
                        total_iterations += stats['iterations']
                        i = end_i
                        continue
            if isinstance(node, Block):
                scan(node.body)
            i += 1

    scan(tree)
    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed, 'total_iterations_folded': total_iterations}


if __name__ == '__main__':
    run_tool(fold_for_loops, description='Fold a statically-enumerable for /l or for-in accumulator loop into a literal.')
