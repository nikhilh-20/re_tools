"""VBScript tokenizer — produces a flat, string/comment-aware token stream.

Every pass locates edit targets by token span, never by raw-text regex, so a
' or & inside a string/comment is structurally impossible to be mistaken for code.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator, List


class TokenKind(Enum):
    STRING   = auto()   # "..." with "" escaping
    NUMBER   = auto()   # decimal / &Hxx hex / &Oxx octal
    IDENT    = auto()   # identifier (may be a keyword — see KEYWORDS)
    COMMENT  = auto()   # ' ... or Rem ... to end of line
    LINECONT = auto()   # trailing _  (line continuation)
    NEWLINE  = auto()   # \n or \r\n
    COLON    = auto()   # : (statement separator)
    OP       = auto()   # & + - * / \ ^ = <> <= >= < > ( ) , . # _
    WS       = auto()   # spaces/tabs between tokens
    UNKNOWN  = auto()   # anything the lexer doesn't recognise


# VBScript keywords (case-insensitive); callers check tok.kind == IDENT and
# tok.value.upper() in KEYWORDS rather than a separate token kind so that
# case-folded matching stays in one place.
KEYWORDS = frozenset("""
AND BYREF BYVAL CALL CASE CLASS CONST DIM DO EACH ELSE ELSEIF END ERASE ERROR
EXECUTE EXECUTEGLOBAL EXIT EXPLICIT FALSE FOR FUNCTION GET IF IN IS LET LOOP
MOD NEW NEXT NOT NOTHING NULL OBJECT ON OPTION OR PRESERVE PRIVATE PUBLIC
RANDOMIZE REDIM REM RESUME SELECT SET STEP STOP SUB THEN TO TRUE UNTIL
WEND WHILE WITH XOR
""".split())


@dataclass(slots=True)
class VbsToken:
    kind:  TokenKind
    value: str       # raw source text of this token
    start: int       # byte offset in the original source
    end:   int       # exclusive end byte offset

    @property
    def upper(self) -> str:
        return self.value.upper()

    def is_kw(self, *words: str) -> bool:
        return self.kind == TokenKind.IDENT and self.upper in {w.upper() for w in words}


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

# Compiled token patterns, tried in order.
_PATTERNS: list[tuple[TokenKind, re.Pattern[str]]] = [
    # String literal: double-quoted, "" is an escaped quote inside.
    #
    # Written as runs-of-non-quote separated by literal "" escapes, not as
    # `"(?:[^"]|"")*"`: that alternation forces the regex engine to choose
    # between its two branches at every single character of the string body,
    # which measured out as quadratic — a 32 MB single-line literal took ~17s
    # to match and a 64 MB one raised MemoryError outright. `[^"]*` instead
    # consumes an entire non-quote run in one step, so only the (rare) ""
    # escape sites cost an extra branch; the same 64 MB literal now matches
    # in well under a tenth of a second. Matches identically on escaped
    # quotes, empty strings, and unterminated strings (verified).
    (TokenKind.STRING,  re.compile(r'"[^"]*(?:""[^"]*)*"', re.S)),
    # Line-continuation: must come before OP so a trailing _ isn't tokenised as OP.
    (TokenKind.LINECONT, re.compile(r'_[ \t]*(?=\r?\n|$)')),
    # Number: hex &H, octal &O, or plain decimal (optional trailing type-suffix dDfFsSlLiIuU%).
    (TokenKind.NUMBER,  re.compile(r'&[Hh][0-9A-Fa-f]+|&[Oo][0-7]+|'
                                   r'[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?[dDfFsSlLiIuU%]?')),
    # Newlines: CRLF, bare CR (old-Mac style, or a stray duplicate CR before
    # CRLF — seen in some obfuscated drops), or bare LF.
    (TokenKind.NEWLINE, re.compile(r'\r\n|\r|\n')),
    # Comments: single-quote style.
    (TokenKind.COMMENT, re.compile(r"'[^\r\n]*")),
    # Statement separator.
    (TokenKind.COLON,   re.compile(r':')),
    # Multi-char operators first, then single-char.
    (TokenKind.OP,      re.compile(r'<>|<=|>=')),
    (TokenKind.OP,      re.compile(r'[&+\-*/\\^=<>()\[\],\.#]')),
    # Identifiers and keywords.
    (TokenKind.IDENT,   re.compile(r'[A-Za-z_][A-Za-z0-9_]*')),
    # Whitespace.
    (TokenKind.WS,      re.compile(r'[ \t]+')),
]


def _check_rem_comment(tokens: list[VbsToken], src: str, pos: int) -> int | None:
    """If the IDENT just produced is 'REM' at the start of a logical statement,
    consume the rest of the line as a COMMENT token and return the new position.
    Returns None if this is not a REM comment."""
    # Already added to tokens; peek at what precedes it: if the previous
    # non-WS token on this logical line is NEWLINE, COLON, or nothing, it's REM.
    preceding = [t for t in tokens[:-1] if t.kind not in (TokenKind.WS,)]
    if preceding and preceding[-1].kind not in (TokenKind.NEWLINE, TokenKind.COLON):
        return None
    # Consume to end of line.
    m = re.match(r'[^\r\n]*', src[pos:])
    rest = m.group(0) if m else ''
    return pos + len(rest)


def tokenize(src: str) -> list[VbsToken]:
    """Tokenize *src* into a list of VbsTokens (including WS and NEWLINE tokens).
    Never raises — unrecognised characters are emitted as UNKNOWN tokens."""
    tokens: list[VbsToken] = []
    pos = 0
    n = len(src)
    while pos < n:
        matched = False
        for kind, pat in _PATTERNS:
            m = pat.match(src, pos)
            if not m:
                continue
            tok = VbsToken(kind=kind, value=m.group(0), start=pos, end=m.end())

            # Special-case: REM keyword → rest of line becomes a COMMENT.
            if kind == TokenKind.IDENT and tok.upper == 'REM':
                tokens.append(tok)
                new_pos = _check_rem_comment(tokens, src, m.end())
                if new_pos is not None:
                    # Replace the IDENT with a COMMENT spanning "REM <rest>"
                    tokens.pop()
                    comment_text = src[pos:new_pos]
                    tokens.append(VbsToken(TokenKind.COMMENT, comment_text, pos, new_pos))
                    pos = new_pos
                    matched = True
                    break
                # else: REM used as a variable/function name (unusual but legal)

            tokens.append(tok)
            pos = m.end()
            matched = True
            break

        if not matched:
            tokens.append(VbsToken(TokenKind.UNKNOWN, src[pos], pos, pos + 1))
            pos += 1

    return tokens


class VbsTokenizer:
    """Thin stateful wrapper around tokenize() that also tracks logical lines
    by merging line-continuation tokens."""

    def __init__(self, src: str) -> None:
        self.src = src
        self.tokens = tokenize(src)

    # ------------------------------------------------------------------
    # Convenience iterators
    # ------------------------------------------------------------------

    def code_tokens(self) -> list[VbsToken]:
        """All tokens except WS and COMMENT."""
        return [t for t in self.tokens if t.kind not in (TokenKind.WS, TokenKind.COMMENT)]

    def non_ws(self) -> list[VbsToken]:
        """All tokens except WS (keeps COMMENTs and NEWLINEs)."""
        return [t for t in self.tokens if t.kind != TokenKind.WS]
