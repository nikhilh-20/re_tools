"""vbs_propagate_constants — flow-sensitive constant propagation.

Walks top-level statements in order. For each variable assigned a constant
on the RHS, records the value. Downstream reads of that variable in the same
or later statements are replaced with the literal — provided the variable is
not re-assigned to a non-constant or modified inside a block (If/For/While).

Inside a block body, two regimes apply depending on the *kind* of every
block currently open:

  - Non-looping blocks (If/Select/With/Function/Sub/Class/Property): body
    statements execute in a fixed straight-line order at most once per entry,
    so a constant computed partway through (e.g. `Grejss = Reserveres` where
    Reserveres is already known) is tracked in a scope-local env and folded
    into later statements in the *same* straight-line run. This local scope
    is cleared the instant any block opens or closes (depth changes), so it
    never leaks across a branch/call boundary.
  - Looping blocks (For/Do/While) anywhere in the current nesting: local
    tracking is disabled entirely and the original fully-conservative
    behaviour applies (every block-depth assignment is killed, never
    folded), because a value computed from one iteration's inputs is not
    generally valid for the next.

Analog of PsPropagate-Constants.

Usage:
    python vbs_propagate_constants.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits, quote_vbs, format_number
from vbsdeoblib.resolver import resolve_const, Const
from vbsdeoblib.statements import split_statements, find_block_end


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    substituted_total = 0
    # Computed once from the original source: whether a name is ever bound is
    # a property of the program as written, not of a partly folded intermediate.
    name_counts = _build_name_counts(src)
    for _ in range(50):
        src, n, s = _one_pass(src, name_counts)
        changed_total += n
        substituted_total += s
        if n == 0:
            break
    return src, {'changed': changed_total, 'substituted_reads': substituted_total}


# Above this many characters, a tracked string constant is not kept in
# `local_env` by the in-block assignment path (_apply_inblock_assignment)
# — it is dropped (killed) instead of cached. This flat cap survives only
# for that one path: its tracked values are scoped to a single
# straight-line block (not the whole file), so the risk it guards against
# is inherently smaller there, and it isn't implicated in the bug this
# cap otherwise caused (see the design note above _count_self_refs for the
# real fix, which replaces this exact style of size-only cap everywhere
# else in this module).
_MAX_TRACKED_STRING_LEN = 8192

# --- Growth control, redesigned around two genuinely distinct risks -------
#
# A size cap conflates two unrelated failure modes: (1) UNBOUNDED GROWTH —
# a value that feeds itself (`x = x & x`) doubles every evaluation and
# compounds without limit; genuinely pathological, must be blocked
# structurally. (2) BOUNDED-BUT-LARGE OUTPUT — one big constant
# substituted at a handful of sites; nothing is exploding, this is the
# tool doing its job. For a *deobfuscator specifically*, refusing (2) is
# anti-purpose: a large embedded blob (e.g. a placeholder-padded payload
# string) is exactly what's most worth revealing. Any magnitude-based cap
# — flat, or proportional to file size — ends up blocking (2) whenever one
# constant is a large fraction of its own file, which is the ordinary
# shape of a VBS dropper (stash one big blob, use it a few times), not an
# edge case.
#
# So (1) is caught structurally (_count_self_refs — see below), with no
# size involved at all, and (2) is simply allowed. The one thing size
# still guards is (1)'s residual blind spot: a multiplicative chain spread
# across *distinct* names (`y = x & x`, then `z = y & y`) has zero
# self-references at any single step, and a compounding call like
# `Replace(x, "a", "bb")` only has one — neither trips the structural
# check. _SubstitutionBudget closes that gap with a single cumulative
# per-pass resource ceiling, reusing _MAX_COLLAPSED_VALUE_LEN (below) as
# the memory bound it already is rather than inventing a new constant.


def _count_self_refs(lhs_upper: str, rhs_toks: list) -> int:
    """How many times the assignment's own LHS appears in its RHS. 0 or 1
    can only ever ADD source-bounded text per evaluation (the classic
    accumulator, `x = x & "chunk"`); 2 or more MULTIPLIES the value every
    evaluation (`x = x & x` doubles) — that compounds without bound
    regardless of magnitude, so it's refused unconditionally rather than
    judged by size. Checked on the original rhs_toks, before _substitute
    rewrites them, so it reflects the statement as written, not whatever
    the LHS currently resolves to."""
    return sum(1 for t in rhs_toks
               if t.kind == TokenKind.IDENT and t.upper == lhs_upper)


class _SubstitutionBudget:
    """Cumulative per-pass ceiling on tracked-value 'weight' (value size x
    projected read count), reusing _MAX_COLLAPSED_VALUE_LEN as the same
    64MB memory bound it already is for self-append run scanning — not a
    new invented constant. Exists solely to catch growth patterns
    _count_self_refs can't see by construction (a chain spread across
    distinct names, or a compounding function call) — see the design note
    above. charge() always floors its request at 1 (even a value with no
    projected further reads still counts its own footprint once), which
    is what lets a single genuinely oversized self-append collapse (e.g.
    from a chain whose own internal scan-time guard let it reach close to
    64MB before stopping) exhaust the budget outright — starving every
    subsequent statement in the same pass of any further tracking, the
    same backstop the old per-value cap provided for that specific case,
    but earned here as a side effect of a real resource ceiling instead of
    an arbitrary size threshold."""

    def __init__(self, limit: int | None = None):
        self._remaining = _MAX_COLLAPSED_VALUE_LEN if limit is None else limit

    def charge(self, weight: int) -> bool:
        weight = max(1, weight)
        if weight > self._remaining:
            return False
        self._remaining -= weight
        return True

# Resource guard for a self-append *run* while it is being resolved (see
# _try_collapse_self_append_run), independent of the structural check
# above. A legitimate accumulator's final size is proportional to how much
# source text built it; the pathological case this guards against is a
# self-referencing link like `x = x & x`, which doubles the accumulator
# every link regardless of chain length — an absolute bound is enough
# because such growth crosses any bound within a handful of doublings
# (~log2(bound) links), so the scan stops almost immediately rather than
# needing a per-link growth heuristic. Also reused, unmodified, as
# _SubstitutionBudget's default per-pass ceiling — see above.
_MAX_COLLAPSED_VALUE_LEN = 64 * 1024 * 1024

# Minimum number of self-append links (statements after the seed) required
# before a run is collapsed. 1 means "any run that actually appends at
# least once" collapses — deliberately not tuned higher: a resolved
# self-append run is emitted as its resolved value regardless of how short
# it happens to be, with no chain-length magic number.
_MIN_APPEND_LINKS_TO_COLLAPSE = 1

# Statements led by one of these keywords only ever *declare* names (Dim,
# ReDim, Const/Public/Private without an initializer already handled by the
# assignment path) — they never call anything, so _kill_call_stmt must never
# see them: killing a name here means the tool would refuse to ever track
# it, even at its very first real assignment later in the same file.
_DECL_KEYWORDS = frozenset(['DIM', 'REDIM', 'CONST', 'PUBLIC', 'PRIVATE'])


def _exceeds_cap(val: Const) -> bool:
    return isinstance(val, str) and len(val) > _MAX_TRACKED_STRING_LEN


def _const_to_literal(v: Const) -> str:
    if isinstance(v, str):
        return quote_vbs(v)
    return format_number(v)


def _is_false_const(v: Const) -> bool:
    """VBScript falsiness of an already-resolved constant (comparisons here
    resolve to -1/0, not Python True/False — see vbsdeoblib.resolver)."""
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        return v.lower() in ('', 'false', '0')
    return False


def _one_pass(src: str, name_counts: dict[str, int] | None = None) -> tuple[str, int, int]:
    tokens = tokenize(src)
    stmts  = split_statements(tokens)
    env: dict[str, Const] = {}   # name_upper -> value
    # Track which names are "killed" (assigned non-constant or assigned inside a block)
    killed: set[str] = set()
    block_depth: int = 0  # nesting depth inside block structures
    block_kinds: list[str] = []       # stack of 'LOOP' | 'OTHER' per open block
    local_env: dict[str, Const] = {}  # straight-line-scoped constants inside a block
    skip_until: int = -1              # set past a just-collapsed self-append run
    budget = _SubstitutionBudget()    # cumulative cross-name growth backstop, see above

    edits: list[tuple[int, int, str]] = []

    for stmt_i, stmt in enumerate(stmts):
        if stmt_i < skip_until:
            continue

        ctoks = stmt.code_tokens()
        if not ctoks:
            continue

        kw = ctoks[0].upper if ctoks[0].kind == TokenKind.IDENT else ''

        # Block closers: NEXT/LOOP/WEND each close exactly one level.
        if kw in ('NEXT', 'LOOP', 'WEND'):
            if block_kinds:
                block_kinds.pop()
            block_depth = max(0, block_depth - 1)
            local_env.clear()
            continue

        # END closes one level only when followed by another keyword
        # (END IF, END SUB, END FUNCTION, END WITH, END SELECT, END CLASS,
        # END PROPERTY). Bare END (script terminator) does not change depth.
        if kw == 'END':
            if len(ctoks) > 1 and ctoks[1].kind == TokenKind.IDENT:
                if block_kinds:
                    block_kinds.pop()
                block_depth = max(0, block_depth - 1)
                local_env.clear()
            continue

        # Block openers: kill any variable assigned in the header line, then
        # increment depth so body statements are processed differently below.
        # A single-line 'If c Then stmt' does NOT open a block (no matching
        # End If ever follows it) — only the multi-line header form (ending
        # in a bare THEN) does.
        is_block_open = False
        loop_open = False
        if kw == 'IF':
            last = ctoks[-1]
            is_block_open = last.kind == TokenKind.IDENT and last.upper == 'THEN'
        elif kw in ('FOR', 'DO', 'WHILE'):
            is_block_open = True
            loop_open = True
        elif kw in ('SELECT', 'WITH', 'FUNCTION', 'SUB', 'CLASS', 'PROPERTY'):
            is_block_open = True

        if is_block_open:
            _kill_assignments(ctoks, env, killed)
            if loop_open:
                # Proactively kill every name this loop's body assigns
                # anywhere (any nesting depth), *before* any body statement
                # is substituted. A lazy kill (only when the pass physically
                # reaches that statement) is too late whenever the loop
                # reads the name earlier in its body than it rewrites it —
                # the common read-then-increment/offset-accumulator shape —
                # which would otherwise fold every in-loop read to whatever
                # constant was known before the loop ever started.
                end_i = find_block_end(stmts, stmt_i)
                if end_i is not None:
                    _kill_loop_body_assignments(stmts, stmt_i + 1, end_i, env, killed)
            block_kinds.append('LOOP' if loop_open else 'OTHER')
            block_depth += 1
            local_env.clear()
            continue

        # --- Inside a block body (depth > 0) ---
        if block_depth > 0:
            in_loop = 'LOOP' in block_kinds
            # A single-line 'If cond Then stmt' embeds an ordinary statement
            # after THEN on the same logical line (it never opens a block —
            # see is_block_open above). Flattening the whole line into one
            # token stream for substitution would treat that embedded
            # statement's own assignment target as a read; split at the
            # top-level THEN so each half gets the rule that actually fits it.
            then_idx = _find_top_level_then(ctoks) if kw == 'IF' else None
            if then_idx is not None:
                merged = env if in_loop else {**env, **local_env}
                cond_sub = _substitute(ctoks[1:then_idx], merged, edits)
                cond_val = resolve_const(cond_sub, merged)
                stmt_part = ctoks[then_idx + 1:]
                if _is_assignment(stmt_part):
                    lhs_name, rhs_toks = _split_assignment(stmt_part)
                    if lhs_name:
                        # The write is *conditional*: only treat it as
                        # definitely happening when the guard is statically
                        # true. A statically-false guard means it never
                        # happens (env/local_env must stay untouched, not be
                        # overwritten with the dead branch's value); an
                        # unresolvable guard means it might or might not
                        # happen at runtime, so the name must be invalidated
                        # rather than assumed either unconditionally written
                        # or unconditionally skipped.
                        if cond_val is None:
                            local_env.pop(lhs_name.upper(), None)
                            killed.add(lhs_name.upper())
                            env.pop(lhs_name.upper(), None)
                            _substitute(rhs_toks, merged, edits)
                        elif _is_false_const(cond_val):
                            _substitute(rhs_toks, merged, edits)
                        else:
                            _apply_inblock_assignment(lhs_name, rhs_toks, env, killed, local_env, in_loop, edits)
                else:
                    _kill_call_stmt(stmt_part, killed, env, edits,
                                     None if in_loop else local_env)
            elif _is_assignment(ctoks):
                lhs_name, rhs_toks = _split_assignment(ctoks)
                if lhs_name:
                    _apply_inblock_assignment(lhs_name, rhs_toks, env, killed, local_env, in_loop, edits)
            elif kw not in _DECL_KEYWORDS:
                _kill_call_stmt(ctoks, killed, env, edits,
                                 None if in_loop else local_env)
            continue

        # --- Top-level assignment: [Set|Dim] name = expr  OR  name = expr ---
        if _is_assignment(ctoks):
            lhs_name, rhs_toks = _split_assignment(ctoks)
            if lhs_name:
                run_end = _try_collapse_self_append_run(
                    src, stmts, stmt_i, lhs_name, rhs_toks, env, killed, edits,
                    name_counts, budget)
                if run_end is not None:
                    skip_until = run_end
                else:
                    _apply_toplevel_assignment(lhs_name, rhs_toks, env, killed, edits,
                                                name_counts, budget)
            continue

        # Top-level non-assignment: same single-line-If concern as above —
        # split at the top-level THEN before substituting.
        then_idx = _find_top_level_then(ctoks) if kw == 'IF' else None
        if then_idx is not None:
            cond_sub = _substitute(ctoks[1:then_idx], env, edits)
            cond_val = resolve_const(cond_sub, env)
            stmt_part = ctoks[then_idx + 1:]
            if _is_assignment(stmt_part):
                lhs_name, rhs_toks = _split_assignment(stmt_part)
                if lhs_name:
                    # See the in-block twin of this branch above: the write
                    # only definitely happens when the guard is statically
                    # true; false means it never happens (leave env
                    # untouched); unresolvable means it might, so the name
                    # must be invalidated rather than assumed either way.
                    if cond_val is None:
                        killed.add(lhs_name.upper())
                        env.pop(lhs_name.upper(), None)
                        _substitute(rhs_toks, env, edits)
                    elif _is_false_const(cond_val):
                        _substitute(rhs_toks, env, edits)
                    else:
                        _apply_toplevel_assignment(lhs_name, rhs_toks, env, killed, edits,
                                                    name_counts, budget)
            else:
                _kill_call_stmt(stmt_part, killed, env, edits)
        elif kw not in _DECL_KEYWORDS:
            _kill_call_stmt(ctoks, killed, env, edits)

    if not edits:
        return src, 0, 0
    new_src = apply_edits(src, edits)
    return new_src, len(edits), len(edits)


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


def _apply_toplevel_assignment(lhs_name: str, rhs_toks: list, env: dict,
                                killed: set, edits: list,
                                name_counts: dict[str, int] | None = None,
                                budget: '_SubstitutionBudget | None' = None) -> None:
    """Shared bookkeeping for a recognised top-level 'name = rhs' —
    whether it's the whole statement or the tail of a single-line
    'If cond Then name = rhs'."""
    lhs_up = lhs_name.upper()
    # Self-append accumulator: VBScript's uninitialized Variant is Empty,
    # which coerces to "" in a string expression.  When this is provably
    # the first assignment to the name AND the RHS self-references it as an
    # operand of `&` (e.g. X = X & "chunk"), seed it as "" so the whole
    # chain folds. Restricted to `&` because that operator alone guarantees
    # string context — see _is_string_self_append.
    sub_env = env
    if (lhs_up not in env and lhs_up not in killed
            and _is_string_self_append(lhs_up, rhs_toks)):
        sub_env = dict(env)
        sub_env[lhs_up] = ''
    rhs_toks_sub = _substitute(rhs_toks, sub_env, edits)
    if lhs_up not in killed:
        val = resolve_const(rhs_toks_sub, sub_env)
        if val is None:
            # Not resolvable the normal way — but a decoy seed like
            # `accum = someNeverAssignedName` is common: obfuscators seed
            # an accumulator from a bare name that is never assigned
            # anywhere in the file specifically to defeat a naive "first
            # assignment" seeding heuristic. VBScript's implicit,
            # never-assigned Variant deterministically reads as Empty
            # ("" in a string context) — not a guess, just the language's
            # own default — so this is safe to fold exactly like the
            # self-append seeding above.
            val = _resolve_bare_undeclared(rhs_toks_sub, name_counts)
        # Subtract 1 for this assignment's own LHS occurrence — it's a
        # write, not a future substitution site. (A separate `Dim`
        # declaration elsewhere for the same name, if any, still counts as
        # a phantom read here — a known, disclosed conservatism: excluding
        # it too would need this function to see the raw source, not just
        # name_counts, which isn't worth the extra plumbing for what's a
        # heuristic safety net, not a hard guarantee.)
        reads = max(0, (name_counts or {}).get(lhs_up, 1) - 1)
        weight = len(val) * reads if isinstance(val, str) else 0
        # >= 2 self-references in the ORIGINAL (pre-substitution) RHS means
        # this evaluation multiplies the value (x = x & x doubles) — refuse
        # unconditionally, no size involved; see _count_self_refs. The
        # budget then catches what structure can't see (growth spread
        # across distinct names, or via a compounding call).
        if (val is not None and _count_self_refs(lhs_up, rhs_toks) < 2
                and (budget is None or budget.charge(weight))):
            env[lhs_up] = val
        else:
            killed.add(lhs_up)
            env.pop(lhs_up, None)


def _next_code_stmt(stmts: list, j: int) -> int | None:
    """Index of the next statement at or after *j* whose code_tokens() is
    non-empty, or None if none remains. A `\\r\\r\\n` line ending tokenizes
    as more than one NEWLINE (see the tokenizer's CRCRLF handling), so
    split_statements yields extra code-less spans between real statements —
    skipping them here lets a self-append run scan treat consecutive *code*
    statements as adjacent regardless of how many such spans separate them."""
    while j < len(stmts) and not stmts[j].code_tokens():
        j += 1
    return j if j < len(stmts) else None


def _resolve_seed_value(lhs_up: str, rhs_toks: list, env: dict, killed: set,
                         name_counts: dict[str, int] | None) -> Const | None:
    """Read-only mirror of the value-resolution half of
    _apply_toplevel_assignment (self-append empty-seed injection, then
    resolve_const, then the _resolve_bare_undeclared decoy-seed fallback) —
    used to speculatively resolve a statement's value while scanning for a
    self-append run, without emitting any edits. Deliberately duplicated
    rather than shared, so _apply_toplevel_assignment's own tested code path
    is never touched by this addition; keep the two in sync.

    Unlike _apply_toplevel_assignment, this never calls _substitute: that
    call's only effect beyond producing edits is to hand resolve_const a
    token list with reads already replaced by literals, which is redundant
    once resolve_const already consults the very same env for identifier
    lookups (see resolver.py's _atom: `self._env.get(name.upper())`) —
    passing the original tokens plus env resolves to the same value."""
    sub_env = env
    if (lhs_up not in env and lhs_up not in killed
            and _is_string_self_append(lhs_up, rhs_toks)):
        sub_env = dict(env)
        sub_env[lhs_up] = ''
    val = resolve_const(rhs_toks, sub_env)
    if val is None:
        val = _resolve_bare_undeclared(rhs_toks, name_counts)
    return val


def _try_collapse_self_append_run(src: str, stmts: list, stmt_i: int,
                                   lhs_name: str, rhs_toks: list,
                                   env: dict, killed: set, edits: list,
                                   name_counts: dict[str, int] | None,
                                   budget: '_SubstitutionBudget | None' = None) -> int | None:
    """If the top-level assignment at stmts[stmt_i] seeds a self-append
    accumulator chain — X = <const>, then one or more X = X & <const> links
    — resolve the whole run and replace it with a single edit
    'X = "<final literal>"' instead of substituting X's ever-growing value
    into every link individually (which is what makes total edit output
    O(chain_length^2) — see the growth-control design note above).

    Returns the statement index to resume the caller's loop from (the first
    statement after the collapsed run) on success, or None if stmts[stmt_i]
    is not a self-append seed — the caller should then fall back to the
    normal _apply_toplevel_assignment path for this one statement, exactly
    as if this function didn't exist."""
    lhs_up = lhs_name.upper()
    if lhs_up in killed:
        return None

    # Cheap structural pre-check: is the next *code* statement even shaped
    # like a self-append link to this name? Bail before doing any constant
    # resolution — keeps the added cost for every ordinary (non-chain)
    # assignment down to a handful of token comparisons.
    nxt = _next_code_stmt(stmts, stmt_i + 1)
    if nxt is None:
        return None
    nxt_ctoks = stmts[nxt].code_tokens()
    if not _is_assignment(nxt_ctoks):
        return None
    nxt_name, nxt_rhs = _split_assignment(nxt_ctoks)
    if not nxt_name or nxt_name.upper() != lhs_up:
        return None
    if not _is_string_self_append(lhs_up, nxt_rhs):
        return None

    cur_val = _resolve_seed_value(lhs_up, rhs_toks, env, killed, name_counts)
    if not isinstance(cur_val, str):
        return None

    run_links: list[int] = []
    j = nxt
    while j is not None:
        ctoks = stmts[j].code_tokens()
        if not _is_assignment(ctoks):
            break
        name, rhs = _split_assignment(ctoks)
        if not name or name.upper() != lhs_up:
            break
        if not _is_string_self_append(lhs_up, rhs):
            break
        new_val = resolve_const(rhs, {**env, lhs_up: cur_val})
        if not isinstance(new_val, str) or len(new_val) > _MAX_COLLAPSED_VALUE_LEN:
            break
        cur_val = new_val
        run_links.append(j)
        j = _next_code_stmt(stmts, j + 1)

    if len(run_links) < _MIN_APPEND_LINKS_TO_COLLAPSE:
        return None

    last_i = run_links[-1]
    _emit_collapsed_run(src, stmts, stmt_i, last_i, lhs_name, cur_val, edits)
    # Same budget check as _apply_toplevel_assignment: the collapsed run
    # itself is always emitted as one edit regardless (above) — this only
    # decides whether it's worth keeping in env for *other*, separate read
    # sites elsewhere in the file. name_counts[lhs_up] would count the
    # chain's own construction (2 occurrences per link) as "reads", which
    # it isn't — _external_occurrences excludes them.
    consumed_text = src[stmts[stmt_i].start:stmts[last_i].end]
    reads = _external_occurrences(name_counts, lhs_up, consumed_text)
    weight = len(cur_val) * reads if isinstance(cur_val, str) else 0
    # The chain's own links are self-referential by construction and
    # already safely bounded by _MAX_COLLAPSED_VALUE_LEN above (that's
    # what collapsing means) — the structural check here is on the SEED's
    # original RHS instead, same as _apply_toplevel_assignment, since a
    # seed that already multiplies before any chain even starts (e.g. a
    # literal `x = x & x` as the very first statement) shouldn't be
    # retained regardless of the collapsed result's size.
    if (_count_self_refs(lhs_up, rhs_toks) < 2
            and (budget is None or budget.charge(weight))):
        env[lhs_up] = cur_val
    else:
        killed.add(lhs_up)
        env.pop(lhs_up, None)
    return last_i + 1


def _emit_collapsed_run(src: str, stmts: list, first_i: int, last_i: int,
                         lhs_name: str, final_val: Const, edits: list) -> None:
    """Replace the statement span [first_i, last_i] (inclusive) — a
    resolved self-append run — with a single 'name = "<literal>"'
    statement, preserving the first statement's leading indentation and the
    last statement's trailing line terminator. Any code-less statement
    absorbed inside the span (see _next_code_stmt) is silently dropped along
    with it — the same convention vbs_fold_array_join_loops.py already uses
    when collapsing a multi-statement span into one literal assignment."""
    first_stmt = stmts[first_i]
    last_stmt = stmts[last_i]
    indent = _line_indent(src, first_stmt.start)
    last_toks = last_stmt.tokens
    terminator = (src[last_toks[-1].start:last_toks[-1].end]
                  if last_toks and last_toks[-1].kind == TokenKind.NEWLINE else '')
    new_stmt = f'{indent}{lhs_name} = {_const_to_literal(final_val)}{terminator}'
    edits.append((first_stmt.start, last_stmt.end, new_stmt))


def _line_indent(src: str, offset: int) -> str:
    """Leading whitespace of the line containing *offset*, or '' if
    anything non-whitespace precedes it on that line. Same helper already
    used by vbs_fold_array_join_loops.py for the identical span-collapse
    convention."""
    line_start = src.rfind('\n', 0, offset) + 1
    prefix = src[line_start:offset]
    return prefix if prefix.strip() == '' else ''


def _apply_inblock_assignment(lhs_name: str, rhs_toks: list, env: dict, killed: set,
                               local_env: dict, in_loop: bool, edits: list) -> None:
    """Shared bookkeeping for a recognised in-block 'name = rhs' —
    whether it's the whole statement or the tail of a single-line
    'If cond Then name = rhs' inside an open block."""
    lhs_up = lhs_name.upper()
    # Clear any local knowledge of this name *before* substituting RHS so a
    # self-referencing update reads only its pre-this-statement value.
    local_env.pop(lhs_up, None)
    merged = env if in_loop else {**env, **local_env}
    # Outer/global env never retains a block-local write — matches the
    # original fully-conservative behaviour.
    killed.add(lhs_up)
    env.pop(lhs_up, None)
    rhs_toks_sub = _substitute(rhs_toks, merged, edits)
    if not in_loop:
        # Straight-line, non-looping block: safe to track this as a local
        # constant for later statements in the same run (a value computed
        # here executes exactly once before any subsequent read of it).
        val = resolve_const(rhs_toks_sub, merged)
        if val is not None and not _exceeds_cap(val):
            local_env[lhs_up] = val
    # Inside a loop: never track (a value derived from one iteration's
    # inputs is not valid for the next).


def _kill_assignments(ctoks: list, env: dict, killed: set) -> None:
    """Heuristically kill any variable that appears as LHS of = in ctoks."""
    for idx, t in enumerate(ctoks):
        if (t.kind == TokenKind.IDENT
                and idx + 1 < len(ctoks)
                and ctoks[idx+1].kind == TokenKind.OP
                and ctoks[idx+1].value == '='):
            name = t.value.upper()
            killed.add(name)
            env.pop(name, None)


def _kill_loop_body_assignments(stmts: list, start_i: int, end_i: int,
                                 env: dict, killed: set) -> None:
    """Kill every name assigned anywhere between statement indices
    [start_i, end_i) — a loop's full body, any nesting depth — reusing the
    same 'IDENT immediately followed by =' shape _kill_assignments already
    uses for block headers. Deliberately a token-shape heuristic rather than
    a precise assignment parse: it will also kill a name that only appears
    in a comparison (e.g. the X in 'If X = 5 Then' inside the loop), but
    over-killing here only costs a missed fold, never produces a wrong
    substitution — the same trade-off the rest of this tool already makes."""
    for j in range(start_i, end_i):
        _kill_assignments(stmts[j].code_tokens(), env, killed)


def _resolve_bare_undeclared(rhs_toks: list, name_counts: dict[str, int] | None) -> Const | None:
    """Fallback for an RHS resolve_const couldn't handle: the decoy-seed
    idiom `accum = someNameThatIsNeverBound`, used to defeat a naive "first
    assignment" seeding heuristic.

    VBScript without Option Explicit auto-declares any unbound name as an
    Empty Variant, which reads as "" in a string context. That much is the
    language's own defined behaviour, not an assumption about any sample, so
    the entire risk sits in *proving* the name is never bound, by any route.
    This fires only when the name occurs **exactly once** in the whole raw
    source: the read being folded, and nothing else anywhere.

    That single criterion rules out every binding route at once, because each
    of them needs a second occurrence somewhere in the text:
      - an assignment / Dim / Const / ReDim / Class field (its own LHS)
      - being passed to a Sub or Function, whose parameters are ByRef by
        default and may therefore write straight back through the argument
      - a For / For Each loop variable, or a procedure name or parameter
      - a name assigned from inside an Execute / Eval string payload
        (counted, because occurrences are tallied over raw source text with
        string and comment content included)

    Reserved words, builtins, and the zero-argument intrinsics callable with
    no parentheses (Rnd, Timer, Now, Err, ScriptEngine, ...) are excluded
    outright: those are calls, not unbound variables, and a call can legally
    occur exactly once.
    """
    if name_counts is None:
        return None
    if len(rhs_toks) != 1:
        return None
    t = rhs_toks[0]
    if t.kind != TokenKind.IDENT:
        return None
    up = t.upper
    if up in _VBS_RESERVED or up in _VBS_GLOBAL_NAMES:
        return None
    if up.startswith('VB'):
        return None                      # vbCrLf, vbTextCompare, ... intrinsics
    if name_counts.get(up, 0) != 1:
        return None
    return ''


_WORD_RE = re.compile(r'[A-Za-z_]\w*')

# Names that may appear bare - no parentheses, no arguments - and are thus a
# call or an intrinsic object rather than an unbound variable. `x = Rnd`
# must never fold to `x = ""`.
_VBS_GLOBAL_NAMES = frozenset("""
RND TIMER NOW DATE TIME ERR ME
SCRIPTENGINE SCRIPTENGINEMAJORVERSION SCRIPTENGINEMINORVERSION
SCRIPTENGINEBUILDVERSION GETLOCALE SETLOCALE
WSCRIPT WSH APPLICATION DEBUG
""".split())

_DYNAMIC_DISPATCH_RE = re.compile(
    r'(?i)\b(?:Execute|ExecuteGlobal|Eval|CallByName|GetRef)\b')

_OPTION_EXPLICIT_RE = re.compile(r'(?im)^[ \t]*Option\s+Explicit\b')


def _build_name_counts(src: str) -> dict[str, int] | None:
    """Word-boundary occurrence tally over the *raw* source - string and
    comment text included, deliberately: a name mentioned only inside an
    Execute payload is still a possible binding site.

    Returns None, disabling the Empty fallback entirely, when the file uses
    Option Explicit (an unbound reference is a compile error there, so the
    fold could never describe a real execution) or contains any dynamic
    dispatch construct, which can bind names by a route no static tally
    covers. Same dynamic-dispatch guard vbs_remove_deadcode already applies
    before deleting a name it believes is dead.
    """
    if _OPTION_EXPLICIT_RE.search(src) or _DYNAMIC_DISPATCH_RE.search(src):
        return None
    counts: dict[str, int] = {}
    for m in _WORD_RE.finditer(src):
        k = m.group(0).upper()
        counts[k] = counts.get(k, 0) + 1
    return counts


def _external_occurrences(name_counts: dict[str, int] | None, lhs_up: str,
                           consumed_text: str) -> int:
    """name_counts[lhs_up] counts every mention of the name anywhere in the
    whole file — for a self-append chain that includes the chain's own
    construction (2 occurrences per link: the LHS and the self-referencing
    operand), which isn't a future read site once the chain is collapsed
    to one edit. Subtract occurrences within *consumed_text* (the raw
    source span the chain actually consumed) to get a much closer estimate
    of genuine other-site reads."""
    if not name_counts:
        return 0
    consumed = sum(1 for m in _WORD_RE.finditer(consumed_text)
                   if m.group(0).upper() == lhs_up)
    return max(0, name_counts.get(lhs_up, 0) - consumed)


def _leading_skip(ctoks: list) -> int:
    """Number of leading modifier tokens to skip before the declared name,
    e.g. 'Dim x', 'Set x', 'Const x', or 'Public Const x' / 'Private Const x'."""
    start = 0
    if (ctoks[start].kind == TokenKind.IDENT and ctoks[start].upper in ('PUBLIC', 'PRIVATE')
            and start + 1 < len(ctoks) and ctoks[start + 1].kind == TokenKind.IDENT
            and ctoks[start + 1].upper == 'CONST'):
        start += 1
    if start < len(ctoks) and ctoks[start].kind == TokenKind.IDENT and ctoks[start].upper in ('DIM', 'SET', 'LET', 'CONST'):
        start += 1
    return start


def _is_assignment(ctoks: list) -> bool:
    """Return True if this looks like a simple top-level assignment."""
    if not ctoks:
        return False
    start = _leading_skip(ctoks)
    if start >= len(ctoks):
        return False
    # Next should be IDENT = ...
    if ctoks[start].kind != TokenKind.IDENT:
        return False
    if start + 1 >= len(ctoks):
        return False
    # The token after the name must be '='
    return ctoks[start + 1].kind == TokenKind.OP and ctoks[start + 1].value == '='


def _split_assignment(ctoks: list) -> tuple[str | None, list]:
    """Return (lhs_name, rhs_tokens) for a simple assignment, or (None, [])."""
    start = _leading_skip(ctoks)
    if start + 2 > len(ctoks):
        return None, []
    lhs = ctoks[start]
    eq  = ctoks[start + 1]
    if lhs.kind != TokenKind.IDENT or eq.value != '=':
        return None, []
    return lhs.value, ctoks[start + 2:]


def _substitute(ctoks: list, env: dict, edits: list) -> list:
    """Replace bare IDENT tokens whose name is in env with their constant literal.
    Appends (start, end, replacement) to edits. Returns a copy of ctoks with
    substituted values (as STRING/NUMBER tokens) for re-resolution.

    An IDENT immediately preceded by a '.' is a member name (`obj.Count`),
    not a variable reference — even when some unrelated variable of the same
    name is tracked in env, it must never be replaced."""
    result = []
    for i, t in enumerate(ctoks):
        is_member_name = (i > 0 and ctoks[i - 1].kind == TokenKind.OP
                           and ctoks[i - 1].value == '.')
        if (not is_member_name
                and t.kind == TokenKind.IDENT
                and t.upper not in _VBS_RESERVED
                and t.upper in env):
            val = env[t.upper]
            rep = _const_to_literal(val)
            edits.append((t.start, t.end, rep))
            # Return a fake token list entry for the resolver
            from vbsdeoblib.tokenizer import VbsToken
            result.append(VbsToken(
                kind=TokenKind.STRING if isinstance(val, str) else TokenKind.NUMBER,
                value=rep,
                start=t.start,
                end=t.end,
            ))
        else:
            result.append(t)
    return result


_OPEN_BRACKETS = ('(', '[')
_CLOSE_BRACKETS = (')', ']')


def _split_top_level(toks: list, sep: str) -> list[list]:
    """Split *toks* on occurrences of the single-character operator *sep*
    that sit at bracket depth 0 (paren/`[` nesting tracked, so a comma or
    other separator inside a nested call or subscript is not a split
    point)."""
    parts: list[list] = []
    cur: list = []
    depth = 0
    for t in toks:
        if t.kind == TokenKind.OP and t.value in _OPEN_BRACKETS:
            depth += 1
        elif t.kind == TokenKind.OP and t.value in _CLOSE_BRACKETS:
            depth -= 1
        elif depth == 0 and t.kind == TokenKind.OP and t.value == sep:
            parts.append(cur)
            cur = []
            continue
        cur.append(t)
    parts.append(cur)
    return parts


def _spans_one_paren_group(toks: list) -> bool:
    """True when toks[0] is '(' and its matching ')' is toks[-1] — i.e. toks
    is exactly one parenthesized group, not e.g. `(a) & (b)` or `(a), (b)`."""
    if len(toks) < 2 or toks[0].kind != TokenKind.OP or toks[0].value != '(':
        return False
    depth = 0
    for i, t in enumerate(toks):
        if t.kind == TokenKind.OP and t.value in _OPEN_BRACKETS:
            depth += 1
        elif t.kind == TokenKind.OP and t.value in _CLOSE_BRACKETS:
            depth -= 1
            if depth == 0:
                return i == len(toks) - 1
    return False


def _classify_call_stmt(code: list) -> tuple[set, set]:
    """Classify every position in a non-assignment statement's code tokens
    as a possible ByRef target or a safe-to-substitute read.

    VBScript can only bind a procedure argument ByRef when the argument
    expression IS a bare variable (or array-element) reference — the callee
    needs an address, which exists only for a name the caller already has
    storage for. An identifier that is merely an operand inside a larger
    expression argument (`Execute w & " s"`) is evaluated to a value before
    the call: nothing can ever be written back through it, so it is exactly
    as safe to substitute as an assignment RHS read.

    Returns (byref_indices, read_indices), sets of indices into *code*.
    An index in neither set (a member name after '.', the callee reference,
    the '=' operator itself) is left untouched: neither substituted nor
    killed.
    """
    n = len(code)
    byref: set = set()
    ignore: set = set()

    for i, t in enumerate(code):
        if (t.kind == TokenKind.IDENT and i > 0
                and code[i - 1].kind == TokenKind.OP and code[i - 1].value == '.'):
            ignore.add(i)   # member name, not a variable reference

    # Property/element write (`obj.P = v`, `arr(i) = v`): not recognised by
    # _is_assignment (which requires a bare IDENT immediately before '='),
    # but a top-level '=' still marks a write whose LHS base behaves like a
    # ByRef target — everything else (subscript, RHS) is a plain read.
    depth = 0
    eq_i = None
    for i, t in enumerate(code):
        if t.kind == TokenKind.OP and t.value in _OPEN_BRACKETS:
            depth += 1
        elif t.kind == TokenKind.OP and t.value in _CLOSE_BRACKETS:
            depth -= 1
        elif depth == 0 and t.kind == TokenKind.OP and t.value == '=':
            eq_i = i
            break
    if eq_i is not None:
        for i in range(eq_i):
            if i in ignore:
                continue
            if code[i].kind == TokenKind.IDENT:
                byref.add(i)
                break
        reads = set(range(n)) - byref - ignore - {eq_i}
        return byref, reads

    # Call statement: skip a leading CALL, then the callee reference (IDENT,
    # plus any '.IDENT' member chain) — never a read or a kill target.
    i = 0
    has_call = bool(code) and code[0].kind == TokenKind.IDENT and code[0].upper == 'CALL'
    if has_call:
        ignore.add(0)
        i = 1
    if i < n and code[i].kind == TokenKind.IDENT:
        ignore.add(i)
        i += 1
        while (i + 1 < n and code[i].kind == TokenKind.OP and code[i].value == '.'
               and code[i + 1].kind == TokenKind.IDENT):
            ignore.add(i + 1)
            i += 2

    region = code[i:]
    if region and _spans_one_paren_group(region):
        if has_call:
            # Call Foo(x, y): the parens ARE the argument list.
            groups = _split_top_level(region[1:-1], ',')
            base = i + 1
        else:
            # Foo (x): bare parens force ByVal evaluation of the whole
            # expression before the call — no ByRef target is possible.
            groups, base = [], None
    else:
        groups, base = _split_top_level(region, ','), i

    if base is not None:
        pos = base
        for g in groups:
            if len(g) == 1 and g[0].kind == TokenKind.IDENT and g[0].upper not in _VBS_RESERVED:
                byref.add(pos)
            elif (len(g) >= 3 and g[0].kind == TokenKind.IDENT
                  and g[0].upper not in _VBS_RESERVED and _spans_one_paren_group(g[1:])):
                byref.add(pos)   # array-element arg: base is ByRef, subscript is a read
            pos += len(g) + 1

    reads = set(range(n)) - byref - ignore
    return byref, reads


def _kill_call_stmt(ctoks: list, killed: set, real_env: dict, edits: list,
                     local_env: dict | None = None) -> None:
    """A non-assignment statement (a Sub/Function/Method call, a bare
    expression statement, or a property/array-element write) is walked
    position by position via _classify_call_stmt: a possible ByRef argument
    is killed exactly as before — never substituted, because VBScript
    passes Sub/Function/Method arguments ByRef by default, so the callee may
    overwrite it (e.g. a decoder Sub's `f encoded, out` writes the decoded
    value back through `out`). Every other identifier position is a value
    already computed before the call ever reaches the callee — it can never
    be a ByRef target — so it is substituted exactly like an assignment RHS
    read, and never killed.

    Substituting a read and killing a target are mutually exclusive by
    construction, which is what keeps this statement's effect stable across
    passes: resolve_const re-derives env from scratch every pass by
    re-tokenizing whatever text the previous pass produced, so a position
    that was substituted (its identifier is now gone from the text) must
    never also need a kill to remain correct on the next pass, and a
    position that is killed must never be substituted (that would erase the
    only textual trace of the kill). See tests/test_propagate_constants.py's
    ByRef regression tests for the failure this guards against.
    """
    if not ctoks:
        return
    byref, reads = _classify_call_stmt(ctoks)
    merged = real_env if local_env is None else {**real_env, **local_env}
    for i in sorted(reads):
        t = ctoks[i]
        if t.kind == TokenKind.IDENT and t.upper not in _VBS_RESERVED and t.upper in merged:
            _substitute([t], merged, edits)
    for i in byref:
        name = ctoks[i].upper
        killed.add(name)
        real_env.pop(name, None)
        if local_env is not None:
            local_env.pop(name, None)


def _is_string_self_append(lhs_upper: str, rhs_toks: list) -> bool:
    """True when *lhs_upper* appears in *rhs_toks* as a direct operand of a
    `&` operator — i.e. unambiguously in string-concatenation context.

    Seeding a not-yet-assigned accumulator as "" is only valid there.
    VBScript's `&` always coerces both operands to string, so an unset
    Variant (Empty) genuinely reads as "". `+` is ambiguous: it *adds* when
    either operand is numeric, and `Empty + 2` is 2 whereas `"" + 2` is a
    runtime type mismatch — so a `+` self-append is never seeded, even
    though `+` does concatenate when both operands happen to be strings.
    """
    code = [t for t in rhs_toks
            if t.kind not in (TokenKind.WS, TokenKind.COMMENT,
                              TokenKind.NEWLINE, TokenKind.LINECONT)]
    for i, t in enumerate(code):
        if not (t.kind == TokenKind.IDENT and t.upper == lhs_upper):
            continue
        prev = code[i - 1] if i > 0 else None
        nxt = code[i + 1] if i + 1 < len(code) else None
        for nb in (prev, nxt):
            if nb is not None and nb.kind == TokenKind.OP and nb.value == '&':
                return True
    return False


# VBScript keywords and built-in names that should never be substituted.
_VBS_RESERVED = frozenset("""
AND BYREF BYVAL CALL CASE CLASS CONST DIM DO EACH ELSE ELSEIF END ERASE ERROR
EXECUTE EXECUTEGLOBAL EXIT FALSE FOR FUNCTION GET GOTO IF IN IS LET LOOP MOD NEW
NEXT NOT NOTHING NULL OBJECT ON OPTION OR PRESERVE PRIVATE PUBLIC RANDOMIZE REDIM
REM RESUME SELECT SET STEP STOP SUB THEN TO TRUE UNTIL WEND WHILE WITH XOR
CHR ASC LEN UCASE LCASE TRIM LTRIM RTRIM CSTR CINT CDBL CBOOL MID LEFT RIGHT
REPLACE INSTR INSTRREV STRREVERSE SPACE STRING HEX OCT ABS INT FIX SQR
CREATEOBJECT GETOBJECT WSCRIPT MSGBOX INPUTBOX NOW DATE TIME TIMER
""".split())


if __name__ == '__main__':
    run_tool(run, description='Flow-sensitive constant propagation across variable assignments')
