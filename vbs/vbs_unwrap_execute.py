"""vbs_unwrap_execute — inline Execute("<constant statement>") calls.

Obfuscators frequently wrap a single, otherwise-ordinary statement in
Execute/ExecuteGlobal purely to hide it from static string/method scanners
(e.g. `Execute "oRE.Pattern = Intoner"` instead of `oRE.Pattern = Intoner`).
When the argument resolves to a compile-time-constant string (via the shared
resolver — so it also fires after Chr()/concat obfuscation has been folded)
and that string parses as a single ordinary VBScript statement, this pass
replaces the whole Execute call with the inlined statement.

Never runs the payload. Complements vbs_annotate_execute.py (which never
modifies code, only comments); this one performs the actual inlining and is
deliberately more conservative about *when* it fires:

  - Eval is never unwrapped: it is an expression (and a peculiar one —
    Eval("a=1") is a *comparison* in VBScript, not an assignment), not a
    statement, so there is no statement form to inline it as.
  - ExecuteGlobal is only unwrapped at true module top level (block_depth
    == 0). Inside a procedure it assigns to *global* scope; inlining it
    there would silently turn a global write into a local one.
  - The resolved payload must look like a single VBScript statement: it
    either contains a top-level '=' (assignment), starts with a
    statement-leading keyword (Call, Set, Dim, If, ...), or is a bare/
    member-chain call (`Foo Arg`, `Foo.Bar(Arg)`). A bare expression like
    `Execute "1+1"` is a VBScript runtime error and is left untouched.
  - A colon-joined statement following the Execute on the same line is
    preserved exactly (the trailing colon is re-emitted), and no trailing
    comment is added in that case (a comment would swallow the rest of the
    line). Otherwise a short marker comment is appended so the change is
    visible in a diff.

Usage:
    python vbs_unwrap_execute.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits
from vbsdeoblib.resolver import resolve_const
from vbsdeoblib.statements import split_statements

_EXEC_KW = frozenset(['EXECUTE', 'EXECUTEGLOBAL'])

_STMT_LEAD_KW = frozenset([
    'CALL', 'SET', 'DIM', 'IF', 'FOR', 'DO', 'WHILE', 'ON', 'WITH',
    'EXIT', 'REDIM', 'RANDOMIZE', 'EXECUTE', 'EXECUTEGLOBAL', 'ERASE', 'CONST',
])


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(50):
        src, n = _one_pass(src)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total, 'unwrapped': changed_total}


def _one_pass(src: str) -> tuple[str, int]:
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    if not stmts:
        return src, 0

    edits: list[tuple[int, int, str]] = []
    depth = 0

    for stmt in stmts:
        ctoks = stmt.code_tokens()
        if not ctoks:
            continue
        kw = ctoks[0].upper if ctoks[0].kind == TokenKind.IDENT else ''

        if kw in ('NEXT', 'LOOP', 'WEND'):
            depth = max(0, depth - 1)
            continue
        if kw == 'END':
            if len(ctoks) > 1 and ctoks[1].kind == TokenKind.IDENT:
                depth = max(0, depth - 1)
            continue

        is_block_open = False
        if kw == 'IF':
            last = ctoks[-1]
            is_block_open = last.kind == TokenKind.IDENT and last.upper == 'THEN'
        elif kw in ('FOR', 'DO', 'WHILE', 'SELECT', 'WITH', 'FUNCTION', 'SUB', 'CLASS', 'PROPERTY'):
            is_block_open = True
        if is_block_open:
            depth += 1
            continue

        if kw not in _EXEC_KW:
            continue
        if kw == 'EXECUTEGLOBAL' and depth != 0:
            continue  # would silently change assignment scope — skip

        arg_toks = ctoks[1:]
        ends_with_colon = bool(arg_toks) and arg_toks[-1].kind == TokenKind.COLON
        if ends_with_colon:
            arg_toks = arg_toks[:-1]
        if not arg_toks:
            continue

        val = resolve_const(arg_toks)
        if not isinstance(val, str) or not _looks_like_statement(val):
            continue

        last_tok = stmt.tokens[-1] if stmt.tokens else None
        if ends_with_colon:
            replacement = val + ':'
        elif last_tok is not None and last_tok.kind == TokenKind.NEWLINE:
            replacement = f"{val}  ' <deobfuscator> unwrapped {kw.capitalize()}" + last_tok.value
        else:
            replacement = f"{val}  ' <deobfuscator> unwrapped {kw.capitalize()}"

        edits.append((stmt.start, stmt.end, replacement))

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


def _looks_like_statement(payload: str) -> bool:
    """Heuristic: does *payload* look like a single ordinary VBScript
    statement (assignment, keyword-led statement, or bare/member-chain
    call) rather than a bare expression?"""
    toks = [t for t in tokenize(payload)
            if t.kind not in (TokenKind.WS, TokenKind.COMMENT, TokenKind.NEWLINE, TokenKind.LINECONT)]
    if not toks:
        return False

    depth = 0
    for t in toks:
        if t.kind == TokenKind.OP and t.value == '(':
            depth += 1
        elif t.kind == TokenKind.OP and t.value == ')':
            depth = max(0, depth - 1)
        elif t.kind == TokenKind.OP and t.value == '=' and depth == 0:
            return True  # assignment statement

    first = toks[0]
    if first.kind == TokenKind.IDENT and first.upper in _STMT_LEAD_KW:
        return True
    if first.kind != TokenKind.IDENT:
        return False

    # Walk past a leading identifier / member-access chain (Foo.Bar.Baz).
    i = 1
    while (i + 1 < len(toks) and toks[i].kind == TokenKind.OP and toks[i].value == '.'
           and toks[i + 1].kind == TokenKind.IDENT):
        i += 2
    if i >= len(toks):
        return True  # bare zero-arg call: "Foo" / "Foo.Bar"

    nxt = toks[i]
    if nxt.kind == TokenKind.OP and nxt.value == '(':
        return True  # "Foo(...)" / "Foo.Bar(...)"
    if nxt.kind == TokenKind.OP and nxt.value in ('+', '-', '*', '/', '\\', '^', '&', '<', '>', ',', '.'):
        return False  # looks like a bare expression, not a call
    return True  # "Foo Arg1, Arg2" bare call


if __name__ == '__main__':
    run_tool(run, description='Inline Execute("<constant statement>") calls')
