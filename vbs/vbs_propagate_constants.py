"""vbs_propagate_constants — flow-sensitive constant propagation.

Walks top-level statements in order. For each variable assigned a constant
on the RHS, records the value. Downstream reads of that variable in the same
or later statements are replaced with the literal — provided the variable is
not re-assigned to a non-constant or modified inside a block (If/For/While).

Inside a block body, two regimes apply depending on the *kind* of every
block currently open:

  - Non-looping blocks (If/Select/With/Function/Sub/Class/Property): body
    statements execute in a fixed straight-line order at most once per entry,
    so a constant computed partway through (e.g. `Grejss = Reserveres` where
    Reserveres is already known) is tracked in a scope-local env and folded
    into later statements in the *same* straight-line run. This local scope
    is cleared the instant any block opens or closes (depth changes), so it
    never leaks across a branch/call boundary.
  - Looping blocks (For/Do/While) anywhere in the current nesting: local
    tracking is disabled entirely and the original fully-conservative
    behaviour applies (every block-depth assignment is killed, never
    folded), because a value computed from one iteration's inputs is not
    generally valid for the next.

Analog of PsPropagate-Constants.

Usage:
    python vbs_propagate_constants.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs, format_number
from vbsdeoblib.resolver import resolve_const, Const
from vbsdeoblib.statements import split_statements, find_block_end


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    substituted_total = 0
    for _ in range(50):
        src, n, s = _one_pass(src)
        changed_total += n
        substituted_total += s
        if n == 0:
            break
    return src, {'changed': changed_total, 'substituted_reads': substituted_total}


def _const_to_literal(v: Const) -> str:
    if isinstance(v, str):
        return quote_vbs(v)
    return format_number(v)


def _is_false_const(v: Const) -> bool:
    """VBScript falsiness of an already-resolved constant (comparisons here
    resolve to -1/0, not Python True/False — see vbsdeoblib.resolver)."""
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        return v.lower() in ('', 'false', '0')
    return False


def _one_pass(src: str) -> tuple[str, int, int]:
    tokens = tokenize(src)
    stmts  = split_statements(tokens)
    env: dict[str, Const] = {}   # name_upper -> value
    # Track which names are "killed" (assigned non-constant or assigned inside a block)
    killed: set[str] = set()
    block_depth: int = 0  # nesting depth inside block structures
    block_kinds: list[str] = []       # stack of 'LOOP' | 'OTHER' per open block
    local_env: dict[str, Const] = {}  # straight-line-scoped constants inside a block

    edits: list[tuple[int, int, str]] = []

    for stmt_i, stmt in enumerate(stmts):
        ctoks = stmt.code_tokens()
        if not ctoks:
            continue

        kw = ctoks[0].upper if ctoks[0].kind == TokenKind.IDENT else ''

        # Block closers: NEXT/LOOP/WEND each close exactly one level.
        if kw in ('NEXT', 'LOOP', 'WEND'):
            if block_kinds:
                block_kinds.pop()
            block_depth = max(0, block_depth - 1)
            local_env.clear()
            continue

        # END closes one level only when followed by another keyword
        # (END IF, END SUB, END FUNCTION, END WITH, END SELECT, END CLASS,
        # END PROPERTY). Bare END (script terminator) does not change depth.
        if kw == 'END':
            if len(ctoks) > 1 and ctoks[1].kind == TokenKind.IDENT:
                if block_kinds:
                    block_kinds.pop()
                block_depth = max(0, block_depth - 1)
                local_env.clear()
            continue

        # Block openers: kill any variable assigned in the header line, then
        # increment depth so body statements are processed differently below.
        # A single-line 'If c Then stmt' does NOT open a block (no matching
        # End If ever follows it) — only the multi-line header form (ending
        # in a bare THEN) does.
        is_block_open = False
        loop_open = False
        if kw == 'IF':
            last = ctoks[-1]
            is_block_open = last.kind == TokenKind.IDENT and last.upper == 'THEN'
        elif kw in ('FOR', 'DO', 'WHILE'):
            is_block_open = True
            loop_open = True
        elif kw in ('SELECT', 'WITH', 'FUNCTION', 'SUB', 'CLASS', 'PROPERTY'):
            is_block_open = True

        if is_block_open:
            _kill_assignments(ctoks, env, killed)
            if loop_open:
                # Proactively kill every name this loop's body assigns
                # anywhere (any nesting depth), *before* any body statement
                # is substituted. A lazy kill (only when the pass physically
                # reaches that statement) is too late whenever the loop
                # reads the name earlier in its body than it rewrites it —
                # the common read-then-increment/offset-accumulator shape —
                # which would otherwise fold every in-loop read to whatever
                # constant was known before the loop ever started.
                end_i = find_block_end(stmts, stmt_i)
                if end_i is not None:
                    _kill_loop_body_assignments(stmts, stmt_i + 1, end_i, env, killed)
            block_kinds.append('LOOP' if loop_open else 'OTHER')
            block_depth += 1
            local_env.clear()
            continue

        # --- Inside a block body (depth > 0) ---
        if block_depth > 0:
            in_loop = 'LOOP' in block_kinds
            # A single-line 'If cond Then stmt' embeds an ordinary statement
            # after THEN on the same logical line (it never opens a block —
            # see is_block_open above). Flattening the whole line into one
            # token stream for substitution would treat that embedded
            # statement's own assignment target as a read; split at the
            # top-level THEN so each half gets the rule that actually fits it.
            then_idx = _find_top_level_then(ctoks) if kw == 'IF' else None
            if then_idx is not None:
                merged = env if in_loop else {**env, **local_env}
                cond_sub = _substitute(ctoks[1:then_idx], merged, edits)
                cond_val = resolve_const(cond_sub, merged)
                stmt_part = ctoks[then_idx + 1:]
                if _is_assignment(stmt_part):
                    lhs_name, rhs_toks = _split_assignment(stmt_part)
                    if lhs_name:
                        # The write is *conditional*: only treat it as
                        # definitely happening when the guard is statically
                        # true. A statically-false guard means it never
                        # happens (env/local_env must stay untouched, not be
                        # overwritten with the dead branch's value); an
                        # unresolvable guard means it might or might not
                        # happen at runtime, so the name must be invalidated
                        # rather than assumed either unconditionally written
                        # or unconditionally skipped.
                        if cond_val is None:
                            local_env.pop(lhs_name.upper(), None)
                            killed.add(lhs_name.upper())
                            env.pop(lhs_name.upper(), None)
                            _substitute(rhs_toks, merged, edits)
                        elif _is_false_const(cond_val):
                            _substitute(rhs_toks, merged, edits)
                        else:
                            _apply_inblock_assignment(lhs_name, rhs_toks, env, killed, local_env, in_loop, edits)
                else:
                    merged = env if in_loop else {**env, **local_env}
                    _substitute(stmt_part, merged, edits)
            elif _is_assignment(ctoks):
                lhs_name, rhs_toks = _split_assignment(ctoks)
                if lhs_name:
                    _apply_inblock_assignment(lhs_name, rhs_toks, env, killed, local_env, in_loop, edits)
            else:
                merged = env if in_loop else {**env, **local_env}
                _substitute(ctoks, merged, edits)
            continue

        # --- Top-level assignment: [Set|Dim] name = expr  OR  name = expr ---
        if _is_assignment(ctoks):
            lhs_name, rhs_toks = _split_assignment(ctoks)
            if lhs_name:
                _apply_toplevel_assignment(lhs_name, rhs_toks, env, killed, edits)
            continue

        # Top-level non-assignment: same single-line-If concern as above —
        # split at the top-level THEN before substituting.
        then_idx = _find_top_level_then(ctoks) if kw == 'IF' else None
        if then_idx is not None:
            cond_sub = _substitute(ctoks[1:then_idx], env, edits)
            cond_val = resolve_const(cond_sub, env)
            stmt_part = ctoks[then_idx + 1:]
            if _is_assignment(stmt_part):
                lhs_name, rhs_toks = _split_assignment(stmt_part)
                if lhs_name:
                    # See the in-block twin of this branch above: the write
                    # only definitely happens when the guard is statically
                    # true; false means it never happens (leave env
                    # untouched); unresolvable means it might, so the name
                    # must be invalidated rather than assumed either way.
                    if cond_val is None:
                        killed.add(lhs_name.upper())
                        env.pop(lhs_name.upper(), None)
                        _substitute(rhs_toks, env, edits)
                    elif _is_false_const(cond_val):
                        _substitute(rhs_toks, env, edits)
                    else:
                        _apply_toplevel_assignment(lhs_name, rhs_toks, env, killed, edits)
            else:
                _substitute(stmt_part, env, edits)
        else:
            _substitute(ctoks, env, edits)

    if not edits:
        return src, 0, 0
    new_src = apply_edits(src, edits)
    return new_src, len(edits), len(edits)


def _find_top_level_then(ctoks: list) -> int | None:
    """Index of a top-level (not inside parens) THEN keyword, or None."""
    depth = 0
    for i, t in enumerate(ctoks):
        if t.kind == TokenKind.OP and t.value == '(':
            depth += 1
        elif t.kind == TokenKind.OP and t.value == ')':
            depth -= 1
        elif depth == 0 and t.kind == TokenKind.IDENT and t.upper == 'THEN':
            return i
    return None


def _apply_toplevel_assignment(lhs_name: str, rhs_toks: list, env: dict,
                                killed: set, edits: list) -> None:
    """Shared bookkeeping for a recognised top-level 'name = rhs' —
    whether it's the whole statement or the tail of a single-line
    'If cond Then name = rhs'."""
    lhs_up = lhs_name.upper()
    # Self-append accumulator: VBScript's uninitialized Variant is Empty,
    # which coerces to "" in a string expression.  When this is provably
    # the first assignment to the name AND the RHS self-references it
    # (e.g. X = X & "chunk"), seed it as "" so the whole chain folds.
    sub_env = env
    if (lhs_up not in env and lhs_up not in killed
            and _is_self_referencing(lhs_up, rhs_toks)):
        sub_env = dict(env)
        sub_env[lhs_up] = ''
    rhs_toks_sub = _substitute(rhs_toks, sub_env, edits)
    if lhs_up not in killed:
        val = resolve_const(rhs_toks_sub, sub_env)
        if val is not None:
            env[lhs_up] = val
        else:
            killed.add(lhs_up)
            env.pop(lhs_up, None)


def _apply_inblock_assignment(lhs_name: str, rhs_toks: list, env: dict, killed: set,
                               local_env: dict, in_loop: bool, edits: list) -> None:
    """Shared bookkeeping for a recognised in-block 'name = rhs' —
    whether it's the whole statement or the tail of a single-line
    'If cond Then name = rhs' inside an open block."""
    lhs_up = lhs_name.upper()
    # Clear any local knowledge of this name *before* substituting RHS so a
    # self-referencing update reads only its pre-this-statement value.
    local_env.pop(lhs_up, None)
    merged = env if in_loop else {**env, **local_env}
    # Outer/global env never retains a block-local write — matches the
    # original fully-conservative behaviour.
    killed.add(lhs_up)
    env.pop(lhs_up, None)
    rhs_toks_sub = _substitute(rhs_toks, merged, edits)
    if not in_loop:
        # Straight-line, non-looping block: safe to track this as a local
        # constant for later statements in the same run (a value computed
        # here executes exactly once before any subsequent read of it).
        val = resolve_const(rhs_toks_sub, merged)
        if val is not None:
            local_env[lhs_up] = val
    # Inside a loop: never track (a value derived from one iteration's
    # inputs is not valid for the next).


def _kill_assignments(ctoks: list, env: dict, killed: set) -> None:
    """Heuristically kill any variable that appears as LHS of = in ctoks."""
    for idx, t in enumerate(ctoks):
        if (t.kind == TokenKind.IDENT
                and idx + 1 < len(ctoks)
                and ctoks[idx+1].kind == TokenKind.OP
                and ctoks[idx+1].value == '='):
            name = t.value.upper()
            killed.add(name)
            env.pop(name, None)


def _kill_loop_body_assignments(stmts: list, start_i: int, end_i: int,
                                 env: dict, killed: set) -> None:
    """Kill every name assigned anywhere between statement indices
    [start_i, end_i) — a loop's full body, any nesting depth — reusing the
    same 'IDENT immediately followed by =' shape _kill_assignments already
    uses for block headers. Deliberately a token-shape heuristic rather than
    a precise assignment parse: it will also kill a name that only appears
    in a comparison (e.g. the X in 'If X = 5 Then' inside the loop), but
    over-killing here only costs a missed fold, never produces a wrong
    substitution — the same trade-off the rest of this tool already makes."""
    for j in range(start_i, end_i):
        _kill_assignments(stmts[j].code_tokens(), env, killed)


def _leading_skip(ctoks: list) -> int:
    """Number of leading modifier tokens to skip before the declared name,
    e.g. 'Dim x', 'Set x', 'Const x', or 'Public Const x' / 'Private Const x'."""
    start = 0
    if (ctoks[start].kind == TokenKind.IDENT and ctoks[start].upper in ('PUBLIC', 'PRIVATE')
            and start + 1 < len(ctoks) and ctoks[start + 1].kind == TokenKind.IDENT
            and ctoks[start + 1].upper == 'CONST'):
        start += 1
    if start < len(ctoks) and ctoks[start].kind == TokenKind.IDENT and ctoks[start].upper in ('DIM', 'SET', 'LET', 'CONST'):
        start += 1
    return start


def _is_assignment(ctoks: list) -> bool:
    """Return True if this looks like a simple top-level assignment."""
    if not ctoks:
        return False
    start = _leading_skip(ctoks)
    if start >= len(ctoks):
        return False
    # Next should be IDENT = ...
    if ctoks[start].kind != TokenKind.IDENT:
        return False
    if start + 1 >= len(ctoks):
        return False
    # The token after the name must be '='
    return ctoks[start + 1].kind == TokenKind.OP and ctoks[start + 1].value == '='


def _split_assignment(ctoks: list) -> tuple[str | None, list]:
    """Return (lhs_name, rhs_tokens) for a simple assignment, or (None, [])."""
    start = _leading_skip(ctoks)
    if start + 2 > len(ctoks):
        return None, []
    lhs = ctoks[start]
    eq  = ctoks[start + 1]
    if lhs.kind != TokenKind.IDENT or eq.value != '=':
        return None, []
    return lhs.value, ctoks[start + 2:]


def _substitute(ctoks: list, env: dict, edits: list) -> list:
    """Replace bare IDENT tokens whose name is in env with their constant literal.
    Appends (start, end, replacement) to edits. Returns a copy of ctoks with
    substituted values (as STRING/NUMBER tokens) for re-resolution."""
    result = []
    for t in ctoks:
        if (t.kind == TokenKind.IDENT
                and t.upper not in _VBS_RESERVED
                and t.upper in env):
            val = env[t.upper]
            rep = _const_to_literal(val)
            edits.append((t.start, t.end, rep))
            # Return a fake token list entry for the resolver
            from vbsdeoblib.tokenizer import VbsToken
            result.append(VbsToken(
                kind=TokenKind.STRING if isinstance(val, str) else TokenKind.NUMBER,
                value=rep,
                start=t.start,
                end=t.end,
            ))
        else:
            result.append(t)
    return result


def _is_self_referencing(lhs_upper: str, rhs_toks: list) -> bool:
    """Return True if *lhs_upper* appears as a bare IDENT in *rhs_toks*."""
    return any(
        t.kind == TokenKind.IDENT and t.upper == lhs_upper
        for t in rhs_toks
    )


# VBScript keywords and built-in names that should never be substituted.
_VBS_RESERVED = frozenset("""
AND BYREF BYVAL CALL CASE CLASS CONST DIM DO EACH ELSE ELSEIF END ERASE ERROR
EXECUTE EXECUTEGLOBAL EXIT FALSE FOR FUNCTION GET IF IN IS LET LOOP MOD NEW
NEXT NOT NOTHING NULL OBJECT ON OPTION OR PRESERVE PRIVATE PUBLIC RANDOMIZE REDIM
REM RESUME SELECT SET STEP STOP SUB THEN TO TRUE UNTIL WEND WHILE WITH XOR
CHR ASC LEN UCASE LCASE TRIM LTRIM RTRIM CSTR CINT CDBL CBOOL MID LEFT RIGHT
REPLACE INSTR INSTRREV STRREVERSE SPACE STRING HEX OCT ABS INT FIX SQR
CREATEOBJECT GETOBJECT WSCRIPT MSGBOX INPUTBOX NOW DATE TIME TIMER
""".split())


if __name__ == '__main__':
    run_tool(run, description='Flow-sensitive constant propagation across variable assignments')
