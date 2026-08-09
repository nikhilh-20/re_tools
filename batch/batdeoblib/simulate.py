"""Shared flow-sensitive statement simulator.

Walks a parsed statement/block tree top-to-bottom, IN SOURCE ORDER ONLY --
like vbs_propagate_constants, this never follows goto/call edges or forks on
`if`, it is a straight-line walk. Anything assigned inside a block (`for`
body, `if`/`else` body, or a bare `( ... )` group) is conservatively marked
Unknown immediately after the block, since a real run might execute that
block zero times, once, or (for `for`) many times with a different value
each time -- exactly the same conservative-invalidation rule
vbs_propagate_constants documents for loop/if/try/switch bodies.

This module is the single source of truth for "what does the environment
look like at statement N" -- bat_fold_substrings, bat_fold_strsub,
bat_resolve_indirection, bat_propagate_constants, and bat_unwrap_trueif all
consume it rather than each re-implementing forward simulation.

Block-local %-pre-expansion: per the empirically-verified rule (see
tokenizer.py docstring), a `%VAR%` reference lexically inside a `(...)`
block resolves using the environment AS OF BLOCK ENTRY, held fixed for every
statement in that block, while `!VAR!` still resolves per-statement live.
SimStep exposes both: `.pct_env` (block-entry snapshot, or the live env at
top level) for %-refs, and `.env` (the live, continuously-updated env) for
!-refs and for callers that want the true running state.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterator
from .env import Env
from .statements import Statement, Block
from .tokenizer import TokenKind
from .expansion import expand_statement, Expanded
from .resolver import eval_arith, ArithError

_COMPOUND_OPS = ('<<=', '>>=', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=')


def _split_compound_assignment(part: str) -> tuple[str, str, bool]:
    """Split a `set /a` comma-item ('NAME=EXPR' or 'NAME<op>=EXPR') into
    (name, expr_to_evaluate, ok). For a compound form, expr_to_evaluate
    wraps the current value in explicitly so the caller's evaluator sees
    the full `(name) <op> (expr)` computation, not just the RHS alone --
    `set /a "x+=5"` means x = x + 5, and the pre-assignment value of x is a
    real operand, not something the expression text alone contains. Never
    raises -- an unparseable shape returns ok=False with name possibly still
    populated (letting the caller invalidate it) or empty (nothing to do)."""
    eq_idx = part.find('=')
    if eq_idx == -1:
        return '', '', False
    name_part = part[:eq_idx]
    expr_part = part[eq_idx + 1:]
    for op in _COMPOUND_OPS:
        opchar = op[:-1]
        if name_part.endswith(opchar):
            name = name_part[:-len(opchar)].strip()
            if not name:
                return '', '', False
            return name, f'({name}){opchar}({expr_part})', True
    name = name_part.strip()
    if not name:
        return '', '', False
    return name, expr_part, True


def numeric_resolver(env: Env):
    """Build a resolve_var callback for eval_arith() implementing `set /a`'s
    documented bare-identifier semantics: a variable that's unset or holds a
    non-numeric string contributes 0 (verified empirically -- NOT an error),
    while a genuinely Unknown (unresolvable) variable still refuses the
    whole expression by returning None."""
    def resolve(name: str) -> int | None:
        v = env.resolve_read(name)
        if v is None:
            return None
        v = v.strip()
        if not v:
            return 0
        try:
            return int(v, 16) if v.lower().startswith('0x') else int(v, 8) if v.startswith('0') and v[1:].isdigit() else int(v)
        except ValueError:
            return 0
    return resolve


@dataclass
class SimStep:
    stmt: Statement
    env: Env          # live, continuously-updated environment
    pct_env: Env       # environment to resolve %-refs against (block-entry snapshot)


def _quote_stripped_set_value(stmt: Statement, env: Env, pct_env: Env) -> tuple[str, Expanded] | None:
    """For a `set NAME=VALUE` / `set "NAME=VALUE"` statement, return
    (name, Expanded-value). Implements cmd.exe's real quote-stripping rule:
    a SINGLE pair of quotes wrapping the ENTIRE `NAME=VALUE` text is
    stripped; a quote that does not wrap the whole assignment is NOT
    stripped and becomes part of the value (`set X="abc"` assigns X the
    literal 4-character text `"abc"`, quotes included).

    Uses the FULL token list (WS included), not code_tokens(): the value
    portion of `set "X=a b c"` legitimately contains internal whitespace,
    and code_tokens() strips WS unconditionally regardless of quote state --
    using it here would silently glue every space-separated word in the
    value together."""
    full = [t for t in stmt.tokens if t.kind not in (TokenKind.NEWLINE, TokenKind.COMMENT)]
    i = 0
    while i < len(full) and full[i].kind == TokenKind.WS:
        i += 1
    if i >= len(full) or full[i].kind != TokenKind.TEXT or full[i].value.lstrip('@').upper() != 'SET':
        return None
    i += 1
    while i < len(full) and full[i].kind == TokenKind.WS:
        i += 1
    rest = full[i:]
    if not rest:
        return None
    if rest[0].kind == TokenKind.TEXT and rest[0].value.startswith('/'):
        return None   # /a, /p -- handled by dedicated callers

    wraps_whole = rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
    body_tokens = rest[1:-1] if wraps_whole else rest

    # find first '=' among the body's TEXT tokens (unquoted structural '=')
    eq_idx = None
    for i, t in enumerate(body_tokens):
        if t.kind == TokenKind.TEXT and '=' in t.value:
            eq_idx = i
            break
    if eq_idx is None:
        return None

    name_tok = body_tokens[eq_idx]
    eq_pos = name_tok.value.index('=')
    name = ''.join(t.value for t in body_tokens[:eq_idx]) + name_tok.value[:eq_pos]
    name = name.strip()
    if not name:
        return None

    value_tokens = [
        *([name_tok.__class__(name_tok.kind, name_tok.value[eq_pos + 1:], name_tok.start, name_tok.end,
                               name_tok.in_quotes, name_tok.inner)] if name_tok.value[eq_pos + 1:] else []),
        *body_tokens[eq_idx + 1:],
    ]
    return name, _expand_mixed(value_tokens, env, pct_env, stmt.in_block)


def _expand_mixed(tokens, env: Env, pct_env: Env, in_block: bool) -> Expanded:
    """Expand a token run where %-tokens resolve against pct_env (block-entry
    snapshot) and !-tokens resolve against the live env -- the two-clock
    behavior blocks require. At top level pct_env IS env, so this collapses
    to ordinary single-environment expansion."""
    if not in_block or pct_env is env:
        return expand_statement(tokens, env, is_call=False)

    # Two-pass: first let pct_env resolve %-tokens only (bang left literal by
    # forcing delayed_expansion off on a shadow), producing an intermediate
    # token-substituted string; unresolved bang candidates are preserved
    # verbatim so a second pass can resolve them against the live env.
    from .tokenizer import tokenize as _tok
    shadow = Env(delayed_expansion=False)
    shadow.restore(pct_env.snapshot())
    r1 = expand_statement(tokens, shadow, is_call=False)
    if not r1.ok:
        return r1
    if not env.delayed_expansion:
        return r1
    r2 = expand_statement(_tok(r1.text), env, is_call=False)
    return r2


def _apply_set(stmt: Statement, env: Env, pct_env: Env) -> None:
    ct = stmt.code_tokens()
    rest = ct[1:] if ct else []
    is_a = len(rest) >= 1 and rest[0].kind == TokenKind.TEXT and rest[0].value.upper() in ('/A',)
    is_p = len(rest) >= 1 and rest[0].kind == TokenKind.TEXT and rest[0].value.upper() in ('/P',)

    if is_p:
        # set /p NAME=prompt -- reads user input; find the target name best-effort
        for t in rest[1:]:
            if t.kind == TokenKind.TEXT and '=' in t.value:
                name = t.value.split('=', 1)[0].strip()
                if name:
                    env.set_unknown(name)
                break
        return

    if is_a:
        body = rest[1:]
        expanded = _expand_mixed(body, env, pct_env, stmt.in_block)
        if not expanded.ok:
            return   # can't even see the expression text -- nothing to invalidate by name safely
        text = expanded.text.strip().strip('"')
        for part in text.split(','):
            part = part.strip()
            if '=' not in part:
                continue
            name, expr_full, ok = _split_compound_assignment(part)
            if not ok:
                if name:
                    env.set_unknown(name)
                continue
            try:
                val = eval_arith(expr_full, numeric_resolver(env))
                env.set_known(name, str(val))
            except ArithError:
                env.set_unknown(name)
        return

    r = _quote_stripped_set_value(stmt, env, pct_env)
    if r is None:
        return
    name, expanded = r
    if expanded.ok:
        if expanded.text == '':
            # `set "X="` (RHS empty) doesn't set X to an empty string --
            # cmd.exe's environment has no such state; it deletes the
            # variable outright. Verified empirically: `if defined X` is
            # false immediately after `set "X="`.
            env.unset(name)
        else:
            env.set_known(name, expanded.text)
    else:
        env.set_unknown(name)


def _apply_setlocal(stmt: Statement, env: Env) -> None:
    ct = stmt.code_tokens()
    words = [t.value.upper() for t in ct if t.kind == TokenKind.TEXT]
    enable = None
    if any(w == 'ENABLEDELAYEDEXPANSION' for w in words):
        enable = True
    elif any(w == 'DISABLEDELAYEDEXPANSION' for w in words):
        enable = False
    env.setlocal(enable_delayed=enable)


def _call_set_target_name(stmt: Statement) -> str | None:
    """For a `call set "NAME=..."` / `call set NAME=...` statement, return
    NAME (uppercased), or None if this `call` isn't a `set` invocation at
    all (e.g. `call :label`, `call otherprogram`)."""
    ct = stmt.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT or ct[0].value.lstrip('@').upper() != 'CALL':
        return None
    rest = ct[1:]
    if not rest or rest[0].kind != TokenKind.TEXT or rest[0].value.upper() != 'SET':
        return None
    body = rest[1:]
    if body and body[0].kind == TokenKind.TEXT and body[0].value.upper() in ('/A', '/P'):
        body = body[1:]
    wraps = body and body[0].kind == TokenKind.QUOTE and body[-1].kind == TokenKind.QUOTE and len(body) > 1
    inner = body[1:-1] if wraps else body
    for t in inner:
        if t.kind == TokenKind.TEXT and '=' in t.value:
            return t.value.split('=', 1)[0].strip().upper() or None
    return None


def _statement_names_written(stmt: Statement) -> set[str]:
    """Names this statement could assign, used to invalidate them after an
    enclosing block ends (conservative: a block might run 0, 1, or many
    times, so nothing it writes is trusted to survive past it)."""
    names: set[str] = set()
    ct = stmt.code_tokens()
    if ct and ct[0].kind == TokenKind.TEXT and ct[0].value.lstrip('@').upper() == 'SET':
        rest = ct[1:]
        if rest and rest[0].kind == TokenKind.TEXT and rest[0].value.upper() == '/A':
            body = rest[1:]
            text = ''.join(t.value for t in body)
            for part in text.strip('"').split(','):
                name, _expr, ok = _split_compound_assignment(part.strip())
                if ok:
                    names.add(name.upper())
        else:
            wraps_whole = rest and rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
            body_tokens = rest[1:-1] if wraps_whole else rest
            for t in body_tokens:
                if t.kind == TokenKind.TEXT and '=' in t.value:
                    names.add(t.value.split('=', 1)[0].strip().upper())
                    break
    call_target = _call_set_target_name(stmt)
    if call_target:
        names.add(call_target)
    return names


def _walk(nodes: list, env: Env, pct_env: Env) -> Iterator[SimStep]:
    for node in nodes:
        if isinstance(node, Block):
            block_pct_env = Env(delayed_expansion=env.delayed_expansion)
            block_pct_env.restore(env.snapshot())
            written: set[str] = set()
            for leaf in _flatten_for_names(node.body):
                written |= _statement_names_written(leaf)
            yield from _walk(node.body, env, block_pct_env)
            for name in written:
                env.set_unknown(name)
        else:
            yield SimStep(node, env, pct_env)
            ct = node.code_tokens()
            if not ct:
                continue
            word = ct[0].value.lstrip('@').upper() if ct[0].kind == TokenKind.TEXT else ''
            if word == 'SET':
                _apply_set(node, env, pct_env)
            elif word == 'SETLOCAL':
                _apply_setlocal(node, env)
            elif word == 'ENDLOCAL':
                env.endlocal()
            elif word == 'CALL':
                # `call set "X=..."` -- the generic simulator can't compute
                # what this assigns (that needs bat_resolve_indirection.py's
                # specific %%/! double-expansion handling), but it MUST NOT
                # silently leave X in whatever state it already had either:
                # X genuinely IS about to be written to. Leaving it alone
                # would let a later fold treat X's stale UNSET state as
                # "confidently empty" and silently fold a wrong value in --
                # exactly the bug this branch exists to prevent. Mark
                # whatever name a `call set` targets as Unknown instead.
                target = _call_set_target_name(node)
                if target:
                    env.set_unknown(target)


def _flatten_for_names(nodes: list) -> Iterator[Statement]:
    for node in nodes:
        if isinstance(node, Statement):
            yield node
        else:
            yield from _flatten_for_names(node.body)


def simulate(nodes: list, env: Env | None = None) -> Iterator[SimStep]:
    """Straight-line forward simulation over a parse_script() tree. Yields
    one SimStep per statement, in source order, with `env` mutated in place
    as assignments are simulated -- callers that need to freeze a snapshot
    should call `.env.snapshot()` themselves at the point of interest."""
    env = env if env is not None else Env()
    yield from _walk(nodes, env, env)
