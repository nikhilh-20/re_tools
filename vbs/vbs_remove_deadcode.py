"""vbs_remove_deadcode — liveness-based dead-store removal and false-condition block elimination.

Default mode:
  - Liveness-based dead-store removal: assignments to variables never read are
    deleted. Iterates to fixpoint — removing one dead store can expose more.
  - Declarations (Dim/ReDim/Private/Public, scalar or array): whole statement
    deleted when all declared names are dead; trimmed to keep only live names
    when partially dead. ReDim is only treated as a candidate write when its
    bounds expressions are call-free (see _CALL_LIKE_RE guard) — a bound that
    may have a side effect keeps the whole statement untouched.
  - Unreferenced Function/Sub definitions whose name is never called from
    outside their own body are removed. This is the same reachability
    question already answered for variables above (never read == never
    called) — a definition's shape doesn't change what liveness means.
  - Statically-false If/Do While blocks removed.

--preserve-strings:
  Keep string/number literal RHS assignments even when the LHS is dead
  (safety guard for files not yet run through vbs_propagate_constants).

--aggressive:
  Treats self-referential accumulator chains (e.g. `x = x & f()` repeated,
  with x never read outside its own writer statements) as dead: a variable is
  removed if every read of it is confined to its own writer statements, even
  though it "reads itself" on every line. Off by default because it changes
  what counts as live, not just what counts as reachable — every read really
  does exist, so this is a strictly wider notion of "dead" than reachability.

--remove-empty-loops:
  Also removes empty-body Do/While loops whose condition contains no
  parenthesized call (e.g. `Do While f.AtEndOfStream <> True / Loop` — a
  common anti-sandbox stall). Off by default: such loops are IOC/TTP
  evidence, so the default behaviour only flags them with a marker comment
  rather than silently deleting them.

Analog of PsRemove-DeadCode.

Usage:
    python vbs_remove_deadcode.py --input in.vbs --output out.vbs [--aggressive] [--preserve-strings] [--remove-empty-loops]
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits
from vbsdeoblib.resolver import resolve_const
from vbsdeoblib.statements import split_statements, StatementSpan


def run(src: str, aggressive: bool = False, preserve_strings: bool = False,
        remove_empty_loops: bool = False, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(50):
        src, n = _one_pass(src, aggressive, preserve_strings, remove_empty_loops)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


# ---------------------------------------------------------------------------
# Top-level pass dispatcher
# ---------------------------------------------------------------------------

def _one_pass(src: str, aggressive: bool, preserve_strings: bool,
              remove_empty_loops: bool) -> tuple[str, int]:
    # Sub-pass A: statically-false If blocks (no Else)
    edits = _false_if_edits(src)

    # Sub-pass A2: statically-false single-line 'If cond Then stmt' — the
    # multi-line block regex above can't match these (no End If to anchor on).
    if not edits:
        edits = _false_single_line_if_edits(src)

    # Sub-pass B: statically-false Do While blocks
    if not edits:
        edits = _false_while_edits(src)

    # Sub-pass B2: local dead-store elimination (sequential overwrite of the
    # same variable before any intervening read — e.g. a var reassigned N
    # times in a row for volume inflation, then fully overwritten).
    if not edits:
        edits = _local_dead_store_edits(src)

    # Sub-pass B3: empty-body loop flagging / removal (see --remove-empty-loops).
    if not edits:
        edits = _empty_loop_edits(src, remove_empty_loops)

    # Sub-pass C: liveness-based dead-store removal (default mode); with
    # --aggressive, also catches self-contained self-referential clusters.
    if not edits:
        edits = _dead_store_edits(src, preserve_strings, aggressive)

    # Sub-pass D: unreferenced Function/Sub removal (default mode — same
    # reachability question as sub-pass C, just for definitions instead of
    # variables; it does not redefine liveness, so it isn't --aggressive-gated).
    if not edits:
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
        r'(?im)^[ \t]*If\s+(.+?)\s+Then\s*:?\s*\r?\n'
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
        r'(?im)^[ \t]*Do\s+While\s+(.+?)\s*:?\s*\r?\n'
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


def _find_top_level_then(ctoks: list) -> int | None:
    """Index of a top-level (not inside parens) THEN keyword, or None."""
    depth = 0
    for i, t in enumerate(ctoks):
        if t.kind == TokenKind.OP and t.value == '(':
            depth += 1
        elif t.kind == TokenKind.OP and t.value == ')':
            depth -= 1
        elif depth == 0 and t.kind == TokenKind.IDENT and t.upper == 'THEN':
            return i
    return None


def _false_single_line_if_edits(src: str) -> list[tuple[int, int, str]]:
    """Remove a single-line 'If <always-false-cond> Then <stmt>' entirely.
    _false_if_edits only matches the multi-line 'If ... Then\\n ... End If'
    block form (it anchors on a following End If); a one-liner never opens a
    block at all (see the is_block_open convention used throughout this
    toolkit — a multi-line If header ends with a bare THEN, a one-liner
    doesn't), so it needs its own statement-based pass."""
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    edits: list[tuple[int, int, str]] = []
    for stmt in stmts:
        ctoks = stmt.code_tokens()
        if not ctoks or not (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'IF'):
            continue
        last = ctoks[-1]
        if last.kind == TokenKind.IDENT and last.upper == 'THEN':
            continue  # multi-line block header — handled by _false_if_edits
        then_idx = _find_top_level_then(ctoks)
        if then_idx is None or then_idx == 1:
            continue  # malformed / no condition tokens
        cond_toks = ctoks[1:then_idx]
        val = resolve_const(cond_toks)
        if val is not None and _falsy(val):
            edits.append((stmt.start, stmt.end, ''))
    return edits


# ---------------------------------------------------------------------------
# Sub-pass B3: empty-body loop flagging / removal
# ---------------------------------------------------------------------------

_EMPTY_LOOP_MARKER = "' [deobfuscator] empty-body loop - likely anti-sandbox stall"

# A blank-or-comment-only line, repeated zero or more times, is the body.
_BLANK_OR_COMMENT_LINE = r"(?:[ \t]*(?:'[^\r\n]*)?\r?\n)*"

_EMPTY_DO_PRETEST_PAT = re.compile(
    r"(?im)^[ \t]*Do\s+(?:While|Until)\s+(.+?)\s*\r?\n"
    + _BLANK_OR_COMMENT_LINE +
    r"[ \t]*Loop\b[^\r\n]*"
)
_EMPTY_WHILE_WEND_PAT = re.compile(
    r"(?im)^[ \t]*While\s+(.+?)\s*\r?\n"
    + _BLANK_OR_COMMENT_LINE +
    r"[ \t]*Wend\b[^\r\n]*"
)
_EMPTY_DO_POSTTEST_PAT = re.compile(
    r"(?im)^[ \t]*Do[ \t]*\r?\n"
    + _BLANK_OR_COMMENT_LINE +
    r"[ \t]*Loop\s+(?:While|Until)\s+(.+?)[^\r\n]*"
)

_CALL_LIKE_RE = re.compile(r'[A-Za-z_]\w*\s*\(')


def _condition_has_call(cond_text: str) -> bool:
    """Heuristic purity check: does the loop condition contain something
    that looks like a function/method call with arguments? A parenless
    property read (e.g. '.AtEndOfStream') is treated as pure; this is a
    documented heuristic, not a proof."""
    return bool(_CALL_LIKE_RE.search(cond_text))


def _already_flagged(src: str, pos: int) -> bool:
    window_start = max(0, pos - 200)
    return _EMPTY_LOOP_MARKER in src[window_start:pos]


def _empty_loop_edits(src: str, remove: bool) -> list[tuple[int, int, str]]:
    edits: list[tuple[int, int, str]] = []
    for pat in (_EMPTY_DO_PRETEST_PAT, _EMPTY_WHILE_WEND_PAT, _EMPTY_DO_POSTTEST_PAT):
        for m in pat.finditer(src):
            cond_text = m.group(1).strip() if m.groups() else ''
            indent = _line_indent(src, m.start())

            if remove:
                if _condition_has_call(cond_text):
                    continue  # not safe to assume side-effect-free — leave alone
                region = src[m.start(): m.end()]
                extra_blank = '\n' * max(0, region.count('\n') - 1)
                replacement = f"{indent}{_EMPTY_LOOP_MARKER}\n{extra_blank}"
                edits.append((m.start(), m.end(), replacement))
            else:
                if _already_flagged(src, m.start()):
                    continue
                edits.append((m.start(), m.start(), f"{indent}{_EMPTY_LOOP_MARKER}\n"))
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

# RHS has observable side effects: object creation or method call
_side_effect_re = re.compile(r'(?i)(CreateObject|GetObject|\.\w+\s*\()')

# Dynamic-dispatch constructs: their string arguments can reference a variable
# by name without the tokenizer ever seeing an IDENT read for it.
_DYNAMIC_EXEC_KW = frozenset(['EXECUTE', 'EXECUTEGLOBAL', 'EVAL', 'CALLBYNAME', 'GETREF'])


def _build_line_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    for line in lines[:-1]:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _has_dynamic_exec(tokens: list) -> bool:
    return any(t.kind == TokenKind.IDENT and t.upper in _DYNAMIC_EXEC_KW for t in tokens)


def _rhs_has_side_effect(rhs_toks: list, src: str, rhs_end: int) -> bool:
    """Side-effect scan restricted to live code: STRING token spans are
    blanked out first so text that merely looks like '.Method(' or
    'CreateObject' inside a string argument (e.g. an embedded PowerShell/JS
    payload passed to Replace()) isn't mistaken for an actual VBScript call
    performed by the assignment statement itself.

    Exception: if the RHS's own live code invokes a dynamic-dispatch
    construct (Execute/ExecuteGlobal/Eval/CallByName/GetRef), a string
    argument to it may genuinely execute as code (e.g.
    Eval("CreateObject(...)")), so fall back to the old unblanked scan
    rather than risk deleting a statement with a real side effect."""
    if not rhs_toks:
        return False
    rhs_start = rhs_toks[0].start
    rhs_src = src[rhs_start:rhs_end]
    if _has_dynamic_exec(rhs_toks):
        return bool(_side_effect_re.search(rhs_src))
    chars = list(rhs_src)
    for t in rhs_toks:
        if t.kind == TokenKind.STRING:
            for i in range(t.start - rhs_start, t.end - rhs_start):
                chars[i] = ' '
    return bool(_side_effect_re.search(''.join(chars)))


def _word_present(name_upper: str, text: str) -> bool:
    """Case-insensitive whole-identifier search (VBScript identifier chars == \\w)."""
    return re.search(r'\b' + re.escape(name_upper) + r'\b', text, re.IGNORECASE) is not None


def _names_referenced_in_strings(tokens: list, candidate_names: set) -> set:
    """Subset of candidate_names that appear (word-boundary) inside any STRING
    token's raw text. Used to protect names that are only 'read' dynamically,
    e.g. via Execute("... Flerbrugerdrifternes ...")."""
    if not candidate_names:
        return set()
    combined = '\n'.join(t.value for t in tokens if t.kind == TokenKind.STRING)
    if not combined:
        return set()
    return {name for name in candidate_names if _word_present(name, combined)}


def _is_pure_literal_toks(rhs_toks: list) -> bool:
    """True if rhs_toks is a single string or number literal token."""
    return len(rhs_toks) == 1 and rhs_toks[0].kind in (TokenKind.STRING, TokenKind.NUMBER)


def _parse_dim_items(ctoks: list, start: int = 1) -> list:
    """Return [(is_array, name_tok, end_offset), ...] for every name declared
    by a Dim/ReDim/Private/Public variable-declaration statement's code
    tokens, starting at index *start* (just past the leading keyword(s)).

    Scalars: end_offset == name_tok.end. Array-dimensioned names (foo(n)):
    end_offset spans past the matched ')' so callers can reconstruct the
    exact original declarator text via src[name_tok.start:end_offset] — the
    bound expression itself is never a removal candidate and must be
    preserved verbatim if the statement is partially rewritten. Bound
    commas at any paren depth > 0 are never mistaken for item separators.

    Strict grammar: IDENT ['(' ... ')'] (',' IDENT ['(' ... ')'])* — returns
    [] on any malformed shape (stray keyword, unbalanced parens, trailing
    garbage) rather than guessing, same policy as _parse_const_items. This
    matters beyond Dim: reused for Private/Public declarators, where a
    non-declaration statement like 'Public Function Foo()' must be rejected
    outright rather than misread as declaring FUNCTION/Foo as variables."""
    items: list = []
    i = start
    n = len(ctoks)
    if i >= n:
        return []
    while i < n:
        t = ctoks[i]
        if t.kind != TokenKind.IDENT or t.upper in _KW:
            return []  # malformed: bail out rather than guess
        i += 1
        if i < n and ctoks[i].kind == TokenKind.OP and ctoks[i].value == '(':
            depth = 1
            k = i + 1
            while k < n and depth > 0:
                if ctoks[k].kind == TokenKind.OP and ctoks[k].value == '(':
                    depth += 1
                elif ctoks[k].kind == TokenKind.OP and ctoks[k].value == ')':
                    depth -= 1
                k += 1
            if depth != 0:
                return []  # unbalanced parens: malformed
            items.append((True, t, ctoks[k - 1].end))
            i = k
        else:
            items.append((False, t, t.end))
        if i < n:
            if ctoks[i].kind == TokenKind.OP and ctoks[i].value == ',':
                i += 1
            else:
                return []  # trailing garbage after a declarator: malformed
    return items


def _parse_const_items(ctoks: list, start: int) -> list:
    """Return [(name_tok, item_end_offset), ...] for every name declared by a
    'Const ...' (or 'Public/Private Const ...') statement's code tokens,
    starting at index *start* (just past the CONST keyword). item_end_offset
    is the end of that item's RHS expression, so src[name_tok.start:item_end]
    reconstructs the exact 'name = expr' text — same convention as
    _parse_dim_items. Paren depth is tracked so a parenthesized expression's
    internal commas aren't mistaken for item separators. Returns [] on any
    malformed shape rather than guessing."""
    items: list = []
    i = start
    n = len(ctoks)
    if i >= n:
        return []
    while i < n:
        if ctoks[i].kind != TokenKind.IDENT:
            return []  # malformed: bail out rather than guess
        name_tok = ctoks[i]
        i += 1
        if i >= n or not (ctoks[i].kind == TokenKind.OP and ctoks[i].value == '='):
            return []
        i += 1  # skip '='
        rhs_start = i
        depth = 0
        while i < n:
            t = ctoks[i]
            if t.kind == TokenKind.OP and t.value == '(':
                depth += 1
            elif t.kind == TokenKind.OP and t.value == ')':
                depth -= 1
            elif t.kind == TokenKind.OP and t.value == ',' and depth == 0:
                break
            i += 1
        if i == rhs_start:
            return []  # '=' with no RHS: malformed
        items.append((name_tok, ctoks[i - 1].end))
        if i < n:
            i += 1  # skip the comma that ended the inner scan
    return items


def _line_indent(src: str, offset: int) -> str:
    line_start = src.rfind('\n', 0, offset) + 1
    prefix = src[line_start:offset]
    return prefix if prefix.strip() == '' else ''


def _self_contained(name: str, reads_by_name: dict, writer_spans: dict) -> bool:
    """True if every read of *name* falls inside one of its own writer
    statements' byte spans — i.e. the value never escapes the chain of
    statements that write it. A name with zero reads is vacuously
    self-contained (matches the plain 'never read' case)."""
    spans = writer_spans.get(name, [])
    for pos in reads_by_name.get(name, []):
        if not any(s <= pos < e for s, e in spans):
            return False
    return True


def _redim_bounds_have_call(items: list, src: str) -> bool:
    """True if any array declarator's bound expression in a parsed ReDim
    statement looks call-like (see _CALL_LIKE_RE), e.g. ReDim x(Setup()).
    Unlike Dim, ReDim bounds are runtime expressions and VBScript grammar
    does not forbid a call there, so this guard exists for ReDim only.
    Scans strictly between each declarator's own parens — src[nm.end:end_off]
    starts right after the name, so the declared name itself can never
    self-match the leading '(' of its own bounds."""
    for is_arr, nm, end_off in items:
        if is_arr and _CALL_LIKE_RE.search(src[nm.end:end_off]):
            return True
    return False


def _dead_store_edits(src: str, preserve_strings: bool, aggressive: bool = False) -> list[tuple[int, int, str]]:
    """File-global liveness: return edits for assignment/declaration
    statements whose name is never read anywhere else in the file.
    Statement-based (not line-based) so line-continuations and colon-joined
    statements are handled correctly; guarded against names that are only
    referenced dynamically inside a string literal passed to
    Execute/ExecuteGlobal/Eval/CallByName/GetRef (the tokenizer never sees
    those as IDENT reads).

    Declarations — Dim, ReDim, Private, Public, scalar or array — are all
    judged by one liveness rule: binding a name is a write, not a read,
    regardless of syntactic shape. ReDim is the sole exception requiring a
    purity guard (_redim_bounds_have_call): its bounds are runtime
    expressions, so a statement with a call-like bound is left untouched
    entirely rather than risk deleting a side effect.

    --aggressive: a name is dead not only when it is never read at all, but
    also when every read of it is confined to its own writer statements (a
    self-referential accumulator chain, e.g. `x = x & f()` repeated with no
    read of x anywhere else) — a self-contained cluster that can never be
    observed once removed. Off by default, matching the PS analog's
    -Aggressive-gated 'dead variable cluster' pass."""
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    if not stmts:
        return []

    assign_stmts: list[tuple[str, StatementSpan, list]] = []   # (name_upper, stmt, rhs_toks)
    decl_stmts: list[tuple[StatementSpan, list]] = []           # (stmt, [(is_array, name_tok, end_offset), ...])
    const_stmts: list[tuple[StatementSpan, list]] = []          # (stmt, [(name_tok, item_end_offset), ...])
    lhs_offsets: set[int] = set()
    decl_name_offsets: set[int] = set()
    const_name_offsets: set[int] = set()

    for stmt in stmts:
        ctoks = stmt.code_tokens()
        if not ctoks:
            continue
        kw0 = ctoks[0].upper if ctoks[0].kind == TokenKind.IDENT else ''

        if kw0 == 'DIM':
            items = _parse_dim_items(ctoks, 1)
            for _, nm, _ in items:
                decl_name_offsets.add(nm.start)
            if items:
                decl_stmts.append((stmt, items))
            continue

        if kw0 == 'REDIM':
            start = 1
            if (len(ctoks) > 1 and ctoks[1].kind == TokenKind.IDENT
                    and ctoks[1].upper == 'PRESERVE'):
                start = 2
            items = _parse_dim_items(ctoks, start)
            if items and not _redim_bounds_have_call(items, src):
                for _, nm, _ in items:
                    decl_name_offsets.add(nm.start)
                decl_stmts.append((stmt, items))
            # Malformed, or a bound looks call-like: leave the statement
            # alone entirely — its idents fall through to the read-scan.
            continue

        const_start = None
        if kw0 == 'CONST':
            const_start = 1
        elif (kw0 in ('PUBLIC', 'PRIVATE') and len(ctoks) > 1
                and ctoks[1].kind == TokenKind.IDENT and ctoks[1].upper == 'CONST'):
            const_start = 2
        if const_start is not None:
            items = _parse_const_items(ctoks, const_start)
            for nm, _ in items:
                const_name_offsets.add(nm.start)
            if items:
                const_stmts.append((stmt, items))
            continue

        if (kw0 in ('PUBLIC', 'PRIVATE') and len(ctoks) > 1
                and ctoks[1].kind == TokenKind.IDENT and ctoks[1].upper not in _KW):
            items = _parse_dim_items(ctoks, 1)
            if items:
                for _, nm, _ in items:
                    decl_name_offsets.add(nm.start)
                decl_stmts.append((stmt, items))
                continue
            # Fails the strict declarator grammar (e.g. 'Public Default
            # Property Get X') — fall through to the generic read-scan
            # below, same as any other unrecognized statement shape.

        idx = 0
        if ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper in ('SET', 'LET'):
            idx = 1
        if (idx < len(ctoks) - 1 and ctoks[idx].kind == TokenKind.IDENT
                and ctoks[idx + 1].kind == TokenKind.OP and ctoks[idx + 1].value == '='):
            name_tok = ctoks[idx]
            if name_tok.upper not in _KW:
                assign_stmts.append((name_tok.upper, stmt, ctoks[idx + 2:]))
                lhs_offsets.add(name_tok.start)
        # Anything else (Call, single-line If, bare expression, ...): all of
        # its IDENT tokens fall through to the read-scan below.

    # --- Collect reads: any IDENT token outside recognized LHS/decl/Const positions ---
    excluded = lhs_offsets | decl_name_offsets | const_name_offsets
    reads_by_name: dict[str, list] = {}
    for t in tokens:
        if t.kind == TokenKind.IDENT and t.upper not in _KW and t.start not in excluded:
            reads_by_name.setdefault(t.upper, []).append(t.start)

    all_decl_names: set[str] = {nm.upper for _, items in decl_stmts for _, nm, _ in items}
    const_names: set[str] = {nm.upper for _, items in const_stmts for nm, _ in items}
    assign_names: set[str] = {name for name, _, _ in assign_stmts}
    candidates = assign_names | all_decl_names | const_names

    if aggressive:
        writer_spans: dict[str, list] = {}
        for name, stmt, _ in assign_stmts:
            writer_spans.setdefault(name, []).append((stmt.start, stmt.end))
        for stmt, items in decl_stmts:
            for _, nm, _ in items:
                writer_spans.setdefault(nm.upper, []).append((stmt.start, stmt.end))
        dead: set[str] = {n for n in candidates if _self_contained(n, reads_by_name, writer_spans)}
    else:
        reads: set[str] = set(reads_by_name)
        dead = {n for n in candidates if n not in reads}

    if not dead:
        return []

    # Guard: names only referenced from inside a string literal (potential
    # dynamic read via Execute/Eval/etc.) are not safe to remove.
    if _has_dynamic_exec(tokens):
        dead -= _names_referenced_in_strings(tokens, dead)
    if not dead:
        return []

    edits: list[tuple[int, int, str]] = []
    for name, stmt, rhs_toks in assign_stmts:
        if name not in dead:
            continue
        if _rhs_has_side_effect(rhs_toks, src, stmt.end):
            continue
        if preserve_strings and _is_pure_literal_toks(rhs_toks):
            continue
        edits.append((stmt.start, stmt.end, ''))

    # Dim/ReDim/Private/Public declarators all rewrite the same way: keep
    # only the live names, preserving each survivor's exact original text
    # (including array bounds) verbatim, and the exact leading keyword(s)
    # via a prefix slice — same convention as the Const path below,
    # generalized to cover 'ReDim Preserve ' / 'Public ' / 'Private ' too.
    for stmt, items in decl_stmts:
        live_parts: list[str] = []
        changed = False
        for is_arr, nm, end_off in items:
            if nm.upper in dead:
                changed = True
            elif is_arr:
                live_parts.append(src[nm.start:end_off])   # verbatim — preserves bounds
            else:
                live_parts.append(nm.value)
        if not changed:
            continue  # nothing dead in this statement, no edit
        if not live_parts:
            edits.append((stmt.start, stmt.end, ''))
        else:
            prefix = src[stmt.start:items[0][1].start]
            trailing = src[items[-1][2]: stmt.end]
            edits.append((stmt.start, stmt.end, f'{prefix}{", ".join(live_parts)}{trailing}'))

    # Const declarations are always literal by VBScript grammar, so removing
    # a dead one is unconditionally safe — not gated by preserve_strings
    # (which exists to protect assignments whose deadness might be an
    # artifact of not having run vbs_propagate_constants yet).
    for stmt, items in const_stmts:
        live_parts: list[str] = []
        changed = False
        for name_tok, item_end in items:
            if name_tok.upper in dead:
                changed = True
            else:
                live_parts.append(src[name_tok.start:item_end])
        if not changed:
            continue
        if not live_parts:
            edits.append((stmt.start, stmt.end, ''))
        else:
            prefix = src[stmt.start:items[0][0].start]
            trailing = src[items[-1][1]: stmt.end]
            edits.append((stmt.start, stmt.end, f'{prefix}{", ".join(live_parts)}{trailing}'))

    return edits


# ---------------------------------------------------------------------------
# Sub-pass B2: local (sequential-overwrite) dead-store elimination
# ---------------------------------------------------------------------------

def _match_simple_assignment(ctoks: list) -> tuple[str, list] | None:
    """Return (name_upper, rhs_tokens) for a bare 'name = rhs' or 'Let name =
    rhs' statement. Returns None for Dim/Const/ReDim/Set forms (object refs
    and non-reassignable declarations are never local dead-store candidates)."""
    if not ctoks:
        return None
    if ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper in ('DIM', 'CONST', 'REDIM', 'SET'):
        return None
    idx = 1 if (ctoks[0].kind == TokenKind.IDENT and ctoks[0].upper == 'LET') else 0
    if idx >= len(ctoks) - 1:
        return None
    name_tok, eq_tok = ctoks[idx], ctoks[idx + 1]
    if name_tok.kind != TokenKind.IDENT or name_tok.upper in _KW:
        return None
    if not (eq_tok.kind == TokenKind.OP and eq_tok.value == '='):
        return None
    return name_tok.upper, ctoks[idx + 2:]


_PROC_PAT = re.compile(
    r'(?im)^[ \t]*(?:Function|Sub|Class)\s+\w+\s*(?:\([^)]*\))?[^\r\n]*\r?\n'
    r'(?:(?![ \t]*End\s+(?:Function|Sub|Class))[^\r\n]*\r?\n)*'
    r'[ \t]*End\s+(?:Function|Sub|Class)\b[^\r\n]*'
)
_PROPERTY_PAT = re.compile(
    r'(?im)^[ \t]*Property\s+(?:Get|Let|Set)\s+\w+\s*(?:\([^)]*\))?[^\r\n]*\r?\n'
    r'(?:(?![ \t]*End\s+Property)[^\r\n]*\r?\n)*'
    r'[ \t]*End\s+Property\b[^\r\n]*'
)


def _proc_body_ranges(src: str) -> list[tuple[int, int]]:
    """Byte ranges of every Function/Sub/Class/Property body in the file."""
    ranges = [(m.start(), m.end()) for m in _PROC_PAT.finditer(src)]
    ranges += [(m.start(), m.end()) for m in _PROPERTY_PAT.finditer(src)]
    return ranges


def _local_dead_store_edits(src: str) -> list[tuple[int, int, str]]:
    """Remove a store to X when a later unconditional top-level store to X
    exists with no intervening read of X anywhere (at any nesting depth) —
    e.g. a variable reassigned N times in a row for volume inflation, where
    only the final assignment before first use matters.

    Safety conditions (see plan): (1) the removed store's RHS must resolve to
    a pure constant [resolve_const], (2) both stores occur at module top
    level (block_depth == 0 for both, which — because block open/close is
    tracked exactly — also guarantees any intervening blocks are balanced),
    (3) no read of X anywhere between them regardless of depth, (4) X is not
    read inside any Function/Sub/Class/Property body anywhere in the file
    (no call-graph analysis; conservative file-wide guard), (5) X does not
    appear inside any string literal when dynamic-exec constructs are present
    in the file, (6) Const/ReDim/Set-prefixed forms are excluded outright.
    """
    tokens = tokenize(src)
    stmts = split_statements(tokens)
    if not stmts:
        return []

    dynamic_exec = _has_dynamic_exec(tokens)
    proc_ranges = _proc_body_ranges(src)

    pending: dict[str, StatementSpan] = {}
    dead_candidates: list[tuple[str, StatementSpan]] = []
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
            # Multi-line 'If ... Then' block header ends with a bare THEN;
            # single-line 'If c Then stmt' does not open a block at all.
            last = ctoks[-1]
            is_block_open = last.kind == TokenKind.IDENT and last.upper == 'THEN'
        elif kw in ('FOR', 'DO', 'WHILE', 'SELECT', 'WITH', 'FUNCTION', 'SUB', 'CLASS', 'PROPERTY'):
            is_block_open = True

        cur_depth = depth
        if is_block_open:
            depth += 1

        assign = _match_simple_assignment(ctoks) if (cur_depth == 0 and not is_block_open) else None

        if assign is not None:
            name, rhs_toks = assign
            # Reads inside the RHS (including self-reference) clear pending
            # entries *before* we consider this a fresh overwrite.
            for t in rhs_toks:
                if t.kind == TokenKind.IDENT and t.upper not in _KW:
                    pending.pop(t.upper, None)
            if name in pending:
                dead_candidates.append((name, pending[name]))
            if resolve_const(rhs_toks) is not None:
                pending[name] = stmt
            else:
                pending.pop(name, None)
        else:
            # Any other statement (block header/closer, non-assignment
            # top-level statement, or a statement inside a block): every
            # identifier it mentions conservatively clears pending — a read,
            # a conditional write, or anything in between is treated the
            # same way (losing an optimization opportunity is always safe;
            # removing a live store is not).
            for t in ctoks:
                if t.kind == TokenKind.IDENT and t.upper not in _KW:
                    pending.pop(t.upper, None)

    if not dead_candidates:
        return []

    names_needed = {name for name, _ in dead_candidates}

    proc_read_names: set[str] = set()
    if proc_ranges:
        for t in tokens:
            if t.kind == TokenKind.IDENT and t.upper in names_needed:
                if any(a <= t.start < b for a, b in proc_ranges):
                    proc_read_names.add(t.upper)

    stringy_names: set[str] = set()
    if dynamic_exec:
        stringy_names = _names_referenced_in_strings(tokens, names_needed)

    edits: list[tuple[int, int, str]] = []
    for name, stmt in dead_candidates:
        if name in proc_read_names or name in stringy_names:
            continue
        edits.append((stmt.start, stmt.end, ''))

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
    """Return edits that remove Function/Sub definitions whose name is never
    called from outside their own body.

    Liveness is judged *per candidate*: an occurrence of a function's name
    only disqualifies it from removal when that occurrence falls outside the
    candidate's own [start, end) definition range. A purely self-recursive
    function (every occurrence of its name is inside its own body) is still
    correctly eligible for removal — but a call made from a *different*
    function's body now correctly counts as a real, external use, instead of
    being invisible just because it happens to sit inside someone else's
    Function/Sub block (see the toolkit README / bug report for why a
    single shared "inside any def" exclusion was wrong: it made every
    function-to-function call invisible, not just self-recursion)."""
    edits: list[tuple[int, int, str]] = []

    defs = [(m.group(2).upper(), m.start(), m.end()) for m in _fn_sub_pat.finditer(src)]
    if not defs:
        return edits

    # Build LHS exclusions (assignment LHS tokens)
    lines = src.splitlines(keepends=True)
    line_offsets = _build_line_offsets(lines)
    lhs_byte_offsets: set[int] = set()
    for li, line in enumerate(lines):
        m = _lhs_re.match(line)
        if m and m.group(1).upper() not in _KW:
            lhs_byte_offsets.add(line_offsets[li] + m.start(1))

    # Every non-LHS occurrence of each candidate name, by name.
    all_tokens = tokenize(src)
    occurrences_by_name: dict[str, list[int]] = {}
    for tok in all_tokens:
        if tok.kind == TokenKind.IDENT and tok.upper not in _KW and tok.start not in lhs_byte_offsets:
            occurrences_by_name.setdefault(tok.upper, []).append(tok.start)

    def has_external_occurrence(name: str, own_start: int, own_end: int) -> bool:
        return any(pos < own_start or pos >= own_end
                   for pos in occurrences_by_name.get(name, ()))

    candidate_names = {fn_name for fn_name, start, end in defs
                        if fn_name not in _KW and not has_external_occurrence(fn_name, start, end)}
    # Safety: a name only reachable via a word-boundary match inside a string
    # literal may be dynamically dispatched (Execute/CallByName/GetRef/...).
    stringy_names = _names_referenced_in_strings(all_tokens, candidate_names)

    for fn_name, start, end in defs:
        if fn_name in _KW or fn_name not in candidate_names or fn_name in stringy_names:
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
            {
                'flags': ['--remove-empty-loops'],
                'action': 'store_true',
                'default': False,
                'help': 'Remove (rather than just flag) empty-body loops with a call-free condition',
            },
        ],
    )
