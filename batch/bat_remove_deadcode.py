#!/usr/bin/env python3
"""Removes dead stores and unreachable code. The Batch analogue of
PsRemove-DeadCode / vbs_remove_deadcode.

Two independent analyses:
  1. Reachability: a worklist walk over the flattened statement list and the
     goto/call graph (batdeoblib.cfg), starting from statement 0. An
     unconditional `goto LABEL` or `exit [/b ...]` (the statement's own
     first word, not embedded in an `if`) does NOT fall through to the next
     statement; a conditional one (`if COND goto LABEL`) reaches BOTH the
     target and the fallthrough. A never-reached label is, by construction
     of this same walk, simply never added to the reachable set -- orphaned
     labels and the dead code after an unconditional jump are one and the
     same analysis, not two. A computed/non-literal goto target (the target
     couldn't be resolved to a known label -- see cfg.py) makes the WHOLE
     reachability result unreliable, so this pass refuses entirely rather
     than risk deleting code a real jump might still reach.
  2. Dead stores: a `set`/`set /a` target that is never read by any OTHER
     reachable statement anywhere in the script is removed. This is a
     fixpoint -- removing a dead store can make whatever IT read dead too
     (e.g. a store that fed only that dead store), so the read-count map is
     decremented and rechecked until stable, the same cascading behavior
     PsRemove-DeadCode/vbs_remove_deadcode document.

Deliberately conservative, matching this toolkit's stance across the board:
a statement whose target name is also read via `%~...` positional-arg
modifiers, referenced only inside an unresolvable condition, or written
inside a block this pass can't prove dead is left alone. `--aggressive`
additionally removes a `for` loop whose body is empty after other passes
have hollowed it out (a common leftover, not a new class of judgment call).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten, Statement, Block
from batdeoblib.cfg import build_cfg


def _first_word(s: Statement) -> str | None:
    ct = s.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT:
        return None
    return ct[0].value.lstrip('@').upper()


def _is_if_stmt(s: Statement) -> bool:
    return _first_word(s) == 'IF'


def _compute_reachable(cfg) -> set[int] | None:
    stmts = cfg.statements
    n = len(stmts)
    goto_by_index = {g.index: g for g in cfg.gotos}
    for g in cfg.gotos:
        if g.target is None:
            return None   # unresolvable jump target -- refuse the whole analysis

    reachable: set[int] = set()
    worklist = [0] if n else []
    while worklist:
        idx = worklist.pop()
        if idx in reachable or idx >= n or idx < 0:
            continue
        reachable.add(idx)
        stmt = stmts[idx]
        word = _first_word(stmt)
        g = goto_by_index.get(idx)
        unconditional_jump = g is not None and word in ('GOTO',)
        unconditional_exit = word == 'EXIT'
        conditional_jump = g is not None and word != 'GOTO'   # embedded in `if ...`

        if g is not None and g.target != 'EOF':
            worklist.append(cfg.label_index(g.target))
        if unconditional_jump or unconditional_exit:
            continue   # no fallthrough
        if idx + 1 < n:
            worklist.append(idx + 1)
    return reachable


def remove_deadcode(text: str, *, aggressive: bool = False, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    stmts = flatten(tree)
    cfg = build_cfg(tree)

    reachable = _compute_reachable(cfg)
    if reachable is None:
        return text, {'changed': 0, 'reason': 'unresolvable goto target -- refused'}

    edits: list[tuple[int, int, str]] = []
    changed = 0
    unreachable_removed = 0
    dead_stores_removed = 0

    def blank(s: Statement):
        body = [t for t in s.tokens if t.kind != TokenKind.NEWLINE]
        if body:
            edits.append((body[0].start, body[-1].end, ''))

    removed_ids: set[int] = set()
    for idx, s in enumerate(stmts):
        if idx not in reachable:
            blank(s)
            removed_ids.add(id(s))
            changed += 1
            unreachable_removed += 1

    # -- dead-store fixpoint over the REACHABLE, non-removed statements --
    live_stmts = [s for i, s in enumerate(stmts) if i in reachable]

    def write_target(s: Statement) -> str | None:
        w = _first_word(s)
        if w != 'SET':
            return None
        ct = s.code_tokens()
        rest = ct[1:]
        if rest and rest[0].kind == TokenKind.TEXT and rest[0].value.upper() == '/P':
            return None   # reads user input -- never a dead store
        if rest and rest[0].kind == TokenKind.TEXT and rest[0].value.upper() == '/A':
            body = rest[1:]
            txt = ''.join(t.value for t in body).strip('"')
            if ',' in txt:
                return None   # multi-assignment -- out of scope for this simple pass
            if '=' not in txt:
                return None
            name = txt.split('=', 1)[0]
            for op in ('<<', '>>', '+', '-', '*', '/', '%', '&', '|', '^'):
                if name.endswith(op):
                    return None   # compound assignment reads its own target -- never dead
            return name.strip().upper()
        wraps = rest and rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
        body = rest[1:-1] if wraps else rest
        for t in body:
            if t.kind == TokenKind.TEXT and '=' in t.value:
                return t.value.split('=', 1)[0].strip().upper()
        return None

    def reads_in(s: Statement) -> list[str]:
        out = []
        for t in s.tokens:
            if t.kind in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                out.append((t.inner or '').split(':', 1)[0].upper())
        # set /a's bare-identifier reads (no %/! needed) -- best-effort scan
        if _first_word(s) == 'SET':
            ct = s.code_tokens()
            rest = ct[1:]
            if rest and rest[0].kind == TokenKind.TEXT and rest[0].value.upper() == '/A':
                import re as _re
                txt = ''.join(t.value for t in rest[1:])
                for part in txt.strip('"').split(','):
                    if '=' not in part:
                        continue
                    _, expr = part.split('=', 1)
                    out.extend(m.upper() for m in _re.findall(r'[A-Za-z_]\w*', expr))
        return out

    # A variable name that shows up anywhere as plain quoted-string content
    # (not a %VAR%/!VAR! token -- those are already counted as real reads)
    # is conservatively treated as live, exactly like the VBS toolkit's
    # documented guard for a name reached via Get-Variable/dynamic dispatch:
    # a `-Command "...$env:XSPPBECR..."` argument passed to powershell is a
    # REAL runtime consumption of that variable (the child process inherits
    # the environment and reads it by name) even though nothing in the
    # batch script's own %/! syntax ever references it. Missing this would
    # make dead-store removal actively destructive -- deleting a variable
    # that unambiguously still gets used, just not through batch's own
    # expansion syntax.
    quoted_blob = '\n'.join(
        t.value for s in live_stmts for t in s.tokens
        if t.kind == TokenKind.TEXT and t.in_quotes
    ).upper()

    from collections import Counter
    read_counts: Counter = Counter()
    stmt_reads: dict[int, list[str]] = {}
    stmt_writes: dict[int, str | None] = {}
    protected_names: set[str] = set()
    for s in live_stmts:
        rs = reads_in(s)
        stmt_reads[id(s)] = rs
        target = write_target(s)
        stmt_writes[id(s)] = target
        for name in rs:
            read_counts[name] += 1
        if target and target in quoted_blob:
            protected_names.add(target)

    dead_ids: set[int] = set()
    progress = True
    while progress:
        progress = False
        for s in live_stmts:
            if id(s) in dead_ids or id(s) in removed_ids:
                continue
            target = stmt_writes[id(s)]
            if target is None:
                continue
            if target in protected_names:
                continue
            if read_counts[target] > 0:
                continue
            dead_ids.add(id(s))
            for name in stmt_reads[id(s)]:
                read_counts[name] -= 1
            progress = True

    for s in live_stmts:
        if id(s) in dead_ids:
            blank(s)
            changed += 1
            dead_stores_removed += 1

    if aggressive:
        def scan_empty_for(nodes: list):
            nonlocal changed
            i = 0
            while i < len(nodes):
                node = nodes[i]
                if (isinstance(node, Statement) and _first_word(node) == 'FOR'
                        and i + 3 < len(nodes) and isinstance(nodes[i + 1], Block)
                        and isinstance(nodes[i + 2], Statement)
                        and nodes[i + 2].code_tokens()
                        and nodes[i + 2].code_tokens()[0].value.upper() == 'DO'
                        and isinstance(nodes[i + 3], Block)):
                    body_stmts = [x for x in flatten(nodes[i + 3].body)
                                  if x.code_tokens() and id(x) not in removed_ids and id(x) not in dead_ids]
                    if not body_stmts:
                        edits.append((node.start, nodes[i + 3].end, ''))
                        changed += 1
                        i += 4
                        continue
                if isinstance(node, Block):
                    scan_empty_for(node.body)
                i += 1
        scan_empty_for(tree)

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {
        'changed': changed,
        'unreachable_removed': unreachable_removed,
        'dead_stores_removed': dead_stores_removed,
    }


if __name__ == '__main__':
    run_tool(remove_deadcode, description='Remove dead stores and unreachable code.')
