"""vbs_fold_split_calls — fold Split(expr, delim, limit, compare) calls with
constant args to an Array(...) literal.

VBScript signature: Split(expression[, delimiter[, limit[, compare]]])
  - delimiter defaults to " " when omitted.
  - limit     defaults to -1 (no limit) when omitted.
  - compare   defaults to 0 (vbBinaryCompare) when omitted; only 0 and 1
              (vbTextCompare) are supported — any other resolved compare mode
              declines the fold rather than guess, same convention InStr
              folding uses in vbsdeoblib/resolver.py.

Split() returns an array, not a scalar, so it doesn't fit resolve_const's
scalar Const contract (see resolver.py docstring) and isn't in PURE_BUILTINS.
This tool resolves each argument independently via resolve_const, computes
the real VBScript Split result in Python, and rewrites the call span to
Array("a","b",...) — a VBScript expression that behaves identically to the
original Split() result for every downstream consumer (UBound, indexing,
For ... Next).

Calls preceded by '.' (member access) and calls to a name the script itself
redefines via Function/Sub are left untouched, same guards as
vbs_fold_builtin_calls.py.

Usage:
    python vbs_fold_split_calls.py --input in.vbs --output out.vbs
"""
import re
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs
from vbsdeoblib.resolver import resolve_const

_USER_DEF_RE = re.compile(r'(?im)^[ \t]*(?:Function|Sub)\s+(\w+)\s*\(')


def run(src: str, env: dict | None = None, **_) -> tuple[str, dict]:
    shadowed = {m.group(1).upper() for m in _USER_DEF_RE.finditer(src)}
    changed_total = 0
    for _ in range(200):
        src, n = _one_pass(src, env or {}, shadowed)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


def _one_pass(src: str, env: dict, shadowed: set[str]) -> tuple[str, int]:
    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []
    used: set[int] = set()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind == TokenKind.IDENT and tok.upper == 'SPLIT' and tok.upper not in shadowed:
            # Reject member access: preceding non-WS token must not be '.'
            p = i - 1
            while p >= 0 and tokens[p].kind == TokenKind.WS:
                p -= 1
            if p >= 0 and tokens[p].kind == TokenKind.OP and tokens[p].value == '.':
                i += 1
                continue
            # Look for '(' immediately after (skip WS)
            j = i + 1
            while j < len(tokens) and tokens[j].kind == TokenKind.WS:
                j += 1
            if j < len(tokens) and tokens[j].kind == TokenKind.OP and tokens[j].value == '(':
                close = _find_matching_paren(tokens, j)
                if close is not None and not any(idx in used for idx in range(i, close + 1)):
                    arg_toks = tokens[j + 1:close]
                    rep = _try_fold_split(arg_toks, env)
                    if rep is not None:
                        edits.append((tok.start, tokens[close].end, rep))
                        for idx in range(i, close + 1):
                            used.add(idx)
                        i = close + 1
                        continue
        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


def _find_matching_paren(tokens: list, open_idx: int) -> int | None:
    depth = 0
    for k in range(open_idx, len(tokens)):
        if tokens[k].kind == TokenKind.OP and tokens[k].value == '(':
            depth += 1
        elif tokens[k].kind == TokenKind.OP and tokens[k].value == ')':
            depth -= 1
            if depth == 0:
                return k
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


def _try_fold_split(arg_toks: list, env: dict) -> str | None:
    args = _split_top_level_args(arg_toks)
    if args is None or not (1 <= len(args) <= 4):
        return None

    expr_val = resolve_const(args[0], env)
    if expr_val is None:
        return None
    expr = str(expr_val)

    delim = ' '
    if len(args) >= 2:
        delim_val = resolve_const(args[1], env)
        if delim_val is None:
            return None
        delim = str(delim_val)

    limit = -1
    if len(args) >= 3:
        limit_val = resolve_const(args[2], env)
        if limit_val is None:
            return None
        try:
            limit = int(limit_val)
        except (TypeError, ValueError):
            return None

    compare = 0
    if len(args) >= 4:
        compare_val = resolve_const(args[3], env)
        if compare_val is None:
            return None
        try:
            compare = int(compare_val)
        except (TypeError, ValueError):
            return None
        if compare not in (0, 1):
            return None  # unsupported compare mode — decline rather than guess

    try:
        parts = _vbs_split(expr, delim, limit, compare)
    except Exception:
        return None

    if not parts:
        return 'Array()'
    return 'Array(' + ', '.join(quote_vbs(p) for p in parts) + ')'


def _vbs_split(expr: str, delim: str, limit: int, compare: int) -> list[str]:
    """Faithful VBScript Split() semantics."""
    if expr == '':
        return ['']
    if delim == '':
        return [expr]
    if limit == 0:
        return []

    maxsplit = -1 if limit == -1 else max(limit - 1, 0)

    if compare == 1:
        pattern = re.escape(delim)
        py_maxsplit = 0 if maxsplit == -1 else maxsplit
        return re.split(pattern, expr, maxsplit=py_maxsplit, flags=re.IGNORECASE)

    if maxsplit == -1:
        return expr.split(delim)
    return expr.split(delim, maxsplit)


if __name__ == '__main__':
    run_tool(run, description='Fold Split(expr, delim, limit, compare) calls with constant args to Array(...) literals')
