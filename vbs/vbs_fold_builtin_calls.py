"""vbs_fold_builtin_calls — fold calls to pure VBScript builtins with constant args.

Allowlisted: Chr, Asc, Len, UCase, LCase, Trim, LTrim, RTrim, CStr, CInt, CDbl,
             CBool, Hex, Oct, Abs, Int, Fix, Sqr, StrReverse, Space, Mid, Left,
             Right, Replace, InStr, String.

Any call not on the allowlist, or where any argument is non-constant, is left untouched.
Calls preceded by '.' (member access, e.g. `obj.Replace(...)`) and calls to a name
the script itself redefines via Function/Sub are also left untouched, since those are
not the builtin regardless of name.

Analog of PsFold-MethodChains / PsFold-StaticStringCalls.

Usage:
    python vbs_fold_builtin_calls.py --input in.vbs --output out.vbs
"""
import re
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs, format_number
from vbsdeoblib.resolver import resolve_const, PURE_BUILTINS, Const

_USER_DEF_RE = re.compile(r'(?im)^[ \t]*(?:Function|Sub)\s+(\w+)\s*\(')


def run(src: str, env: dict | None = None, **_) -> tuple[str, dict]:
    shadowed = {m.group(1).upper() for m in _USER_DEF_RE.finditer(src)} & PURE_BUILTINS.keys()
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
        if tok.kind == TokenKind.IDENT and tok.upper in PURE_BUILTINS and tok.upper not in shadowed:
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
                # Find matching ')'
                depth = 0
                k = j
                while k < len(tokens):
                    if tokens[k].kind == TokenKind.OP:
                        if tokens[k].value == '(':
                            depth += 1
                        elif tokens[k].value == ')':
                            depth -= 1
                            if depth == 0:
                                break
                    k += 1
                if k < len(tokens) and not any(idx in used for idx in range(i, k+1)):
                    span = tokens[i:k+1]
                    val = resolve_const(span, env)
                    if val is not None:
                        rep = quote_vbs(str(val)) if isinstance(val, str) else format_number(val)
                        edits.append((tok.start, tokens[k].end, rep))
                        for idx in range(i, k+1):
                            used.add(idx)
                        i = k + 1
                        continue
        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


if __name__ == '__main__':
    run_tool(run, description='Fold pure VBScript builtin calls with constant args to literals')
