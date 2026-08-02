"""vbs_fold_concat — fold & -concatenation chains of constant terms to one literal.

Handles: "x" & "y"  /  "x" & Chr(65)  /  variable & "y" (when variable known).
Analog of PsFold-Strings (the & operator form).

The pass finds the outermost & chain where every operand resolves to a constant
(string or number coerced to string via VBS rules) and replaces the whole chain
with a single quoted string literal.

Usage:
    python vbs_fold_concat.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs
from vbsdeoblib.resolver import resolve_const, Const


def run(src: str, env: dict | None = None, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(200):
        src, n = _one_pass(src, env or {})
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


def _one_pass(src: str, env: dict) -> tuple[str, int]:
    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []
    used: set[int] = set()

    # Find every '&' operator at top-level or inside parens and try to resolve
    # the widest enclosing & chain.
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind in (TokenKind.STRING, TokenKind.COMMENT):
            i += 1
            continue
        if tok.kind == TokenKind.OP and tok.value in ('&', '+') and i not in used:
            # Walk left and right to find the full & chain.
            # The "chain" is a series of atoms separated by '&' at the same paren level.
            left, right = _expand_concat_chain(tokens, i)
            if left is not None and right is not None:
                span = tokens[left:right+1]
                if not any(idx in used for idx in range(left, right+1)):
                    val = resolve_const(span, env)
                    if val is not None:
                        # Must actually contain an '&' (not just a single literal)
                        has_amp_or_plus = any(
                            t.kind == TokenKind.OP and t.value in ('&', '+')
                            for t in span
                            if t.kind not in (TokenKind.WS, TokenKind.COMMENT)
                        )
                        if has_amp_or_plus and isinstance(val, str):
                            rep = quote_vbs(str(val))
                            edits.append((tokens[left].start, tokens[right].end, rep))
                            for idx in range(left, right+1):
                                used.add(idx)
                            i = right + 1
                            continue
        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


def _expand_concat_chain(tokens: list, amp_idx: int) -> tuple[int | None, int | None]:
    """Find the leftmost and rightmost token indices of the & chain containing
    tokens[amp_idx].  Returns (left_idx, right_idx) or (None, None) on failure."""
    n = len(tokens)

    def skip_ws_left(idx):
        idx -= 1
        while idx >= 0 and tokens[idx].kind == TokenKind.WS:
            idx -= 1
        return idx

    def skip_ws_right(idx):
        idx += 1
        while idx < n and tokens[idx].kind == TokenKind.WS:
            idx += 1
        return idx

    def find_atom_left(idx):
        """Return leftmost token index of the atom/paren-group to the left of idx."""
        idx = skip_ws_left(idx)
        if idx < 0:
            return None
        t = tokens[idx]
        if t.kind == TokenKind.OP and t.value == ')':
            # Find matching '('
            depth = 0
            k = idx
            while k >= 0:
                if tokens[k].kind == TokenKind.OP:
                    if tokens[k].value == ')':
                        depth += 1
                    elif tokens[k].value == '(':
                        depth -= 1
                        if depth == 0:
                            return k
                k -= 1
            return None
        if t.kind in (TokenKind.STRING, TokenKind.NUMBER, TokenKind.IDENT):
            return idx
        return None

    def find_atom_right(idx):
        """Return rightmost token index of the atom/paren-group to the right of idx."""
        idx = skip_ws_right(idx)
        if idx >= n:
            return None
        t = tokens[idx]
        if t.kind == TokenKind.OP and t.value == '(':
            depth = 0
            k = idx
            while k < n:
                if tokens[k].kind == TokenKind.OP:
                    if tokens[k].value == '(':
                        depth += 1
                    elif tokens[k].value == ')':
                        depth -= 1
                        if depth == 0:
                            return k
                k += 1
            return None
        # identifier followed by '(' is a function call
        if t.kind == TokenKind.IDENT:
            j = skip_ws_right(idx)
            if j < n and tokens[j].kind == TokenKind.OP and tokens[j].value == '(':
                # find matching ')'
                depth = 0
                k = j
                while k < n:
                    if tokens[k].kind == TokenKind.OP:
                        if tokens[k].value == '(':
                            depth += 1
                        elif tokens[k].value == ')':
                            depth -= 1
                            if depth == 0:
                                return k
                    k += 1
                return None
            return idx  # bare identifier
        if t.kind in (TokenKind.STRING, TokenKind.NUMBER):
            return idx
        return None

    # Seed: the left-atom of this '&' and the right-atom
    left_atom  = find_atom_left(amp_idx)
    right_atom = find_atom_right(amp_idx)
    if left_atom is None or right_atom is None:
        return None, None

    chain_left  = left_atom
    chain_right = right_atom

    # Expand leftward: if the left-atom is preceded by '&' or '+', include that chain.
    cur = chain_left
    while True:
        la = skip_ws_left(cur)
        if la < 0 or not (tokens[la].kind == TokenKind.OP and tokens[la].value in ('&', '+')):
            break
        new_left = find_atom_left(la)
        if new_left is None:
            break
        chain_left = new_left
        cur = chain_left

    # Expand rightward
    cur = chain_right
    while True:
        ra = skip_ws_right(cur)
        if ra >= n or not (tokens[ra].kind == TokenKind.OP and tokens[ra].value in ('&', '+')):
            break
        new_right = find_atom_right(ra)
        if new_right is None:
            break
        chain_right = new_right
        cur = chain_right

    return chain_left, chain_right


if __name__ == '__main__':
    run_tool(run, description='Fold & concatenation chains of constant terms to a single string literal')
