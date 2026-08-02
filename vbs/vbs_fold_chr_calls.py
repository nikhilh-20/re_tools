"""vbs_fold_chr_calls — fold Chr(<const-int>) → single-character string literal.

Analog of PsFold-CharConcat (the Chr() half).

Works by tokenizing, finding every call-site of Chr() where the argument is a
constant integer, and replacing the entire Chr(N) span with the quoted char.
Repeats until stable so nested or chained forms collapse in one run.

Usage:
    python vbs_fold_chr_calls.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs
from vbsdeoblib.resolver import resolve_const, _parse_number


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(200):
        src, n = _one_pass(src)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


def _one_pass(src: str) -> tuple[str, int]:
    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Look for IDENT == 'CHR' followed immediately (skipping WS) by '('
        if tok.kind == TokenKind.IDENT and tok.upper == 'CHR':
            j = i + 1
            # skip WS
            while j < len(tokens) and tokens[j].kind == TokenKind.WS:
                j += 1
            if j < len(tokens) and tokens[j].kind == TokenKind.OP and tokens[j].value == '(':
                # collect tokens up to matching ')'
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
                if k < len(tokens):
                    # tokens[j..k] inclusive is Chr(...)
                    arg_tokens = tokens[j+1:k]  # between ( and )
                    val = resolve_const(arg_tokens)
                    if val is not None:
                        try:
                            ch = chr(int(val))
                            replacement = quote_vbs(ch)
                            edits.append((tok.start, tokens[k].end, replacement))
                            i = k + 1
                            continue
                        except (ValueError, OverflowError):
                            pass
        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


if __name__ == '__main__':
    run_tool(run, description='Fold Chr(N) calls to string literals')
