"""vbs_annotate_execute — annotate Execute/ExecuteGlobal/Eval with their payload as a comment.

Non-destructive: the original statement is left intact. When the argument is a
string literal or a variable already resolved to a literal, the decoded text is
appended as a block comment immediately after the call.

Never executes the payload. Analog of PsAnnotate-Iex.

Usage:
    python vbs_annotate_execute.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.resolver import resolve_const, _parse_string


_EXEC_NAMES = frozenset(['EXECUTE', 'EXECUTEGLOBAL', 'EVAL'])


def run(src: str, **_) -> tuple[str, dict]:
    tokens = tokenize(src)
    inserts: list[tuple[int, str]] = []   # (insert_after_offset, comment_text)
    annotated = 0

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind == TokenKind.IDENT and tok.upper in _EXEC_NAMES:
            # Collect the rest of the logical line as the argument expression.
            j = i + 1
            # Skip WS
            while j < len(tokens) and tokens[j].kind == TokenKind.WS:
                j += 1
            # Collect tokens until end of logical line (NEWLINE not preceded by LINECONT).
            arg_toks: list = []
            continuation = False
            k = j
            while k < len(tokens):
                t = tokens[k]
                if t.kind == TokenKind.LINECONT:
                    continuation = True
                    k += 1
                    continue
                if t.kind == TokenKind.NEWLINE:
                    if continuation:
                        continuation = False
                        k += 1
                        continue
                    break
                if t.kind == TokenKind.COMMENT:
                    k += 1
                    continue
                arg_toks.append(t)
                k += 1

            # Try to resolve the argument to a string constant.
            val = resolve_const(arg_toks)
            if val is not None and isinstance(val, str):
                payload = str(val)
                # Find the end-of-line position to insert after.
                eol_offset = tokens[k-1].end if k > j else tok.end
                comment = _make_comment(tok.value, payload)
                inserts.append((eol_offset, comment))
                annotated += 1

        i += 1

    if not inserts:
        return src, {'changed': 0, 'annotated': 0}

    # Insert comments at the right places (right-to-left).
    result = src
    for offset, comment in sorted(inserts, reverse=True):
        result = result[:offset] + comment + result[offset:]

    return result, {'changed': annotated, 'annotated': annotated}


def _make_comment(fn_name: str, payload: str) -> str:
    lines = payload.splitlines() or ['']
    block = '\n'.join(f"' > {line}" for line in lines)
    return f"\n' <<<{fn_name.upper()} PAYLOAD BEGIN>>>\n{block}\n' <<<{fn_name.upper()} PAYLOAD END>>>"


if __name__ == '__main__':
    run_tool(run, description='Annotate Execute/ExecuteGlobal/Eval with their payload as comments')
