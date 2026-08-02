"""vbs_expand_colons — split colon-packed lines into one statement per line.

VBScript allows multiple statements on one line separated by ':'.
Obfuscators (and minifiers) use this to pack a whole script onto a few lines.
This pass splits them out, making all other passes easier.

Analog of PsExpand-Semicolons.

Usage:
    python vbs_expand_colons.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool


def run(src: str, **_) -> tuple[str, dict]:
    lines_in  = src.count('\n') + (1 if src and src[-1] != '\n' else 0)
    new_src   = _expand(src)
    lines_out = new_src.count('\n') + (1 if new_src and new_src[-1] != '\n' else 0)
    changed   = 1 if new_src != src else 0
    return new_src, {'changed': changed, 'lines_in': lines_in, 'lines_out': lines_out}


def _expand(src: str) -> str:
    """Walk the token stream, emitting a newline wherever we see COLON outside
    strings/comments, and preserving line-continuation joins."""
    tokens = tokenize(src)
    out_parts: list[str] = []
    continuation = False

    for tok in tokens:
        if tok.kind == TokenKind.LINECONT:
            out_parts.append(tok.value)
            continuation = True
            continue
        if tok.kind == TokenKind.NEWLINE:
            if continuation:
                continuation = False
                out_parts.append(tok.value)
            else:
                out_parts.append(tok.value)
            continue
        if tok.kind == TokenKind.COLON:
            # Emit a newline in place of the colon.
            out_parts.append('\n')
            continuation = False
            continue
        out_parts.append(tok.value)
        continuation = False

    return ''.join(out_parts)


if __name__ == '__main__':
    run_tool(run, description='Split colon-separated statements onto individual lines')
