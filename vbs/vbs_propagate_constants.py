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
from vbsdeoblib.statements import split_statements


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

    for stmt in stmts:
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
            block_kinds.append('LOOP' if loop_open else 'OTHER')
            block_depth += 1
            local_env.clear()
            continue

        # --- Inside a block body (depth > 0) ---
        if block_depth > 0:
            in_loop = 'LOOP' in block_kinds
            if _is_assignment(ctoks):
                lhs_name, rhs_toks = _split_assignment(ctoks)
                if lhs_name:
                    lhs_up = lhs_name.upper()
                    # Clear any local knowledge of this name *before*
                    # substituting RHS so a self-referencing update reads
                    # only its pre-this-statement value.
                    local_env.pop(lhs_up, None)
                    merged = env if in_loop else {**env, **local_env}
                    # Outer/global env never retains a block-local write —
                    # matches the original fully-conservative behaviour.
                    killed.add(lhs_up)
                    env.pop(lhs_up, None)
                    rhs_toks_sub = _substitute(rhs_toks, merged, edits)
                    if not in_loop:
                        # Straight-line, non-looping block: safe to track
                        # this as a local constant for later statements in
                        # the same run (a value computed here executes
                        # exactly once before any subsequent read of it).
                        val = resolve_const(rhs_toks_sub, merged)
                        if val is not None:
                            local_env[lhs_up] = val
                    # Inside a loop: never track (a value derived from one
                    # iteration's inputs is not valid for the next).
            else:
                merged = env if in_loop else {**env, **local_env}
                _substitute(ctoks, merged, edits)
            continue

        # --- Top-level assignment: [Set|Dim] name = expr  OR  name = expr ---
        if _is_assignment(ctoks):
            lhs_name, rhs_toks = _split_assignment(ctoks)
            if lhs_name:
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
                # Substitute known constants into the RHS for readability.
                rhs_toks_sub = _substitute(rhs_toks, sub_env, edits)
                if lhs_up not in killed:
                    val = resolve_const(rhs_toks_sub, sub_env)
                    if val is not None:
                        env[lhs_up] = val
                    else:
                        # RHS not constant — kill the name
                        killed.add(lhs_up)
                        env.pop(lhs_up, None)
                # If lhs is already killed, we still substituted the rhs — nothing more to do.
            continue

        # Top-level non-assignment: substitute known constants into the reads.
        _substitute(ctoks, env, edits)

    if not edits:
        return src, 0, 0
    new_src = apply_edits(src, edits)
    return new_src, len(edits), len(edits)


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
