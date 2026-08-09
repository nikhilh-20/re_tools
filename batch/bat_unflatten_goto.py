#!/usr/bin/env python3
"""Straightens a chain of unconditional `goto`s that only exists to scramble
reading order -- the Batch analogue of PsUnflatten-Switch's control-flow-
flattening reconstruction, scoped to what's SAFELY provable without a full
control-flow-graph reordering engine.

Splices label L's body in place of an unconditional `goto L` (dropping both
the goto and L's now-redundant label) exactly when relocating L is provably
safe:
  - L is targeted by exactly ONE goto/call edge anywhere in the script (this
    one) -- cfg.py's edge count.
  - L is not also reachable by plain fall-through from whatever statement
    precedes it in the ORIGINAL source (moving it would silently change
    what that neighbor flows into).
  - The `goto` is unconditional -- the statement's own first word is
    literally GOTO, not embedded in an `if` (a branch has two real
    successors; this pass never tries to linearize through one).

The chain continues as far as it can: if L's own body ends in another such
single-use unconditional goto to M, M is spliced in too, and so on. A block
ending in a loop, a conditional branch, or a goto whose target has any other
incoming edge stops the chain right there -- left untouched, exactly the
same "prove every transition or leave it alone" contract
PsUnflatten-Switch uses for its dispatcher loops.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten, Statement
from batdeoblib.cfg import build_cfg


def _first_word(s: Statement) -> str | None:
    ct = s.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT:
        return None
    return ct[0].value.lstrip('@').upper()


def _label_name(s: Statement) -> str | None:
    ct = s.code_tokens()
    if len(ct) == 1 and ct[0].kind == TokenKind.LABEL:
        return (ct[0].inner or '').upper().split()[0] if (ct[0].inner or '').strip() else None
    return None


def _block_span(stmts: list[Statement], start_idx: int) -> int:
    """End index (exclusive) of the basic block starting at start_idx --
    runs until the next label or end of file."""
    i = start_idx + 1
    n = len(stmts)
    while i < n and _label_name(stmts[i]) is None:
        i += 1
    return i


def _stmt_span_text(text: str, stmts: list[Statement], lo: int, hi: int) -> tuple[int, int]:
    body = [t for s in stmts[lo:hi] for t in s.tokens]
    if not body:
        return (stmts[lo].start, stmts[lo].start)
    return body[0].start, body[-1].end


def unflatten_goto(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    stmts = flatten(tree)
    n = len(stmts)
    cfg = build_cfg(tree)

    goto_in_degree: dict[str, int] = {}
    for g in cfg.gotos:
        if g.target and g.target != 'EOF':
            goto_in_degree[g.target] = goto_in_degree.get(g.target, 0) + 1

    label_at: dict[int, str] = {}
    for i, s in enumerate(stmts):
        name = _label_name(s)
        if name:
            label_at[i] = name

    def falls_through_into(label_idx: int) -> bool:
        """True if the statement immediately before this label is reachable
        code that doesn't itself terminate/jump away (a real fallthrough
        predecessor -- relocating the label would orphan it)."""
        j = label_idx - 1
        while j >= 0 and not stmts[j].code_tokens():
            j -= 1   # skip blank/whitespace-only statements -- not real predecessors
        if j < 0:
            return False
        prev = stmts[j]
        w = _first_word(prev)
        if w == 'GOTO':
            return False
        if w == 'EXIT':
            return False
        return True

    edits: list[tuple[int, int, str]] = []
    changed = 0
    consumed: set[int] = set()   # statement indices already spliced elsewhere -- never reused as a splice source again

    # Scan block-by-block (label-delimited, block 0 = leading unlabeled region).
    block_starts = [0] + [idx for idx in label_at]
    block_starts = sorted(set(block_starts))

    for bstart in block_starts:
        if bstart in consumed:
            continue
        bend = _block_span(stmts, bstart)
        # find the last REAL (non-empty) statement in [bstart, bend)
        last_idx = None
        for k in range(bend - 1, bstart - 1, -1):
            if stmts[k].code_tokens():
                last_idx = k
                break
        if last_idx is None or _first_word(stmts[last_idx]) != 'GOTO':
            continue
        g = next((g for g in cfg.gotos if g.index == last_idx), None)
        if g is None or g.target is None or g.target == 'EOF':
            continue
        target = g.target
        if goto_in_degree.get(target, 0) != 1:
            continue
        target_idx = cfg.label_index(target)
        if target_idx is None or target_idx in consumed:
            continue
        if falls_through_into(target_idx):
            continue
        if target_idx == bstart:
            continue   # self-loop -- not a chain to straighten

        target_end = _block_span(stmts, target_idx)
        # Splice: delete the goto statement's own text (drop the jump),
        # delete the target label's body from its old location, and insert
        # the target's BODY (statements after the label itself -- the label
        # line is dropped, nothing else may still reference it) right after
        # where the goto was.
        goto_body = [t for t in stmts[last_idx].tokens if t.kind != TokenKind.NEWLINE]
        if not goto_body:
            continue
        insert_lo, insert_hi = _stmt_span_text(text, stmts, target_idx + 1, target_end)
        moved_text = text[insert_lo:insert_hi] if insert_hi > insert_lo else ''

        edits.append((goto_body[0].start, goto_body[-1].end, moved_text))
        old_lo, old_hi = _stmt_span_text(text, stmts, target_idx, target_end)
        edits.append((old_lo, old_hi, ''))

        consumed.update(range(target_idx, target_end))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(unflatten_goto, description='Splice single-use unconditional goto chains back into linear order.')
