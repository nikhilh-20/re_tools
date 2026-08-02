"""vbs_remove_deadcode — liveness-based dead-store removal and false-condition block elimination.

Default mode:
  - Liveness-based dead-store removal: assignments to variables never read are
    deleted. Iterates to fixpoint — removing one dead store can expose more.
  - Dim declarations: whole line deleted when all declared names are dead;
    trimmed to keep only live names when partially dead.
  - Statically-false If/Do While blocks removed.

--preserve-strings:
  Keep string/number literal RHS assignments even when the LHS is dead
  (safety guard for files not yet run through vbs_propagate_constants).

--aggressive:
  Also removes unreferenced Function/Sub definitions whose name is never called.

Analog of PsRemove-DeadCode.

Usage:
    python vbs_remove_deadcode.py --input in.vbs --output out.vbs [--aggressive] [--preserve-strings]
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import bisect
import re
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits
from vbsdeoblib.resolver import resolve_const


def run(src: str, aggressive: bool = False, preserve_strings: bool = False, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(50):
        src, n = _one_pass(src, aggressive, preserve_strings)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


# ---------------------------------------------------------------------------
# Top-level pass dispatcher
# ---------------------------------------------------------------------------

def _one_pass(src: str, aggressive: bool, preserve_strings: bool) -> tuple[str, int]:
    # Sub-pass A: statically-false If blocks (no Else)
    edits = _false_if_edits(src)

    # Sub-pass B: statically-false Do While blocks
    if not edits:
        edits = _false_while_edits(src)

    # Sub-pass C: liveness-based dead-store removal (default mode)
    if not edits:
        edits = _dead_store_edits(src, preserve_strings)

    # Sub-pass D: unreferenced Function/Sub removal (--aggressive only)
    if not edits and aggressive:
        edits = _unused_func_edits(src)

    if not edits:
        return src, 0
    unique_edits = list({(s, e, r) for s, e, r in edits})
    return apply_edits(src, unique_edits), len(unique_edits)


# ---------------------------------------------------------------------------
# Sub-pass A/B: statically-false structural blocks
# ---------------------------------------------------------------------------

def _falsy(v) -> bool:
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        return v.lower() in ('', 'false', '0')
    return not bool(v)


def _false_if_edits(src: str) -> list[tuple[int, int, str]]:
    edits: list[tuple[int, int, str]] = []
    false_if = re.compile(
        r'(?im)^[ \t]*If\s+(.+?)\s+Then\s*\r?\n'
        r'(?:(?![ \t]*(?:ElseIf|Else\s+If|Else\b|End\s+If))[^\r\n]*\r?\n)*'
        r'[ \t]*End\s+If\b[^\r\n]*',
    )
    for m in false_if.finditer(src):
        cond_txt = m.group(1).strip()
        ctoks = tokenize(cond_txt)
        val = resolve_const(ctoks)
        if val is not None and _falsy(val):
            region = src[m.start(): m.end()]
            blank = '\n' * region.count('\n')
            edits.append((m.start(), m.end(), blank))
    return edits


def _false_while_edits(src: str) -> list[tuple[int, int, str]]:
    edits: list[tuple[int, int, str]] = []
    false_while = re.compile(
        r'(?im)^[ \t]*Do\s+While\s+(.+?)\s*\r?\n'
        r'(?:(?![ \t]*Loop)[^\r\n]*\r?\n)*'
        r'[ \t]*Loop\b[^\r\n]*',
    )
    for m in false_while.finditer(src):
        cond_txt = m.group(1).strip()
        ctoks = tokenize(cond_txt)
        val = resolve_const(ctoks)
        if val is not None and _falsy(val):
            region = src[m.start(): m.end()]
            blank = '\n' * region.count('\n')
            edits.append((m.start(), m.end(), blank))
    return edits


# ---------------------------------------------------------------------------
# Sub-pass C: liveness-based dead-store removal
# ---------------------------------------------------------------------------

# Matches a simple top-level assignment: [optional Set/Let] <name> = <not ==/<>/<=/>=>
# The ^[ \t]* handles indented lines (e.g. inside function bodies).
# Does NOT match: If x = 0 Then  (keyword precedes the identifier)
_lhs_re = re.compile(
    r'^[ \t]*(?:(?:Set|Let)\s+)?([A-Za-z_]\w*)[ \t]*=[^=<>]'
)

# Dim declaration line
_dim_re = re.compile(r'(?i)^[ \t]*Dim\s+(.*)')

# RHS has observable side effects: object creation or method call
_side_effect_re = re.compile(r'(?i)(CreateObject|GetObject|\.\w+\s*\()')


def _line_of(offset: int, line_offsets: list[int]) -> int:
    """0-based line index for a byte offset (binary search over line_offsets)."""
    return bisect.bisect_right(line_offsets, offset) - 1


def _is_pure_literal(rhs: str) -> bool:
    """True if rhs (the text after '=') is a single string or number literal."""
    toks = [t for t in tokenize(rhs)
            if t.kind not in (TokenKind.WS, TokenKind.NEWLINE, TokenKind.COMMENT)]
    return len(toks) == 1 and toks[0].kind in (TokenKind.STRING, TokenKind.NUMBER)


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    for line in lines[:-1]:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _dead_store_edits(src: str, preserve_strings: bool) -> list[tuple[int, int, str]]:
    """Liveness-based: return edits for dead assignment lines + dead/partial Dim lines."""
    edits: list[tuple[int, int, str]] = []
    lines = src.splitlines(keepends=True)
    if not lines:
        return edits

    line_offsets = _build_line_offsets(lines)

    # --- Line scan: classify each line as assignment, Dim, or other ---
    assigns: dict[str, list[int]] = {}  # name_upper -> [line_indices]
    dim_info: dict[int, list[str]] = {}  # line_index -> [declared name list]
    lhs_byte_offsets: set[int] = set()  # byte offsets of LHS ident tokens

    for li, line in enumerate(lines):
        m = _lhs_re.match(line)
        if m:
            name = m.group(1).upper()
            if name not in _KW:
                assigns.setdefault(name, []).append(li)
                lhs_byte_offsets.add(line_offsets[li] + m.start(1))
            continue

        m = _dim_re.match(line)
        if m:
            raw = re.sub(r"'.*", '', m.group(1)).strip()  # strip trailing comment
            names: list[str] = []
            for part in raw.split(','):
                n = part.strip()
                if re.match(r'^[A-Za-z_]\w*$', n):
                    names.append(n)
            if names:
                dim_info[li] = names

    # --- Find byte offsets of declared names in Dim lines ---
    # Use the tokenizer so we match exactly what the lexer sees.
    dim_line_set: frozenset[int] = frozenset(dim_info.keys())
    src_toks = tokenize(src)

    dim_token_starts: set[int] = set()
    for tok in src_toks:
        if tok.kind != TokenKind.IDENT or tok.upper == 'DIM':
            continue
        li = _line_of(tok.start, line_offsets)
        if li in dim_line_set:
            dim_token_starts.add(tok.start)

    # --- Collect reads ---
    # Any IDENT token not at an LHS or Dim-declaration position is a read.
    excluded_starts = lhs_byte_offsets | dim_token_starts
    reads: set[str] = set()
    for tok in src_toks:
        if (tok.kind == TokenKind.IDENT
                and tok.upper not in _KW
                and tok.start not in excluded_starts):
            reads.add(tok.upper)

    # --- Compute dead names ---
    all_dim_names: set[str] = {
        n.upper() for names in dim_info.values() for n in names
    }
    dead: set[str] = {
        n for n in (set(assigns.keys()) | all_dim_names)
        if n not in reads
    }

    if not dead:
        return edits

    # --- Emit edits for dead assignment lines ---
    for name, line_indices in assigns.items():
        if name not in dead:
            continue
        for li in line_indices:
            line = lines[li]
            m = _lhs_re.match(line)
            if not m:
                continue
            # Extract RHS text (everything after the '=')
            eq_pos = line.index('=', m.end(1))
            rhs = line[eq_pos + 1:]
            # Purity gate: keep lines whose RHS has observable side effects
            if _side_effect_re.search(rhs):
                continue
            # Optional preserve-strings gate (mirrors PS PreserveStringLiterals)
            if preserve_strings and _is_pure_literal(rhs):
                continue
            off = line_offsets[li]
            edits.append((off, off + len(line), ''))

    # --- Emit edits for dead/partial Dim lines ---
    for li, names in dim_info.items():
        live_names = [n for n in names if n.upper() not in dead]
        if len(live_names) == len(names):
            continue  # all live, no change
        line = lines[li]
        off = line_offsets[li]
        if not live_names:
            edits.append((off, off + len(line), ''))
        else:
            indent = re.match(r'^[ \t]*', line).group(0)
            eol = '\r\n' if line.endswith('\r\n') else '\n'
            edits.append((off, off + len(line), f'{indent}Dim {", ".join(live_names)}{eol}'))

    return edits


# ---------------------------------------------------------------------------
# Sub-pass D: unreferenced Function/Sub removal (--aggressive)
# ---------------------------------------------------------------------------

_fn_sub_pat = re.compile(
    r'(?im)^[ \t]*(Function|Sub)\s+(\w+)\s*(?:\([^)]*\))?\s*\r?\n'
    r'(?:(?![ \t]*End\s+(?:Function|Sub))[^\r\n]*\r?\n)*'
    r'[ \t]*End\s+(?:Function|Sub)\b[^\r\n]*'
)


def _unused_func_edits(src: str) -> list[tuple[int, int, str]]:
    """Return edits that remove Function/Sub definitions whose name is never called."""
    edits: list[tuple[int, int, str]] = []

    defs = [(m.group(2).upper(), m.start(), m.end()) for m in _fn_sub_pat.finditer(src)]
    if not defs:
        return edits

    # Build set of byte ranges covered by all definitions
    def_ranges: list[tuple[int, int]] = [(start, end) for _, start, end in defs]

    # Build LHS exclusions (assignment LHS tokens)
    lines = src.splitlines(keepends=True)
    line_offsets = _build_line_offsets(lines)
    lhs_byte_offsets: set[int] = set()
    for li, line in enumerate(lines):
        m = _lhs_re.match(line)
        if m and m.group(1).upper() not in _KW:
            lhs_byte_offsets.add(line_offsets[li] + m.start(1))

    def in_any_def(offset: int) -> bool:
        for start, end in def_ranges:
            if start <= offset < end:
                return True
        return False

    # Collect call-site reads: IDENT tokens outside all definition blocks and LHS positions
    call_reads: set[str] = set()
    str_content: str = src.lower()  # for dynamic-dispatch string check
    for tok in tokenize(src):
        if tok.kind == TokenKind.IDENT and tok.upper not in _KW:
            if tok.start not in lhs_byte_offsets and not in_any_def(tok.start):
                call_reads.add(tok.upper)

    for fn_name, start, end in defs:
        if fn_name in _KW:
            continue
        if fn_name in call_reads:
            continue
        # Safety: if the name appears inside a string literal, skip removal
        # (may be dynamically dispatched via Execute/CallByName)
        if fn_name.lower() in str_content:
            # Quick check: does any STRING token contain it?
            found_in_string = False
            for tok in tokenize(src):
                if tok.kind == TokenKind.STRING and fn_name.lower() in tok.value.lower():
                    found_in_string = True
                    break
            if found_in_string:
                continue
        region = src[start:end]
        blank = '\n' * region.count('\n')
        edits.append((start, end, blank))

    return edits


# ---------------------------------------------------------------------------
# VBScript keyword set (never treated as variable reads)
# ---------------------------------------------------------------------------

_KW = frozenset("""
AND BYREF BYVAL CALL CASE CLASS CONST DIM DO EACH ELSE ELSEIF END ERASE ERROR
EXECUTE EXECUTEGLOBAL EXIT FALSE FOR FUNCTION GET IF IN IS LET LOOP MOD NEW
NEXT NOT NOTHING NULL OBJECT ON OPTION OR PRESERVE PRIVATE PUBLIC RANDOMIZE REDIM
REM RESUME SELECT SET STEP STOP SUB THEN TO TRUE UNTIL WEND WHILE WITH XOR
""".split())


if __name__ == '__main__':
    run_tool(
        run,
        description='Remove dead code: dead stores, statically-false If/Do blocks',
        extra_args=[
            {
                'flags': ['--preserve-strings'],
                'action': 'store_true',
                'default': False,
                'help': 'Keep string/number literal RHS assignments even when the LHS is dead',
            },
        ],
    )
