"""vbs_inline_sink_calls — materialize calls to a side-effecting accumulator
function/sub as direct self-append assignments.

Targets the "indirect accumulator" idiom: a payload string is built not by
`buf = buf & "chunk"` directly, but by routing every chunk through a
throwaway Function/Sub whose only effect is that same self-append, applied to
a *different* global than its own name:

    Function Sink(chunk)
        buf = buf & chunk
    End Function
    Call Sink("first ")
    Call Sink("second")

This is the call-indirected sibling of the direct self-append pattern
vbs_propagate_constants already seeds as "" and folds, and of the
single-expression wrapper pattern vbs_inline_functions already inlines —
neither fires here because the mutated variable (`buf`) is neither the
function's own name (so vbs_inline_functions' `FunctionName = expr` check
never matches) nor written directly at any call site (so
vbs_propagate_constants never sees a literal `buf = buf & "chunk"` to seed).

This tool closes that gap generically: it detects the accumulator
Function/Sub by shape alone (self-append of one of its own parameters to a
different global, as the sole body statement), rewrites every call site —
`Call Sink(arg)`, bare `Sink(arg)`, or bare `Sink arg` — to the equivalent
`buf = buf & (arg)` statement, and removes the now-dead definition.

Follow with vbs_propagate_constants (seeds `buf` as "" and folds the chain)
then vbs_fold_concat (collapses the final concatenation to one literal) then
vbs_remove_deadcode (drops the superseded intermediate stores).

Usage:
    python vbs_inline_sink_calls.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from dataclasses import dataclass
from vbsdeoblib import tokenize, TokenKind, split_statements, run_tool
from vbsdeoblib.io import apply_edits


@dataclass
class _SinkDef:
    name: str
    params: list[str]      # lowercased parameter names
    accum_var: str         # the global variable being self-appended
    op: str                # '&' or '+'
    param: str              # lowercased name of the parameter being appended
    def_start: int
    def_end: int
    prepend: bool = False  # True for `G = P & G` (chunk joins on the left)


_DEF_PATTERNS = [
    (kw, re.compile(
        r'(?im)^[ \t]*' + kw + r'\s+(\w+)\s*\(([^)]*)\)\s*\r?\n'
        r'((?:[ \t]*[^\r\n]*\r?\n)*?)'
        r'[ \t]*End\s+' + kw,
    ))
    for kw in ('Function', 'Sub')
]

# G = G <op> P  — chunk appended on the right
_BODY_APPEND_RE  = re.compile(r'(?i)^(\w+)\s*=\s*\1\s*(&|\+)\s*(\w+)\s*$')
# G = P <op> G  — same idiom mirrored, chunks accumulate in reverse order
_BODY_PREPEND_RE = re.compile(r'(?i)^(\w+)\s*=\s*(\w+)\s*(&|\+)\s*\1\s*$')


def _clean_param(p: str) -> str:
    p = p.strip()
    p = re.sub(r'(?i)^(ByVal|ByRef)\s+', '', p).strip()
    return p.lower()


def _find_sink_defs(src: str) -> list[_SinkDef]:
    defs: list[_SinkDef] = []
    for kw, pattern in _DEF_PATTERNS:
        for m in pattern.finditer(src):
            fn_name = m.group(1)
            params_raw = m.group(2)
            body = m.group(3)
            params = [_clean_param(p) for p in params_raw.split(',') if p.strip()]
            if not params:
                continue

            body_lines = [l.strip() for l in body.splitlines()
                          if l.strip() and not l.strip().startswith("'")]
            if len(body_lines) != 1:
                continue

            bm = _BODY_APPEND_RE.match(body_lines[0])
            prepend = False
            if bm:
                accum_var, op, rhs_param = bm.group(1), bm.group(2), bm.group(3)
            else:
                bm = _BODY_PREPEND_RE.match(body_lines[0])
                if not bm:
                    continue
                accum_var, rhs_param, op = bm.group(1), bm.group(2), bm.group(3)
                prepend = True
            if rhs_param.lower() not in params:
                continue
            if accum_var.lower() == fn_name.lower():
                continue  # that's vbs_inline_functions' wrapper-return pattern, not this one
            if accum_var.lower() in params:
                continue  # appending a param to itself is not a global accumulator

            defs.append(_SinkDef(
                name=fn_name, params=params, accum_var=accum_var, op=op,
                param=rhs_param.lower(), def_start=m.start(), def_end=m.end(),
                prepend=prepend,
            ))
    return defs


def _split_top_level_commas(toks: list) -> list[list]:
    """Split a token list on commas not nested inside parentheses."""
    parts: list[list] = []
    current: list = []
    depth = 0
    for t in toks:
        if t.kind == TokenKind.OP and t.value == '(':
            depth += 1
        elif t.kind == TokenKind.OP and t.value == ')':
            depth -= 1
        if t.kind == TokenKind.OP and t.value == ',' and depth == 0:
            parts.append(current)
            current = []
            continue
        current.append(t)
    if current or parts:
        parts.append(current)
    return parts


def _is_side_effect_free(toks: list) -> bool:
    """True when a discarded argument expression provably can't do anything
    observable. A '(' anywhere means it could be a call, so decline — the
    same call-shape guard vbs_remove_deadcode already applies to ReDim
    bounds before deleting them."""
    return not any(t.kind == TokenKind.OP and t.value == '(' for t in toks)


def _accum_shadowed_in_proc(src: str, accum: str) -> bool:
    """True when the accumulator name is declared local to some procedure or
    class body. The sink writes a *global*; inlining a call that sits inside
    such a body would silently retarget that write to the local instead, so
    the whole sink is declined rather than converted wrongly."""
    up = accum.upper()
    stmts = split_statements(tokenize(src))
    proc_depth = 0
    for st in stmts:
        c = st.code_tokens()
        if not c or c[0].kind != TokenKind.IDENT:
            continue
        kw = c[0].upper
        base = 0
        if kw in ('PRIVATE', 'PUBLIC') and len(c) > 1 and c[1].kind == TokenKind.IDENT \
                and c[1].upper in ('SUB', 'FUNCTION', 'PROPERTY'):
            base, kw = 1, c[1].upper
        if kw in ('FUNCTION', 'SUB', 'PROPERTY', 'CLASS'):
            proc_depth += 1
            continue
        if kw == 'END' and len(c) > 1 and c[1].kind == TokenKind.IDENT \
                and c[1].upper in ('FUNCTION', 'SUB', 'PROPERTY', 'CLASS'):
            proc_depth = max(0, proc_depth - 1)
            continue
        if proc_depth > 0 and kw in ('DIM', 'REDIM', 'PRIVATE', 'PUBLIC'):
            if any(t.kind == TokenKind.IDENT and t.upper == up for t in c[base + 1:]):
                return True
    return False


def _convert_calls(src: str, sink: _SinkDef) -> tuple[str, int]:
    """Rewrite every call-statement to sink.name into `accum = accum OP (arg)`,
    where `arg` is the argument bound to the parameter the body actually
    appends. Skips statements inside the definition itself, and declines
    (leaves the call untouched) whenever the call's shape doesn't match the
    definition's."""
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    edits: list[tuple[int, int, str]] = []
    def_range = (sink.def_start, sink.def_end)
    param_index = sink.params.index(sink.param)

    for st in stmts:
        if not st.tokens:
            continue
        if def_range[0] <= st.tokens[0].start < def_range[1]:
            continue  # inside the definition block

        ctoks = st.code_tokens()
        if not ctoks:
            continue

        idx0 = 0
        if ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'CALL':
            idx0 = 1
        if idx0 >= len(ctoks):
            continue
        if not (ctoks[idx0].kind == TokenKind.IDENT
                and ctoks[idx0].value.lower() == sink.name.lower()):
            continue

        rest = ctoks[idx0 + 1:]
        if not rest:
            continue

        if rest[0].kind == TokenKind.OP and rest[0].value == '(':
            if not (rest[-1].kind == TokenKind.OP and rest[-1].value == ')'):
                continue  # trailing tokens after the closing paren — not a plain call
            arg_toks = rest[1:-1]
        else:
            arg_toks = rest  # bare no-paren call: `Sink "chunk"`

        if not arg_toks:
            continue

        # Arity must match the definition, else this isn't the call we think
        # it is — emitting `acc & ("a", "b")` would be invalid VBScript.
        arg_parts = [p for p in _split_top_level_commas(arg_toks)]
        if len(arg_parts) != len(sink.params):
            continue
        chosen = arg_parts[param_index]
        if not chosen:
            continue
        # Every *other* argument is about to be discarded (the body never
        # reads it), but VBScript would still evaluate it at the call site —
        # so only drop provably inert ones.
        if any(_ is not chosen and not _is_side_effect_free(_) for _ in arg_parts):
            continue

        arg_text = src[chosen[0].start: chosen[-1].end]
        code_start = ctoks[0].start
        code_end = ctoks[-1].end
        if sink.prepend:
            replacement = f'{sink.accum_var} = ({arg_text}) {sink.op} {sink.accum_var}'
        else:
            replacement = f'{sink.accum_var} = {sink.accum_var} {sink.op} ({arg_text})'
        edits.append((code_start, code_end, replacement))

    if not edits:
        return src, 0
    return apply_edits(src, edits), len(edits)


def _remove_def(src: str, sink: _SinkDef) -> str:
    region = src[sink.def_start: sink.def_end]
    blank = '\n' * region.count('\n')
    return src[:sink.def_start] + blank + src[sink.def_end:]


def _one_pass(src: str) -> tuple[str, int, int]:
    defs = _find_sink_defs(src)
    if not defs:
        return src, 0, 0

    total_calls = 0
    inlined_defs = 0

    for sink in defs:
        if _accum_shadowed_in_proc(src, sink.accum_var):
            continue
        new_src, n = _convert_calls(src, sink)
        if n == 0:
            continue
        # Re-locate the definition in the post-edit source before blanking it.
        for nd in _find_sink_defs(new_src):
            if (nd.name.lower() == sink.name.lower()
                    and nd.accum_var.lower() == sink.accum_var.lower()):
                new_src = _remove_def(new_src, nd)
                break
        src = new_src
        total_calls += n
        inlined_defs += 1

    return src, total_calls, inlined_defs


def run(src: str, **_) -> tuple[str, dict]:
    calls_total = 0
    defs_total = 0
    for _ in range(50):
        src, n_calls, n_defs = _one_pass(src)
        calls_total += n_calls
        defs_total += n_defs
        if n_calls == 0:
            break
    return src, {'calls_converted': calls_total, 'sink_defs_inlined': defs_total}


if __name__ == '__main__':
    run_tool(run, description='Materialize calls to a side-effecting self-append '
                               'accumulator Function/Sub as direct assignments')
