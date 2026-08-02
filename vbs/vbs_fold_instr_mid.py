"""vbs_fold_instr_mid — fold the Mid(S, InStr(n,S,"lit"), Len("lit")) identity.

Mid(S, InStr(n, S, L), Len(L)) is always equal to L whenever the InStr call
succeeds (VBScript raises on Mid(S, 0, ...), so on every surviving execution
path the identity holds) — regardless of what S actually contains. This is
pure algebra, not a guess about runtime data, and it defeats a common
character-harvesting obfuscation:

    pos = InStr(1, someBinaryBlob, "s")
    ch  = Mid(someBinaryBlob, pos, 1)      ' == "s", unconditionally

The InStr result is usually consumed through an intermediate position
variable (as above) rather than nested directly, so this pass tracks, per
variable, the most recent constant-needle InStr call assigned to it, and
folds any later Mid(subject, posvar, length) call that references the same
subject and a length equal to Len(needle).

Preconditions (all required — see module docstring in the plan for rationale):
  - InStr subject and needle: subject is a bare variable, needle is a string
    literal. 2-arg InStr(s, needle) or 3-arg InStr(start, s, needle) accepted
    (both imply binary compare); 4-arg accepted only when compare is the
    literal 0 (binary). vbTextCompare (1) is rejected — it can make Mid
    return content that differs in case from the needle.
  - Mid must be the 3-arg form with a length argument that resolves (via the
    shared constant resolver) to exactly Len(needle). The 2-arg Mid(s, start)
    form (no length — returns everything to the end of the string) is never
    folded, since nothing constrains what follows the match.
  - The position variable must not have been reassigned to anything else
    since the InStr call (tracked naturally: each new assignment overwrites
    or clears the tracked entry). The subject variable must not have been
    reassigned since the InStr call either (any assignment to a name that
    equals a tracked subject invalidates that entry).

Known limitation: mutation of the subject or position variable via ByRef
Sub/Function call arguments is not tracked (no interprocedural analysis) —
this mirrors the existing intra-file scope of vbs_propagate_constants.py.

Usage:
    python vbs_fold_instr_mid.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs
from vbsdeoblib.resolver import resolve_const
from vbsdeoblib.statements import split_statements


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(50):
        src, n = _one_pass(src)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total, 'folded': changed_total}


def _one_pass(src: str) -> tuple[str, int]:
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    if not stmts:
        return src, 0

    instr_calls: dict[str, tuple[str, str]] = {}   # posvar_upper -> (subject_upper, needle)
    edits: list[tuple[int, int, str]] = []

    for stmt in stmts:
        ctoks = stmt.code_tokens()
        if not ctoks:
            continue

        _scan_mid_calls(ctoks, instr_calls, edits)

        written = _match_simple_assignment(ctoks)
        if written is None:
            continue
        name, rhs_toks = written

        # Any tracked entry whose subject is the variable now being written
        # to is stale as of this statement.
        for k in [k for k, (subj, _n) in instr_calls.items() if subj == name]:
            instr_calls.pop(k, None)

        instr_info = _match_instr_call(rhs_toks)
        if instr_info is not None:
            instr_calls[name] = instr_info
        else:
            instr_calls.pop(name, None)

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


# ---------------------------------------------------------------------------
# Statement-level helpers
# ---------------------------------------------------------------------------

_ASSIGN_EXCLUDE_KW = frozenset(['DIM', 'CONST', 'REDIM', 'SET'])


def _match_simple_assignment(ctoks: list) -> tuple[str, list] | None:
    """Return (name_upper, rhs_tokens) for a bare 'name = rhs' or 'Let name =
    rhs' statement, else None. Dim/Const/ReDim/Set are excluded — none of
    them are plain string-variable assignments this pass needs to track."""
    if not ctoks:
        return None
    if ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper in _ASSIGN_EXCLUDE_KW:
        return None
    idx = 1 if (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'LET') else 0
    if idx >= len(ctoks) - 1:
        return None
    name_tok, eq_tok = ctoks[idx], ctoks[idx + 1]
    if name_tok.kind != TokenKind.IDENT:
        return None
    if not (eq_tok.kind == TokenKind.OP and eq_tok.value == '='):
        return None
    return name_tok.upper, ctoks[idx + 2:]


def _match_instr_call(rhs_toks: list) -> tuple[str, str] | None:
    """If rhs_toks is exactly a safe InStr(...) call — subject is a bare
    variable, needle is a (resolvable) string literal, compare is absent,
    implicit-binary, or the literal 0 — return (subject_upper, needle).
    Else None."""
    toks = rhs_toks
    if len(toks) < 4:
        return None
    if not (toks[0].kind == TokenKind.IDENT and toks[0].upper == 'INSTR'):
        return None
    if not (toks[1].kind == TokenKind.OP and toks[1].value == '('):
        return None
    if not (toks[-1].kind == TokenKind.OP and toks[-1].value == ')'):
        return None
    args = _split_top_level_args(toks[2:-1])
    if args is None:
        return None

    if len(args) == 2:
        subj_toks, needle_toks = args
    elif len(args) in (3, 4):
        subj_toks, needle_toks = args[1], args[2]
        if len(args) == 4:
            compare_val = resolve_const(args[3])
            if compare_val != 0:
                return None
    else:
        return None

    if len(subj_toks) != 1 or subj_toks[0].kind != TokenKind.IDENT:
        return None
    needle_val = resolve_const(needle_toks)
    if not isinstance(needle_val, str) or needle_val == '':
        return None
    return subj_toks[0].upper, needle_val


# ---------------------------------------------------------------------------
# Expression-level Mid(...) scan (can appear anywhere, not just as a whole
# statement's RHS)
# ---------------------------------------------------------------------------

def _scan_mid_calls(ctoks: list, instr_calls: dict, edits: list) -> None:
    n = len(ctoks)
    i = 0
    while i < n:
        t = ctoks[i]
        if (t.kind == TokenKind.IDENT and t.upper == 'MID'
                and i + 1 < n and ctoks[i + 1].kind == TokenKind.OP and ctoks[i + 1].value == '('):
            # Reject member access: obj.Mid(...) is not the builtin.
            p = i - 1
            if p >= 0 and ctoks[p].kind == TokenKind.OP and ctoks[p].value == '.':
                i += 1
                continue
            close = _find_matching_paren(ctoks, i + 1)
            if close is not None:
                inner = ctoks[i + 2: close]
                args = _split_top_level_args(inner)
                if args is not None and len(args) == 3:
                    literal = _try_fold_mid(args, instr_calls)
                    if literal is not None:
                        edits.append((t.start, ctoks[close].end, literal))
                i = close + 1
                continue
        i += 1


def _find_matching_paren(ctoks: list, open_idx: int) -> int | None:
    depth = 0
    for k in range(open_idx, len(ctoks)):
        if ctoks[k].kind == TokenKind.OP and ctoks[k].value == '(':
            depth += 1
        elif ctoks[k].kind == TokenKind.OP and ctoks[k].value == ')':
            depth -= 1
            if depth == 0:
                return k
    return None


def _split_top_level_args(toks: list) -> list[list] | None:
    """Split a token list (no outer parens) into comma-separated top-level
    argument token-lists. Returns None on unbalanced parens."""
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
    if depth != 0:
        return None
    return args


def _try_fold_mid(args: list, instr_calls: dict) -> str | None:
    subj_toks, pos_toks, len_toks = args
    if len(subj_toks) != 1 or subj_toks[0].kind != TokenKind.IDENT:
        return None
    if len(pos_toks) != 1 or pos_toks[0].kind != TokenKind.IDENT:
        return None
    entry = instr_calls.get(pos_toks[0].upper)
    if entry is None:
        return None
    instr_subject, needle = entry
    if instr_subject != subj_toks[0].upper:
        return None
    length_val = resolve_const(len_toks)
    if length_val != len(needle):
        return None
    return quote_vbs(needle)


if __name__ == '__main__':
    run_tool(run, description='Fold the Mid(S, InStr(n,S,"lit"), Len) identity to the literal')
