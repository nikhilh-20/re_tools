"""vbs_fold_arithmetic — fold constant arithmetic to a numeric literal.

Targets: + - * / \\ Mod ^  over number literals (including &H and &O).
Does NOT fold when the result would be used as a string (leave that to
vbs_fold_concat and vbs_fold_chr_calls).

Analog of PsFold-Arithmetic.

Usage:
    python vbs_fold_arithmetic.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, format_number
from vbsdeoblib.resolver import resolve_const, _parse_number, Const


def _is_numeric(v: Const) -> bool:
    return isinstance(v, (int, float))


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(200):
        src, n = _one_pass(src)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


def _one_pass(src: str) -> tuple[str, int]:
    """Find the outermost arithmetic sub-expressions that resolve to a number
    and replace them with the literal.  We do this by:
    1. Scanning for NUMBER tokens.
    2. For each, expanding outward to find the largest parenthesised or
       operator-chained expression that still resolves to a pure number.
    3. Emitting one edit per such expression.
    """
    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []
    used: set[int] = set()   # token indices already covered by an edit

    # Strategy: find every '(' or NUMBER that is NOT inside a string/comment,
    # try to resolve the sub-expression spanning from there to the matching ')'.
    # This is simpler than full expression parsing because we just want to know
    # "does this paren-group evaluate to a number?".

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind in (TokenKind.STRING, TokenKind.COMMENT):
            i += 1
            continue

        if tok.kind == TokenKind.OP and tok.value == '(':
            # Try to resolve the entire parenthesised expression.
            depth = 0
            k = i
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
                val = resolve_const(span)
                if val is not None and _is_numeric(val):
                    # Make sure this isn't a trivial single-number literal
                    inner = [t for t in span[1:-1]
                             if t.kind not in (TokenKind.WS,)]
                    if not (len(inner) == 1 and inner[0].kind == TokenKind.NUMBER):
                        rep = format_number(val)
                        # Wrap negative in parens to avoid --
                        if isinstance(val, (int, float)) and val < 0:
                            rep = f'({rep})'
                        edits.append((tok.start, tokens[k].end, rep))
                        for idx in range(i, k+1):
                            used.add(idx)
                        i = k + 1
                        continue

        if tok.kind == TokenKind.NUMBER and i not in used:
            # Already a literal — skip.
            pass

        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


if __name__ == '__main__':
    run_tool(run, description='Fold constant arithmetic expressions to numeric literals')
