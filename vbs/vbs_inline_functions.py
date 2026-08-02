"""vbs_inline_functions — inline user-defined single-expression wrapper functions.

Targets: Function definitions whose body is a single assignment of the form
    FunctionName = <expression>
where <expression> is a constant or a pure-builtin call over the parameters.

Example:
    Function ttaffRy(s)
        ttaffRy = Replace(s, "@", "")
    End Function
    ...
    ttaffRy("WScr@ipt.Shell")   →   Replace("WScr@ipt.Shell", "@", "")

After inlining, the function definition itself is removed (turned into a blank
line) so downstream passes (vbs_fold_builtin_calls) can fold the materialised call.

No direct PS analog — this is a VBScript-specific idiom.

Usage:
    python vbs_inline_functions.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    inlined_total = 0
    for _ in range(50):
        src, n, inlined = _one_pass(src)
        changed_total += n
        inlined_total += inlined
        if n == 0:
            break
    return src, {'changed': changed_total, 'functions_inlined': inlined_total}


# ---------------------------------------------------------------------------
# Parse a simple Function block into a descriptor.
# ---------------------------------------------------------------------------

from dataclasses import dataclass

@dataclass
class _FnDef:
    name: str
    params: list[str]    # lowercased parameter names
    body_expr: str       # the RHS expression text (referring to params)
    def_start: int       # byte offset of 'Function'
    def_end:   int       # byte offset after 'End Function\n'


def _find_functions(src: str, tokens: list) -> list[_FnDef]:
    """Extract simple single-assignment Function definitions."""
    fns: list[_FnDef] = []

    # We work on the raw source with a regex scan for Function...End Function blocks,
    # then validate them as single-assignment.
    pattern = re.compile(
        r'(?im)^[ \t]*Function\s+(\w+)\s*\(([^)]*)\)\s*\r?\n'
        r'((?:[ \t]*[^\r\n]+\r?\n)*?)'        # body lines
        r'[ \t]*End\s+Function',
    )
    for m in pattern.finditer(src):
        fn_name   = m.group(1)
        params_raw = m.group(2)
        body       = m.group(3)

        params = [p.strip().lower() for p in params_raw.split(',') if p.strip()]

        # Body must be exactly one non-blank, non-comment line that looks like:
        #   FunctionName = <expr>
        body_lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("'")]
        if len(body_lines) != 1:
            continue

        line = body_lines[0]
        # Must start with FunctionName =
        assign_re = re.compile(r'(?i)^' + re.escape(fn_name) + r'\s*=\s*(.+)$')
        am = assign_re.match(line)
        if not am:
            continue

        body_expr = am.group(1).strip()
        fns.append(_FnDef(
            name=fn_name,
            params=params,
            body_expr=body_expr,
            def_start=m.start(),
            def_end=m.end(),
        ))
    return fns


def _inline_calls(src: str, fn: _FnDef) -> tuple[str, int]:
    """Replace all calls to fn.name(...) with the inlined body expression.
    Skips occurrences inside the Function definition itself."""
    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []

    # Build the byte range of the function definition so we skip it.
    def_range = (fn.def_start, fn.def_end)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Skip tokens inside the definition block itself.
        if def_range[0] <= tok.start < def_range[1]:
            i += 1
            continue
        if tok.kind == TokenKind.IDENT and tok.value.lower() == fn.name.lower():
            j = i + 1
            while j < len(tokens) and tokens[j].kind == TokenKind.WS:
                j += 1
            if j < len(tokens) and tokens[j].kind == TokenKind.OP and tokens[j].value == '(':
                # Collect the argument list up to matching ')'
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
                if k >= len(tokens):
                    i += 1
                    continue

                # Parse argument list (split by top-level commas)
                arg_tokens_list = _split_args(tokens[j+1:k])
                if len(arg_tokens_list) != len(fn.params):
                    i += 1
                    continue

                # Build substitution: replace param names in body_expr with actual arg text
                inlined = fn.body_expr
                for param, arg_toks in zip(fn.params, arg_tokens_list):
                    arg_text = src[arg_toks[0].start: arg_toks[-1].end] if arg_toks else ''
                    # Replace whole-word occurrences of param (case-insensitive)
                    inlined = re.sub(
                        r'(?i)\b' + re.escape(param) + r'\b',
                        arg_text,
                        inlined,
                    )

                edits.append((tok.start, tokens[k].end, f'({inlined})'))
                i = k + 1
                continue
        i += 1

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


def _split_args(tokens: list) -> list[list]:
    """Split a token list on top-level commas; return list of per-arg token lists."""
    args: list[list] = []
    current: list = []
    depth = 0
    for t in tokens:
        if t.kind == TokenKind.OP:
            if t.value == '(':
                depth += 1
            elif t.value == ')':
                depth -= 1
            elif t.value == ',' and depth == 0:
                args.append(current)
                current = []
                continue
        current.append(t)
    if current or args:
        args.append(current)
    # strip leading/trailing WS from each arg
    result = []
    for a in args:
        stripped = a[:]
        while stripped and stripped[0].kind == TokenKind.WS:
            stripped.pop(0)
        while stripped and stripped[-1].kind == TokenKind.WS:
            stripped.pop()
        result.append(stripped)
    return result


def _remove_fn_def(src: str, fn: _FnDef) -> str:
    """Replace the function definition span with blank lines (preserves offsets)."""
    region = src[fn.def_start: fn.def_end]
    # Replace with the same number of newlines to keep line numbers stable-ish.
    blank = '\n' * region.count('\n')
    return src[:fn.def_start] + blank + src[fn.def_end:]


def _one_pass(src: str) -> tuple[str, int, int]:
    tokens = tokenize(src)
    fns = _find_functions(src, tokens)
    if not fns:
        return src, 0, 0

    total_call_edits = 0
    inlined_fns = 0
    edits_def: list[tuple[int, int, str]] = []

    for fn in fns:
        new_src, n = _inline_calls(src, fn)
        if n > 0:
            # We inlined at least one call — now remove the definition.
            # The def offsets are in *original* src; recalculate after call edits.
            # Re-find the definition in the updated src.
            new_fns = _find_functions(new_src, tokenize(new_src))
            for nf in new_fns:
                if nf.name.lower() == fn.name.lower():
                    new_src = _remove_fn_def(new_src, nf)
                    break
            src = new_src
            total_call_edits += n
            inlined_fns += 1

    return src, total_call_edits, inlined_fns


if __name__ == '__main__':
    run_tool(run, description='Inline single-expression wrapper Function definitions at all call sites')
