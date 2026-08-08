"""vbs_fold_concat — fold & -concatenation chains of constant terms to one literal.

Handles: "x" & "y"  /  "x" & Chr(65)  /  variable & "y" (when variable known).
Analog of PsFold-Strings (the & operator form).

The pass finds each maximal & chain and folds every maximal resolvable
sub-run within it (a run of two or more consecutive constant atoms) into a
single quoted string literal, leaving unresolvable atoms (e.g. calls to
unknown functions) — and any lone literal stranded next to one — untouched.

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


def _skip_ws_left(tokens: list, idx: int) -> int:
    idx -= 1
    while idx >= 0 and tokens[idx].kind == TokenKind.WS:
        idx -= 1
    return idx


def _skip_ws_right(tokens: list, idx: int) -> int:
    idx += 1
    while idx < len(tokens) and tokens[idx].kind == TokenKind.WS:
        idx += 1
    return idx


def _atom_end(tokens: list, idx: int) -> int | None:
    """idx must point at the first token of an atom. Return its last token
    index — handles bare literals, `(...)` groups, and `ident(...)` calls."""
    n = len(tokens)
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
    if t.kind == TokenKind.IDENT:
        j = _skip_ws_right(tokens, idx)
        if j < n and tokens[j].kind == TokenKind.OP and tokens[j].value == '(':
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


def _find_atom_left(tokens: list, idx: int) -> int | None:
    """Return leftmost token index of the atom/paren-group/call to the left of idx."""
    idx = _skip_ws_left(tokens, idx)
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
                        # ident(...) call/index — the name is part of the atom too
                        prev = _skip_ws_left(tokens, k)
                        if prev >= 0 and tokens[prev].kind == TokenKind.IDENT:
                            return prev
                        return k
            k -= 1
        return None
    if t.kind in (TokenKind.STRING, TokenKind.NUMBER, TokenKind.IDENT):
        return idx
    return None


def _find_atom_right(tokens: list, idx: int) -> int | None:
    """Return rightmost token index of the atom/paren-group/call to the right of idx."""
    start = _skip_ws_right(tokens, idx)
    if start >= len(tokens):
        return None
    return _atom_end(tokens, start)


def _one_pass(src: str, env: dict) -> tuple[str, int]:
    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []
    used: set[int] = set()

    # Find every '&'/'+' operator at top-level or inside parens, expand to
    # its enclosing maximal chain, and fold every resolvable sub-run inside it.
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind in (TokenKind.STRING, TokenKind.COMMENT):
            i += 1
            continue
        if tok.kind == TokenKind.OP and tok.value in ('&', '+') and i not in used:
            left, right = _expand_concat_chain(tokens, i)
            if (left is not None and right is not None
                    and not any(idx in used for idx in range(left, right + 1))):
                atoms = _split_chain_atoms(tokens, left, right)
                n_folded = _fold_atom_runs(tokens, atoms, env, edits, used)
                if n_folded:
                    # Already fully analyzed this span; anything left unfolded
                    # is either a lone literal or nested inside an unresolvable
                    # atom (its own & tokens get visited on a later pass).
                    i = right + 1
                    continue
        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


def _expand_concat_chain(tokens: list, amp_idx: int) -> tuple[int | None, int | None]:
    """Find the leftmost and rightmost token indices of the & chain containing
    tokens[amp_idx].  Returns (left_idx, right_idx) or (None, None) on failure."""
    left_atom  = _find_atom_left(tokens, amp_idx)
    right_atom = _find_atom_right(tokens, amp_idx)
    if left_atom is None or right_atom is None:
        return None, None

    chain_left  = left_atom
    chain_right = right_atom

    # Expand leftward: if the left-atom is preceded by '&' or '+', include that chain.
    cur = chain_left
    while True:
        la = _skip_ws_left(tokens, cur)
        if la < 0 or not (tokens[la].kind == TokenKind.OP and tokens[la].value in ('&', '+')):
            break
        new_left = _find_atom_left(tokens, la)
        if new_left is None:
            break
        chain_left = new_left
        cur = chain_left

    # Expand rightward
    cur = chain_right
    while True:
        ra = _skip_ws_right(tokens, cur)
        if ra >= len(tokens) or not (tokens[ra].kind == TokenKind.OP and tokens[ra].value in ('&', '+')):
            break
        new_right = _find_atom_right(tokens, ra)
        if new_right is None:
            break
        chain_right = new_right
        cur = chain_right

    return chain_left, chain_right


def _split_chain_atoms(tokens: list, chain_left: int, chain_right: int) -> list[tuple[int, int]]:
    """Split a maximal & chain [chain_left, chain_right] into its ordered
    atom spans [(start, end), ...]."""
    atoms: list[tuple[int, int]] = []
    pos = chain_left
    while True:
        end = _atom_end(tokens, pos)
        if end is None:
            break
        atoms.append((pos, end))
        if end >= chain_right:
            break
        op = _skip_ws_right(tokens, end)
        if op >= len(tokens) or op > chain_right:
            break
        if not (tokens[op].kind == TokenKind.OP and tokens[op].value in ('&', '+')):
            break
        pos = _skip_ws_right(tokens, op)
    return atoms


def _fold_atom_runs(tokens: list, atoms: list[tuple[int, int]], env: dict,
                     edits: list[tuple[int, int, str]], used: set[int]) -> int:
    """Resolve each atom individually, group maximal consecutive resolvable
    runs (length >= 2 atoms), and fold each run whose combined value is a
    string constant."""
    resolved = [resolve_const(tokens[a:b + 1], env) is not None for a, b in atoms]

    n_folded = 0
    idx = 0
    n = len(atoms)
    while idx < n:
        if not resolved[idx]:
            idx += 1
            continue
        run_start = idx
        while idx < n and resolved[idx]:
            idx += 1
        run_end = idx - 1  # inclusive atom index
        if run_end > run_start:
            start_tok = atoms[run_start][0]
            end_tok = atoms[run_end][1]
            val = resolve_const(tokens[start_tok:end_tok + 1], env)
            if val is not None and isinstance(val, str):
                edits.append((tokens[start_tok].start, tokens[end_tok].end, quote_vbs(val)))
                for t_idx in range(start_tok, end_tok + 1):
                    used.add(t_idx)
                n_folded += 1
    return n_folded


if __name__ == '__main__':
    run_tool(run, description='Fold & concatenation chains of constant terms to a single string literal')
