"""vbs_fold_array_join_loops — fold a For...UBound array-join loop into a
single string literal.

Targets the idiom:
    accum = "<const>"
    For idx = <constStart> To UBound(arrName)
    accum = accum & arrName(idx)
    Next

where arrName's nearest prior write is a literal `arrName = Array(e0, e1, ...)`
with every element constant. Since the array contents and iteration count are
both statically known, the loop's entire effect is computable ahead of time:
the whole 4-statement block collapses to `accum = "<joined literal>"`.

Complements vbs_fold_split_calls: that tool turns `Split(str1, str2)` into an
Array(...) literal; this one finishes the job by folding the loop that walks
such an array back into a string — the other half of the "shatter a payload
string into an array so it never appears contiguous to a scanner, reassemble
it at runtime with a trivial loop" obfuscation technique.

Deliberately conservative: any deviation from the exact shape above (extra
statements in the loop body, a non-zero/non-constant Step, the array
reassigned to something other than a literal Array() between definition and
use, no constant initializer immediately before the loop, ...) leaves the
loop completely untouched.

Usage:
    python vbs_fold_array_join_loops.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs, format_number
from vbsdeoblib.resolver import resolve_const, Const
from vbsdeoblib.statements import split_statements, StatementSpan


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(50):
        src, n = _one_pass(src)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


def _one_pass(src: str) -> tuple[str, int]:
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    real_idx = [i for i, s in enumerate(stmts) if s.code_tokens()]

    edits: list[tuple[int, int, str]] = []
    loops_folded = 0

    for pos, i in enumerate(real_idx):
        ctoks = stmts[i].code_tokens()
        if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'FOR'):
            continue

        header = _parse_for_header(ctoks)
        if header is None:
            continue
        idx_name, arr_name, start_toks, step_toks = header

        start_val = resolve_const(start_toks, {})
        if not _is_nonneg_int(start_val):
            continue
        start_val = int(start_val)

        if step_toks is not None:
            step_val = resolve_const(step_toks, {})
            if step_val is None or step_val != 1:
                continue

        next_i = _find_matching_next(stmts, i)
        if next_i is None:
            continue

        body_positions = [k for k in real_idx if i < k < next_i]
        if len(body_positions) != 1:
            continue
        accum_name = _match_accum_body(stmts[body_positions[0]].code_tokens(), arr_name, idx_name)
        if accum_name is None:
            continue

        arr_elems = _find_array_literal(stmts, real_idx, pos, arr_name)
        if arr_elems is None:
            continue
        sliced = arr_elems[start_val:]

        if pos == 0:
            continue
        init_stmt_idx = real_idx[pos - 1]
        init_stmt = stmts[init_stmt_idx]
        init_val = _match_simple_const_assign(init_stmt.code_tokens(), accum_name)
        if init_val is None:
            continue

        joined = _to_str(init_val) + ''.join(_to_str(e) for e in sliced)
        indent = _line_indent(src, init_stmt.start)
        next_toks = stmts[next_i].tokens
        terminator = (src[next_toks[-1].start:next_toks[-1].end]
                      if next_toks and next_toks[-1].kind == TokenKind.NEWLINE else '')
        new_init = f'{indent}{accum_name} = {quote_vbs(joined)}{terminator}'

        edits.append((init_stmt.start, init_stmt.end, new_init))
        edits.append((stmts[i].start, stmts[next_i].end, ''))
        loops_folded += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), loops_folded


# ---------------------------------------------------------------------------
# For-header structural parse
# ---------------------------------------------------------------------------

def _parse_for_header(ctoks: list) -> tuple[str, str, list, list | None] | None:
    """Match 'For idx = startExpr To UBound(arrName) [Step stepExpr]'.
    Returns (idx_name, arr_name, start_toks, step_toks_or_None) or None."""
    if len(ctoks) < 7:
        return None
    if not (ctoks[1].kind == TokenKind.IDENT and ctoks[1].upper not in _RESERVED):
        return None
    idx_name = ctoks[1].value
    if not (ctoks[2].kind == TokenKind.OP and ctoks[2].value == '='):
        return None

    to_idx = _find_top_level_kw(ctoks, 3, 'TO')
    if to_idx is None or to_idx == 3:
        return None
    start_toks = ctoks[3:to_idx]

    step_idx = _find_top_level_kw(ctoks, to_idx + 1, 'STEP')
    end_toks = ctoks[to_idx + 1: step_idx if step_idx is not None else len(ctoks)]
    step_toks = ctoks[step_idx + 1:] if step_idx is not None else None
    if step_toks is not None and not step_toks:
        return None

    if len(end_toks) != 4:
        return None
    ub, lp, arr_tok, rp = end_toks
    if not (ub.kind == TokenKind.IDENT and ub.upper == 'UBOUND'):
        return None
    if not (lp.kind == TokenKind.OP and lp.value == '('):
        return None
    if arr_tok.kind != TokenKind.IDENT:
        return None
    if not (rp.kind == TokenKind.OP and rp.value == ')'):
        return None

    return idx_name, arr_tok.value, start_toks, step_toks


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


def _is_nonneg_int(v) -> bool:
    if not isinstance(v, (int, float)):
        return False
    return v == int(v) and int(v) >= 0


# ---------------------------------------------------------------------------
# Matching Next (FOR/NEXT depth tracking)
# ---------------------------------------------------------------------------

def _find_matching_next(stmts: list[StatementSpan], for_i: int) -> int | None:
    depth = 1
    for j in range(for_i + 1, len(stmts)):
        ctoks = stmts[j].code_tokens()
        if not ctoks or ctoks[0].kind != TokenKind.IDENT:
            continue
        kw = ctoks[0].upper
        if kw == 'FOR':
            depth += 1
        elif kw == 'NEXT':
            depth -= 1
            if depth == 0:
                return j
    return None


# ---------------------------------------------------------------------------
# Body / initializer / array-literal structural matches
# ---------------------------------------------------------------------------

def _match_accum_body(ctoks: list, arr_name: str, idx_name: str) -> str | None:
    """Match 'accum = accum (&|+) arrName(idxName)' exactly. Returns accum
    name (original casing) or None."""
    if len(ctoks) != 8:
        return None
    lhs, eq, rhs1, op, arr_tok, lp, idx_tok, rp = ctoks
    if lhs.kind != TokenKind.IDENT:
        return None
    if not (eq.kind == TokenKind.OP and eq.value == '='):
        return None
    if not (rhs1.kind == TokenKind.IDENT and rhs1.upper == lhs.upper):
        return None
    if not (op.kind == TokenKind.OP and op.value in ('&', '+')):
        return None
    if not (arr_tok.kind == TokenKind.IDENT and arr_tok.upper == arr_name.upper()):
        return None
    if not (lp.kind == TokenKind.OP and lp.value == '('):
        return None
    if not (idx_tok.kind == TokenKind.IDENT and idx_tok.upper == idx_name.upper()):
        return None
    if not (rp.kind == TokenKind.OP and rp.value == ')'):
        return None
    return lhs.value


def _match_simple_const_assign(ctoks: list, name: str) -> Const | None:
    """Match 'name = <constExpr>' and return the resolved constant, or None."""
    if len(ctoks) < 3:
        return None
    if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == name.upper()):
        return None
    if not (ctoks[1].kind == TokenKind.OP and ctoks[1].value == '='):
        return None
    return resolve_const(ctoks[2:], {})


def _find_array_literal(stmts: list[StatementSpan], real_idx: list[int],
                         for_pos: int, arr_name: str) -> list[Const] | None:
    """Scan backward from the For header (at real_idx[for_pos]) for the
    nearest statement writing arr_name. Must be exactly
    'arrName = Array(e0, e1, ...)' with every element constant, or the
    array is treated as unresolvable (None)."""
    for k in range(for_pos - 1, -1, -1):
        ctoks = stmts[real_idx[k]].code_tokens()
        if len(ctoks) < 2:
            continue
        if not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == arr_name.upper()):
            continue
        if not (ctoks[1].kind == TokenKind.OP and ctoks[1].value == '='):
            continue
        rhs = ctoks[2:]
        if not (len(rhs) >= 3 and rhs[0].kind == TokenKind.IDENT and rhs[0].upper == 'ARRAY'
                and rhs[1].kind == TokenKind.OP and rhs[1].value == '('
                and rhs[-1].kind == TokenKind.OP and rhs[-1].value == ')'):
            return None  # nearest writer isn't a literal Array(...) call
        args = _split_top_level_args(rhs[2:-1])
        if args is None:
            return None
        elems: list[Const] = []
        for a in args:
            v = resolve_const(a, {})
            if v is None:
                return None
            elems.append(v)
        return elems
    return None


def _split_top_level_args(toks: list) -> list[list] | None:
    """Split a token list (no outer parens) into comma-separated top-level
    argument token-lists. Returns [] for an empty (whitespace-only) list,
    None on unbalanced parens."""
    if not any(t.kind not in (TokenKind.WS,) for t in toks):
        return []
    args: list[list] = []
    current: list = []
    depth = 0
    for t in toks:
        if t.kind == TokenKind.OP and t.value == '(':
            depth += 1
            current.append(t)
        elif t.kind == TokenKind.OP and t.value == ')':
            depth -= 1
            if depth < 0:
                return None
            current.append(t)
        elif t.kind == TokenKind.OP and t.value == ',' and depth == 0:
            args.append(current)
            current = []
        else:
            current.append(t)
    args.append(current)
    return args


def _to_str(v: Const) -> str:
    return v if isinstance(v, str) else format_number(v)


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
    run_tool(run, description='Fold a For...UBound array-join loop into a single string literal')
