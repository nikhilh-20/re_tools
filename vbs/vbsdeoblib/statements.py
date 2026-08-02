"""Statement-span splitter for VBScript source.

split_statements(tokens) -> list[StatementSpan]

A StatementSpan is a contiguous slice of the token list that forms one
logical VBScript statement.  The splitter respects:
  - Line continuations (trailing _): joins the next physical line.
  - Colon (:) as statement separator (outside strings/comments).
  - Block keywords (If/Then/Else/End, For/Next, Do/Loop, While/Wend,
    Function/Sub/End, Select/Case/End Select) — these are NOT split by
    the statement splitter; callers that need block structure must handle
    them separately.
  - COMMENT and WS tokens are included in the span that owns the line.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List
from .tokenizer import VbsToken, TokenKind


@dataclass
class StatementSpan:
    tokens: list[VbsToken] = field(default_factory=list)

    @property
    def start(self) -> int:
        return self.tokens[0].start if self.tokens else 0

    @property
    def end(self) -> int:
        return self.tokens[-1].end if self.tokens else 0

    def code_tokens(self) -> list[VbsToken]:
        return [t for t in self.tokens
                if t.kind not in (TokenKind.WS, TokenKind.COMMENT,
                                  TokenKind.NEWLINE, TokenKind.LINECONT)]

    def raw(self, src: str) -> str:
        if not self.tokens:
            return ''
        return src[self.start: self.end]


def split_statements(tokens: list[VbsToken]) -> list[StatementSpan]:
    """Split *tokens* into logical statement spans.

    Splitting rules:
      - NEWLINE that is NOT preceded by LINECONT ends the current statement.
      - COLON that is not inside parentheses ends the current statement.
      - LINECONT + NEWLINE merges the following physical line into the current
        logical statement.
    WS, COMMENT, and LINECONT tokens are included in the span they belong to.
    """
    spans: list[StatementSpan] = []
    current: list[VbsToken] = []
    paren_depth = 0
    continuation = False   # True after we saw a LINECONT

    for tok in tokens:
        if tok.kind == TokenKind.LINECONT:
            current.append(tok)
            continuation = True
            continue

        if tok.kind == TokenKind.NEWLINE:
            current.append(tok)
            if continuation:
                continuation = False
                # The newline is swallowed into the logical line — keep going.
                continue
            # Real statement boundary.
            if current:
                spans.append(StatementSpan(current))
                current = []
            continue

        if tok.kind == TokenKind.OP and tok.value == '(':
            paren_depth += 1

        if tok.kind == TokenKind.OP and tok.value == ')':
            paren_depth = max(0, paren_depth - 1)

        if tok.kind == TokenKind.COLON and paren_depth == 0:
            current.append(tok)
            if current:
                spans.append(StatementSpan(current))
                current = []
            continue

        current.append(tok)
        continuation = False

    if current:
        spans.append(StatementSpan(current))

    return spans
