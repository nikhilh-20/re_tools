"""vbs_fold_constant_loops — fold a bounded loop with fully-constant inputs
and a straight-line body into its final result.

Generalizes the idea vbs_fold_array_join_loops already embodies (a loop over
statically-known data is computable ahead of time) to arbitrary VBScript loop
shapes (For / Do While / Do Until / Do...Loop While/Until / While...Wend) and
arbitrary straight-line bodies of plain assignment statements — not just one
accumulator over one Array() literal.

Targets loops like a custom-alphabet + rolling-XOR decode:
    idx = 1 : key = 245
    Do Until idx > Len(enc)
        hi  = InStr(alphaA, Mid(enc, idx, 1)) - 1
        lo  = InStr(alphaB, Mid(enc, idx+1, 1)) - 1
        b   = (hi * 16) Or lo
        b   = b Xor key
        out = out & Chr(b)
        key = (key * 123 + 161) And 255
        idx = idx + 2
    Loop
where every input (enc, alphaA, alphaB, and the loop's own starting values) is
already a constant literal by the time this tool runs. This tool contains no
knowledge of any particular alphabet/key-schedule/algorithm — it just
simulates the body statement-by-statement via the shared resolve_const
evaluator with an evolving scalar environment, the same way every other fold
pass in this toolkit evaluates expressions generically rather than pattern
matching a specific sample's constants.

Deliberately narrow (decline — leave the loop untouched — on any deviation),
matching every other tool's convention:
  - The loop's trip count/termination must be provable via resolve_const at
    every step (a condition that fails to resolve aborts the fold).
  - The body must be straight-line: no nested For/Do/While/If/Select/With/
    Function/Sub/Class/Property. A loop with a conditional or nested loop in
    its body is left completely untouched (v1 scope).
  - Every body statement must be a plain 'name = expr' (optionally
    'Let name = expr') assignment; anything else (Call, method invocation,
    Set, object creation, ...) aborts the fold.
  - Bounded by --max-iterations (default 5,000,000) as a safety cap against
    pathological/adversarial loops; exceeding it aborts only that loop's fold.

Recommended chain position: run this AFTER vbs_fold_builtin_calls,
vbs_propagate_constants, and vbs_fold_concat (so everything the loop reads
from outside itself is already a literal), and BEFORE vbs_remove_deadcode
(which cleans up now-unused inputs like the alphabet/blob strings once
nothing reads them anymore).

Usage:
    python vbs_fold_constant_loops.py --input in.vbs --output out.vbs [--max-iterations N]
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs, format_number
from vbsdeoblib.resolver import resolve_const, Const
from vbsdeoblib.statements import split_statements, StatementSpan, find_block_end, opens_block, closes_block


DEFAULT_MAX_ITERATIONS = 5_000_000


def run(src: str, max_iterations: int = DEFAULT_MAX_ITERATIONS, **_) -> tuple[str, dict]:
    changed_total = 0
    folded_total = 0
    declined_total = 0
    for _ in range(50):
        src, n, f, d = _one_pass(src, max_iterations)
        changed_total += n
        folded_total += f
        declined_total += d
        if n == 0:
            break
    return src, {'changed': changed_total, 'loops_folded': folded_total,
                 'loops_declined': declined_total}


def _one_pass(src: str, max_iterations: int) -> tuple[str, int, int, int]:
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    real_idx = [i for i, s in enumerate(stmts) if s.code_tokens()]

    edits: list[tuple[int, int, str]] = []
    folded = 0
    declined = 0

    for pos, i in enumerate(real_idx):
        ctoks = stmts[i].code_tokens()
        if not ctoks or ctoks[0].kind != TokenKind.IDENT:
            continue
        kw = ctoks[0].upper

        if kw == 'FOR':
            result = _try_for(stmts, real_idx, pos, i, ctoks, max_iterations)
        elif kw == 'DO':
            result = _try_do(stmts, real_idx, pos, i, ctoks, max_iterations)
        elif kw == 'WHILE':
            result = _try_while(stmts, real_idx, pos, i, ctoks, max_iterations)
        else:
            continue

        if result is None or result == 'DECLINE':
            if result == 'DECLINE':
                declined += 1
            continue

        close_i, final_env, changed_vars = result
        indent = _line_indent(src, stmts[i].start)
        lines = [f'{indent}{name} = {_format_const(final_env[name.upper()])}'
                 for name in changed_vars]
        close_toks = stmts[close_i].tokens
        terminator = (src[close_toks[-1].start:close_toks[-1].end]
                      if close_toks and close_toks[-1].kind == TokenKind.NEWLINE else '')
        replacement = ('\n'.join(lines) + terminator) if lines else terminator
        edits.append((stmts[i].start, stmts[close_i].end, replacement))
        folded += 1

    if not edits:
        return src, 0, folded, declined
    return apply_edits(src, edits), len(edits), folded, declined


# ---------------------------------------------------------------------------
# Per-loop-shape handlers. Each returns:
#   None        -> this statement isn't a (recognisable) loop header at all
#   'DECLINE'   -> it is a loop header, but folding was declined
#   (close_i, final_env, changed_vars) -> success
# ---------------------------------------------------------------------------

def _try_for(stmts, real_idx, pos, open_i, ctoks, max_iterations):
    header = _parse_for_header(ctoks)
    if header is None:
        return None
    var_name, start_toks, end_toks, step_toks = header

    close_i = find_block_end(stmts, open_i)
    if close_i is None:
        return 'DECLINE'

    body_positions = [k for k in real_idx if open_i < k < close_i]
    if not body_positions or _body_has_nesting(stmts, body_positions):
        return 'DECLINE'
    body_assigns = _extract_body_assigns(stmts, body_positions)
    if body_assigns is None:
        return 'DECLINE'
    if any(name_tok.upper == var_name.upper() for name_tok, _ in body_assigns):
        return 'DECLINE'   # body reassigns the loop variable itself

    env = _build_pre_env(stmts, real_idx, pos)

    start_val = resolve_const(start_toks, env)
    end_val = resolve_const(end_toks, env)
    step_val = resolve_const(step_toks, env) if step_toks is not None else 1
    if not _is_num(start_val) or not _is_num(end_val) or not _is_num(step_val) or step_val == 0:
        return 'DECLINE'

    var_up = var_name.upper()
    env[var_up] = start_val
    written: set[str] = set()
    iterations = 0
    while (step_val > 0 and env[var_up] <= end_val) or (step_val < 0 and env[var_up] >= end_val):
        iterations += 1
        if iterations > max_iterations:
            return 'DECLINE'
        if not _run_body(body_assigns, env, written):
            return 'DECLINE'
        env[var_up] = env[var_up] + step_val

    changed_vars = [orig for up, orig in _ordered_unique_names(body_assigns) if up in written]
    if not changed_vars:
        return 'DECLINE'
    return close_i, env, changed_vars


def _try_do(stmts, real_idx, pos, open_i, ctoks, max_iterations):
    header = _parse_do_header(ctoks)
    if header is None:
        return None
    test_pos, cond_kind, cond_toks = header

    close_i = find_block_end(stmts, open_i)
    if close_i is None:
        return 'DECLINE'
    close_ctoks = stmts[close_i].code_tokens()

    if test_pos == 'PRE':
        if len(close_ctoks) != 1:
            return 'DECLINE'   # 'Loop' must be bare when the test is on 'Do'
    else:
        parsed_close = _parse_loop_closer(close_ctoks)
        if parsed_close is None or parsed_close[1] is None:
            return 'DECLINE'   # bare 'Do ... Loop' with no condition anywhere -> can't prove termination
        cond_kind, cond_toks = parsed_close

    body_positions = [k for k in real_idx if open_i < k < close_i]
    if not body_positions or _body_has_nesting(stmts, body_positions):
        return 'DECLINE'
    body_assigns = _extract_body_assigns(stmts, body_positions)
    if body_assigns is None:
        return 'DECLINE'

    env = _build_pre_env(stmts, real_idx, pos)
    written: set[str] = set()
    iterations = 0

    if test_pos == 'PRE':
        while True:
            truthy = _truthy(resolve_const(cond_toks, env))
            if truthy is None:
                return 'DECLINE'
            if not (truthy if cond_kind == 'WHILE' else not truthy):
                break
            iterations += 1
            if iterations > max_iterations:
                return 'DECLINE'
            if not _run_body(body_assigns, env, written):
                return 'DECLINE'
    else:
        while True:
            iterations += 1
            if iterations > max_iterations:
                return 'DECLINE'
            if not _run_body(body_assigns, env, written):
                return 'DECLINE'
            truthy = _truthy(resolve_const(cond_toks, env))
            if truthy is None:
                return 'DECLINE'
            if not (truthy if cond_kind == 'WHILE' else not truthy):
                break

    changed_vars = [orig for up, orig in _ordered_unique_names(body_assigns) if up in written]
    if not changed_vars:
        return 'DECLINE'
    return close_i, env, changed_vars


def _try_while(stmts, real_idx, pos, open_i, ctoks, max_iterations):
    cond_toks = _parse_while_header(ctoks)
    if cond_toks is None:
        return None

    close_i = find_block_end(stmts, open_i)
    if close_i is None:
        return 'DECLINE'
    close_ctoks = stmts[close_i].code_tokens()
    if not (len(close_ctoks) == 1 and close_ctoks[0].kind == TokenKind.IDENT
            and close_ctoks[0].upper == 'WEND'):
        return 'DECLINE'

    body_positions = [k for k in real_idx if open_i < k < close_i]
    if not body_positions or _body_has_nesting(stmts, body_positions):
        return 'DECLINE'
    body_assigns = _extract_body_assigns(stmts, body_positions)
    if body_assigns is None:
        return 'DECLINE'

    env = _build_pre_env(stmts, real_idx, pos)
    written: set[str] = set()
    iterations = 0
    while True:
        truthy = _truthy(resolve_const(cond_toks, env))
        if truthy is None:
            return 'DECLINE'
        if not truthy:
            break
        iterations += 1
        if iterations > max_iterations:
            return 'DECLINE'
        if not _run_body(body_assigns, env, written):
            return 'DECLINE'

    changed_vars = [orig for up, orig in _ordered_unique_names(body_assigns) if up in written]
    if not changed_vars:
        return 'DECLINE'
    return close_i, env, changed_vars


# ---------------------------------------------------------------------------
# Header parsers
# ---------------------------------------------------------------------------

def _parse_for_header(ctoks: list) -> tuple[str, list, list, list | None] | None:
    """Match 'For var = startExpr To endExpr [Step stepExpr]'. Unlike
    vbs_fold_array_join_loops, endExpr may be any expression (Len(...),
    UBound(...), a literal, arithmetic), not just UBound(arrName) — this
    tool doesn't need an Array() source, only a resolvable end value."""
    if len(ctoks) < 5:
        return None
    if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'FOR'):
        return None
    if not (ctoks[1].kind == TokenKind.IDENT and ctoks[1].upper not in _RESERVED):
        return None
    var_name = ctoks[1].value
    if not (ctoks[2].kind == TokenKind.OP and ctoks[2].value == '='):
        return None

    to_idx = _find_top_level_kw(ctoks, 3, 'TO')
    if to_idx is None or to_idx == 3:
        return None
    start_toks = ctoks[3:to_idx]

    step_idx = _find_top_level_kw(ctoks, to_idx + 1, 'STEP')
    end_toks = ctoks[to_idx + 1: step_idx if step_idx is not None else len(ctoks)]
    step_toks = ctoks[step_idx + 1:] if step_idx is not None else None
    if not end_toks:
        return None
    if step_toks is not None and not step_toks:
        return None

    return var_name, start_toks, end_toks, step_toks


def _parse_do_header(ctoks: list) -> tuple[str, str | None, list | None] | None:
    """Match 'Do' (bare, post-test) or 'Do While|Until condExpr' (pre-test)."""
    if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'DO'):
        return None
    if len(ctoks) == 1:
        return 'POST', None, None
    if ctoks[1].kind == TokenKind.IDENT and ctoks[1].upper in ('WHILE', 'UNTIL'):
        cond_toks = ctoks[2:]
        if not cond_toks:
            return None
        return 'PRE', ctoks[1].upper, cond_toks
    return None


def _parse_loop_closer(ctoks: list) -> tuple[str | None, list | None] | None:
    """Match 'Loop' (bare) or 'Loop While|Until condExpr'."""
    if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'LOOP'):
        return None
    if len(ctoks) == 1:
        return None, None
    if ctoks[1].kind == TokenKind.IDENT and ctoks[1].upper in ('WHILE', 'UNTIL'):
        cond_toks = ctoks[2:]
        if not cond_toks:
            return None
        return ctoks[1].upper, cond_toks
    return None


def _parse_while_header(ctoks: list) -> list | None:
    """Match 'While condExpr' (block form — VBScript's While is always
    While...Wend, there is no single-line form)."""
    if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'WHILE'):
        return None
    cond_toks = ctoks[1:]
    return cond_toks if cond_toks else None


def _find_top_level_kw(ctoks: list, start: int, kw: str) -> int | None:
    depth = 0
    for i in range(start, len(ctoks)):
        t = ctoks[i]
        if t.kind == TokenKind.OP and t.value == '(':
            depth += 1
        elif t.kind == TokenKind.OP and t.value == ')':
            depth -= 1
        elif depth == 0 and t.kind == TokenKind.IDENT and t.upper == kw:
            return i
    return None


# ---------------------------------------------------------------------------
# Body extraction / simulation
# ---------------------------------------------------------------------------

def _body_has_nesting(stmts: list[StatementSpan], body_positions: list[int]) -> bool:
    return any(opens_block(stmts[k].code_tokens()) for k in body_positions)


def _match_simple_assignment(ctoks: list):
    """Return (name_tok, rhs_toks) for a bare 'name = rhs' or 'Let name =
    rhs' statement, or None for anything else (Dim/Const/ReDim/Set, a call,
    an If, ...) — those all abort the fold for the whole loop."""
    if not ctoks:
        return None
    if ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper in ('DIM', 'CONST', 'REDIM', 'SET'):
        return None
    idx = 1 if (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'LET') else 0
    if idx >= len(ctoks) - 1:
        return None
    name_tok, eq_tok = ctoks[idx], ctoks[idx + 1]
    if name_tok.kind != TokenKind.IDENT or name_tok.upper in _RESERVED:
        return None
    if not (eq_tok.kind == TokenKind.OP and eq_tok.value == '='):
        return None
    return name_tok, ctoks[idx + 2:]


def _extract_body_assigns(stmts: list[StatementSpan], body_positions: list[int]):
    assigns = []
    for k in body_positions:
        parsed = _match_simple_assignment(stmts[k].code_tokens())
        if parsed is None:
            return None
        assigns.append(parsed)
    return assigns


def _run_body(body_assigns: list, env: dict, written: set) -> bool:
    for name_tok, rhs_toks in body_assigns:
        val = resolve_const(rhs_toks, env)
        if val is None:
            return False
        env[name_tok.upper] = val
        written.add(name_tok.upper)
    return True


def _ordered_unique_names(body_assigns: list) -> list[tuple[str, str]]:
    """[(NAME_UPPER, original_case_spelling), ...] in first-occurrence order."""
    seen = set()
    out = []
    for name_tok, _ in body_assigns:
        if name_tok.upper not in seen:
            seen.add(name_tok.upper)
            out.append((name_tok.upper, name_tok.value))
    return out


def _build_pre_env(stmts: list[StatementSpan], real_idx: list[int], pos: int) -> dict:
    """Constant env of every variable's last known value from a top-level
    (block-depth 0) forward scan of every statement before the loop. Content
    inside any nested block (If/Function/...) is skipped entirely — neither
    tracked nor treated as killing an outer value — a conservative
    approximation appropriate for this tool's recommended position after
    vbs_propagate_constants has already done real flow-sensitive tracking."""
    env: dict[str, Const] = {}
    depth = 0
    for k in real_idx[:pos]:
        ctoks = stmts[k].code_tokens()
        if not ctoks:
            continue
        if closes_block(ctoks):
            depth = max(0, depth - 1)
            continue
        is_open = opens_block(ctoks)
        cur_depth = depth
        if is_open:
            depth += 1
        if cur_depth != 0 or is_open:
            continue
        assign = _match_simple_assignment(ctoks)
        if assign is None:
            continue
        name_tok, rhs_toks = assign
        val = resolve_const(rhs_toks, env)
        if val is not None:
            env[name_tok.upper] = val
        else:
            env.pop(name_tok.upper, None)
    return env


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, (int, float))


def _truthy(v) -> bool | None:
    """VBScript boolean coercion for a resolved condition value. Returns
    None (unresolvable) for anything that isn't already numeric — real
    conditions here are always comparisons/And-Or-Xor chains, which the
    resolver already reduces to -1/0, so a non-numeric result means the
    condition wasn't actually foldable and the loop must be declined."""
    if isinstance(v, (int, float)):
        return v != 0
    return None


def _format_const(v: Const) -> str:
    return quote_vbs(v) if isinstance(v, str) else format_number(v)


def _line_indent(src: str, offset: int) -> str:
    line_start = src.rfind('\n', 0, offset) + 1
    prefix = src[line_start:offset]
    return prefix if prefix.strip() == '' else ''


_RESERVED = frozenset("""
AND BYREF BYVAL CALL CASE CLASS CONST DIM DO EACH ELSE ELSEIF END ERASE ERROR
EXECUTE EXECUTEGLOBAL EXIT FALSE FOR FUNCTION GET IF IN IS LET LOOP MOD NEW
NEXT NOT NOTHING NULL OBJECT ON OPTION OR PRESERVE PRIVATE PUBLIC RANDOMIZE REDIM
REM RESUME SELECT SET STEP STOP SUB THEN TO TRUE UNTIL WEND WHILE WITH XOR
""".split())


if __name__ == '__main__':
    run_tool(
        run,
        description='Fold a bounded loop over fully-constant data (any shape, any straight-line body) into its final result',
        extra_args=[
            {
                'flags': ['--max-iterations'],
                'type': int,
                'default': DEFAULT_MAX_ITERATIONS,
                'help': f'Safety cap on simulated iterations per loop (default {DEFAULT_MAX_ITERATIONS})',
            },
        ],
    )
