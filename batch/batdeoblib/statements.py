"""Statement/block structure builder for Batch token streams.

A cmd.exe script is a sequence of *statements* (commands), some of which are
grouped into parenthesized *blocks* -- `if (...) else (...)`, `for ... do
(...)`, or a bare `( ... )` grouping. Block membership matters beyond pure
grouping: it changes WHEN `%VAR%` references inside it get expanded.

Empirically verified (see tokenizer.py docstring / README): a `%VAR%`
reference lexically inside a `(...)` block is expanded exactly ONCE, when the
block is initially read/parsed -- using whatever VAR held at that moment --
and that expanded value is then reused for every statement in the block, even
across multiple `for` iterations. A `!VAR!` reference inside the same block,
by contrast, is a per-statement runtime substitution exactly like it is at
top level; this is *why* delayed expansion exists. bat_propagate_constants.py
and friends must consult a Statement's `.in_block` to know which rule
applies to a given %-reference.

Statement splitting rules:
  - NEWLINE not absorbed into a LINECONT ends the current statement (the
    tokenizer already merges caret-continued physical lines into one
    contiguous run with no intervening NEWLINE, so this is a plain split).
  - `&`, `&&`, `||`, `|` (OP tokens, therefore already guaranteed to be
    outside quotes) end the current statement and start a new one at the
    SAME nesting level.
  - `,` and `;` do NOT split statements -- in cmd.exe they are argument
    separators within one command, not command separators.
  - A `(` OP opens a new Block frame; the matching `)` OP closes it. Both
    belong to the ENCLOSING frame as delimiters, not to the block's own body.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .tokenizer import BatToken, TokenKind

_SPLIT_OPS = {'&', '&&', '||', '|'}


@dataclass
class Statement:
    tokens: list[BatToken] = field(default_factory=list)
    in_block: bool = False
    connector_before: str | None = None   # '&' / '&&' / '||' / '|' / None

    @property
    def start(self) -> int:
        return self.tokens[0].start if self.tokens else 0

    @property
    def end(self) -> int:
        return self.tokens[-1].end if self.tokens else 0

    def code_tokens(self) -> list[BatToken]:
        return [t for t in self.tokens
                if t.kind not in (TokenKind.WS, TokenKind.COMMENT, TokenKind.NEWLINE, TokenKind.LINECONT)]

    def raw(self, src: str) -> str:
        return src[self.start:self.end] if self.tokens else ''

    def is_label(self) -> bool:
        ct = self.code_tokens()
        return len(ct) == 1 and ct[0].kind == TokenKind.LABEL

    def is_comment(self) -> bool:
        ct = self.code_tokens()
        return len(ct) == 1 and ct[0].kind == TokenKind.COMMENT

    def first_word(self) -> str | None:
        for t in self.code_tokens():
            if t.kind == TokenKind.TEXT:
                return t.value.lstrip('@').upper()
            return None
        return None

    def is_call(self) -> bool:
        return self.first_word() == 'CALL'


@dataclass
class Block:
    open_tok: BatToken
    close_tok: BatToken
    body: list['Statement | Block'] = field(default_factory=list)
    in_block: bool = False   # is this block itself nested inside another block?
    connector_before: str | None = None

    @property
    def start(self) -> int:
        return self.open_tok.start

    @property
    def end(self) -> int:
        return self.close_tok.end

    def raw(self, src: str) -> str:
        return src[self.start:self.end]


def _split_statements_flat(tokens: list[BatToken], *, in_block: bool) -> list[Statement]:
    """Split a flat (no nested parens) token run into Statements on NEWLINE
    and on & / && / || / | connectors."""
    out: list[Statement] = []
    current: list[BatToken] = []
    pending_connector: str | None = None

    def flush():
        nonlocal current, pending_connector
        if current:
            out.append(Statement(current, in_block=in_block, connector_before=pending_connector))
        current = []
        pending_connector = None

    for tok in tokens:
        if tok.kind == TokenKind.NEWLINE:
            current.append(tok)
            flush()
            continue
        if tok.kind == TokenKind.OP and tok.value in _SPLIT_OPS:
            flush()
            pending_connector = tok.value
            continue
        current.append(tok)

    flush()
    return out


def parse_script(tokens: list[BatToken]) -> list['Statement | Block']:
    """Build the top-level statement/block sequence for a whole token stream.

    Returns the ROOT frame's children; each Block recursively contains its
    own children the same way. Unbalanced parens (more '(' than ')', or vice
    versa) degrade gracefully: any block left open at end-of-input is closed
    at the final token rather than raising, since a deobfuscation tool must
    never crash on malformed/truncated input.
    """
    def build(tok_iter: list[BatToken], start_idx: int, *, in_block: bool) -> tuple[list['Statement | Block'], int]:
        segment: list[BatToken] = []
        children: list['Statement | Block'] = []
        pending_connector: str | None = None
        i = start_idx
        n = len(tok_iter)

        while i < n:
            tok = tok_iter[i]
            if tok.kind == TokenKind.OP and tok.value == '(':
                # flush what we have as ordinary statements first
                if segment:
                    stmts = _split_statements_flat(segment, in_block=in_block)
                    if stmts and pending_connector is not None:
                        stmts[0].connector_before = pending_connector
                    children.extend(stmts)
                    segment = []
                    pending_connector = None
                sub_children, next_i = build(tok_iter, i + 1, in_block=True)
                close_tok = tok_iter[next_i] if next_i < n and tok_iter[next_i].kind == TokenKind.OP and tok_iter[next_i].value == ')' else tok
                blk = Block(open_tok=tok, close_tok=close_tok, body=sub_children,
                            in_block=in_block, connector_before=pending_connector)
                pending_connector = None
                children.append(blk)
                i = next_i + 1 if next_i < n else n
                continue
            if tok.kind == TokenKind.OP and tok.value == ')':
                # end of this block frame
                if segment:
                    stmts = _split_statements_flat(segment, in_block=in_block)
                    if stmts and pending_connector is not None:
                        stmts[0].connector_before = pending_connector
                    children.extend(stmts)
                    segment = []
                return children, i
            segment.append(tok)
            i += 1

        if segment:
            stmts = _split_statements_flat(segment, in_block=in_block)
            if stmts and pending_connector is not None:
                stmts[0].connector_before = pending_connector
            children.extend(stmts)
        return children, i

    top, _ = build(tokens, 0, in_block=False)
    return top


def flatten(nodes: list['Statement | Block']) -> list[Statement]:
    """Depth-first flatten of a parse_script() tree into a plain Statement list,
    in source order, for passes that don't need block structure."""
    out: list[Statement] = []
    for node in nodes:
        if isinstance(node, Statement):
            out.append(node)
        else:
            out.extend(flatten(node.body))
    return out
