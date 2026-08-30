"""Label table and goto/call graph for Batch scripts.

Batch has exactly one control-flow primitive worth modeling structurally:
labels (`:name`) as jump targets for `goto` and `call`. There is no
block-structured loop/if the way VBScript or PowerShell have (the `(...)`
groups in statements.py are lexical/expansion-scope constructs, not loop
nodes) -- `for` is the only real loop, and it is handled directly by
bat_fold_for_loops.py rather than through this module.

Label matching is cmd.exe's own: case-insensitive, and only the label's own
first "word" matters -- `:loop extra text` is exactly the same label as
`:loop`; `goto` targets are matched the same way, and a leading `:` on the
goto argument is optional and stripped.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .statements import Statement, Block, flatten
from .tokenizer import TokenKind


@dataclass
class LabelInfo:
    name: str            # normalized (uppercased) label name
    stmt: Statement       # the :label statement itself
    index: int            # position in the flattened statement list


@dataclass
class GotoEdge:
    stmt: Statement
    index: int
    target: str | None    # normalized label name, or None if not a literal target
    is_call: bool
    reason: str | None = None   # set when target couldn't be resolved statically


@dataclass
class ControlFlowGraph:
    statements: list[Statement]
    labels: dict[str, LabelInfo] = field(default_factory=dict)
    gotos: list[GotoEdge] = field(default_factory=list)

    def label_index(self, name: str) -> int | None:
        info = self.labels.get(name.upper().lstrip(':'))
        return info.index if info else None


def _normalize_label(raw: str) -> str:
    # `:` / `::` / a colon followed only by whitespace has no label name.
    parts = raw.upper().lstrip(':').split()
    return parts[0] if parts else ''


def build_cfg(nodes: list['Statement | Block']) -> ControlFlowGraph:
    stmts = flatten(nodes)
    labels: dict[str, LabelInfo] = {}
    for idx, s in enumerate(stmts):
        ct = s.code_tokens()
        # `:name` -- with or without trailing text, which cmd.exe ignores
        # (`:loop rest of line` is exactly the label `loop`). is_label() only
        # accepts the bare form, so key off the leading token directly.
        if ct and ct[0].kind == TokenKind.LABEL:
            name = _normalize_label(ct[0].inner or '')
            if name and name not in labels:   # first definition wins, matches cmd.exe
                labels[name] = LabelInfo(name, s, idx)

    gotos: list[GotoEdge] = []
    for idx, s in enumerate(stmts):
        ct = s.code_tokens()
        # GOTO/CALL don't have to be the statement's own first word: a
        # same-line `if <cond> goto X` (no parens -- the parenthesized
        # `if (...) (goto X)` form instead becomes its own nested Statement,
        # already reached by flatten() into the block body) embeds the
        # keyword after the condition. `if`'s condition grammar never
        # legitimately contains a bare, unquoted GOTO/CALL token, so the
        # first unquoted occurrence anywhere in the statement is unambiguous.
        kw_idx = next((i for i, t in enumerate(ct)
                       if t.kind == TokenKind.TEXT and not t.in_quotes
                       and t.value.lstrip('@').upper() in ('GOTO', 'CALL')), None)
        if kw_idx is None:
            continue
        word = ct[kw_idx].value.lstrip('@').upper()
        is_call = word == 'CALL'
        rest = ct[kw_idx + 1:]
        if is_call:
            target_tok = next((t for t in rest if t.kind in (TokenKind.TEXT, TokenKind.LABEL)), None)
            if target_tok is None or not (target_tok.kind == TokenKind.LABEL or target_tok.value.startswith(':')):
                continue  # `call` on an external command/program, not a label -- not our concern here
        else:
            target_tok = rest[0] if rest else None

        if target_tok is None:
            gotos.append(GotoEdge(s, idx, None, is_call, reason='no target token'))
            continue
        if target_tok.kind == TokenKind.LABEL:
            name = _normalize_label(target_tok.inner or '')
        elif target_tok.kind == TokenKind.TEXT:
            name = _normalize_label(target_tok.value)
        else:
            gotos.append(GotoEdge(s, idx, None, is_call, reason=f'non-literal target: {target_tok.kind.name}'))
            continue
        if name.upper() == 'EOF':
            gotos.append(GotoEdge(s, idx, 'EOF', is_call))
            continue
        if name not in labels:
            gotos.append(GotoEdge(s, idx, None, is_call, reason=f'target label not found: {name}'))
            continue
        gotos.append(GotoEdge(s, idx, name, is_call))

    return ControlFlowGraph(stmts, labels, gotos)


def reachable_labels(cfg: ControlFlowGraph) -> set[str]:
    """Labels reached by at least one *statically resolved* goto/call edge.
    Does not attempt fall-through reachability analysis (that's each pass's
    job, since 'reachable' means different things for dead-code removal vs.
    goto-unflattening) -- this is purely the direct-jump-target set."""
    return {g.target for g in cfg.gotos if g.target and g.target != 'EOF'}
