"""cmd.exe-aware tokenizer for Windows Batch source.

Produces a flat, quote-and-caret-aware token stream. Every pass locates edit
targets by token span (offsets into the *original* source), never by raw-text
regex, so a special character inside a quoted string or after a caret escape
is structurally impossible to be mistaken for grammar.

Semantics implemented here are the LEXICAL layer only (what cmd.exe's line
reader recognizes as a shape), not the semantic layer (whether a recognized
shape actually gets expanded). In particular:

  - `!NAME!` is always tokenized as a BANG_CANDIDATE when it lexically pairs
    up on one line — whether it is *actually* delayed-expanded depends on
    whether `setlocal EnableDelayedExpansion` / `cmd /v:on` is active at that
    point in execution, which is a runtime fact tracked by env.py /
    expansion.py, not a lexical one. (Verified empirically: with delayed
    expansion off, `!V!` prints completely unexpanded, literal exclamation
    marks and all — the parser doesn't even attempt substitution.)

  - `%%A` (a doubled percent immediately followed by a single letter) is
    tokenized as a plain PERCENT_LITERAL + TEXT pair here. Recognizing it as
    a `for`-loop metavariable reference is contextual (only inside the `do`
    clause of a `for %%A in (...) do ...` that declared that letter) and is
    handled by statements.py's `bind_for_variables`.

Empirically-verified ground truth this tokenizer/expansion.py encode (probed
directly against cmd.exe on Windows 10.0.19045; see README "Verification"):
  - `%%` always collapses to a literal `%`, in or out of quotes.
  - `%VAR%` (matched pair) with VAR unset -> empty string. NOT left literal.
    (Contradicts widely-repeated folklore; verified directly.)
  - A `%` with no matching second `%` before end of line -> deleted (empty),
    not left literal.
  - `!VAR!` (delayed expansion active) with VAR unset -> empty string.
  - Outside quotes, `^` always consumes itself and makes the next character
    literal, whether or not that character was otherwise special (so
    `^X` -> `X`, not just `^&` -> `&`). This is the caret-splitting evasion
    trick, the direct analogue of PsStrip-Backticks / backtick-splitting.
  - Inside quotes, `^` has no special meaning at all (stays literal, doesn't
    escape anything).
  - `^` immediately before a line break deletes both (line continuation).
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum, auto


class TokenKind(Enum):
    TEXT        = auto()  # run of ordinary literal characters
    WS          = auto()  # spaces/tabs (not newline)
    NEWLINE     = auto()  # \n or \r\n
    QUOTE       = auto()  # a single " character (toggles quote state)
    CARET_ESC   = auto()  # ^ + next char, outside quotes (LINECONT excluded)
    LINECONT    = auto()  # ^ + newline, outside quotes
    PCT_LIT     = auto()  # %% -> literal %
    PCT_VAR     = auto()  # %NAME% or %NAME:modifier%  (matched pair)
    PCT_ARG     = auto()  # %0-%9, %*
    PCT_MODARG  = auto()  # %~<mods><digit|*>
    PCT_UNMATCH = auto()  # a lone % with no closing partner on the line
    BANG_CAND   = auto()  # !NAME! or !NAME:modifier!  (matched pair)
    OP          = auto()  # & && || | < > >> ( ) , ;   (grammar-significant)
    LABEL       = auto()  # :name at start of a statement
    COMMENT     = auto()  # rem ... / :: ...  (whole-line)
    UNKNOWN     = auto()


@dataclass(slots=True)
class BatToken:
    kind:      TokenKind
    value:     str    # raw source text of this token (includes carets, %, ! etc.)
    start:     int    # byte offset in the original source
    end:       int    # exclusive end byte offset
    in_quotes: bool    # was this token's *start* position inside a quoted span?
    inner:     str | None = None   # decoded payload for PCT_VAR/BANG_CAND/CARET_ESC (name/modifier text or literal char)

    @property
    def upper(self) -> str:
        return self.value.upper()


_MOD_LETTERS = "fdpnxsatz"
# %~[mods]N  or  %~[mods]*   (mods = any combo of f d p n x s a t z, order-insensitive;
# real cmd.exe also supports $PATH:N — not modeled, falls back to PCT_UNMATCH-like TEXT)
_MODARG_RE = re.compile(rf'~([{_MOD_LETTERS}]*)([0-9]|\*)')

_LABEL_NAME_RE = re.compile(r'[^\s&|<>()"^%!]+')
_OP2_RE = re.compile(r'&&|\|\||>>')
_OP1 = set('&|<>(),;')


def _is_line_start(tokens: list[BatToken]) -> bool:
    """True if the next token would be the first non-WS token on its logical
    physical line (i.e. previous real token was NEWLINE, LINECONT, or nothing)."""
    for t in reversed(tokens):
        if t.kind == TokenKind.WS:
            continue
        return t.kind in (TokenKind.NEWLINE, TokenKind.LINECONT)
    return True


def _try_comment(src: str, pos: int, tokens: list[BatToken]) -> BatToken | None:
    """At a statement-start position, recognize `rem ...` or `:: ...` as a
    whole-line COMMENT token. Returns None if this isn't a comment start."""
    n = len(src)
    m = re.match(r'[ \t]*', src[pos:])
    lead_ws_end = pos + (m.end() if m else 0)

    # `::` — the classic double-colon comment idiom.
    if src.startswith('::', lead_ws_end):
        end_m = re.search(r'\r?\n', src[lead_ws_end:])
        end = lead_ws_end + end_m.start() if end_m else n
        return BatToken(TokenKind.COMMENT, src[pos:end], pos, end, in_quotes=False)

    # `rem` (case-insensitive), followed by WS, EOL, or EOF.
    rem_m = re.match(r'(?i)rem(?=[ \t]|\r?\n|$)', src[lead_ws_end:])
    if rem_m:
        end_m = re.search(r'\r?\n', src[lead_ws_end:])
        end = lead_ws_end + end_m.start() if end_m else n
        return BatToken(TokenKind.COMMENT, src[pos:end], pos, end, in_quotes=False)

    return None


def tokenize(src: str) -> list[BatToken]:
    """Tokenize *src* into a list of BatTokens (including WS/NEWLINE tokens).
    Never raises — unrecognised characters are emitted as UNKNOWN tokens."""
    tokens: list[BatToken] = []
    pos = 0
    n = len(src)
    in_quotes = False

    while pos < n:
        ch = src[pos]
        line_start = _is_line_start(tokens)

        # --- whole-line comment at statement-start (never inside quotes: a
        # statement-start position is by definition not mid-quote) ---
        if line_start and not in_quotes:
            c = _try_comment(src, pos, tokens)
            if c is not None:
                tokens.append(c)
                pos = c.end
                continue

        # --- label at statement-start: single ':' not doubled ---
        if line_start and not in_quotes and ch == ':' and src[pos:pos + 2] != '::':
            m = _LABEL_NAME_RE.match(src, pos + 1)
            name_end = m.end() if m else pos + 1
            tokens.append(BatToken(TokenKind.LABEL, src[pos:name_end], pos, name_end,
                                    in_quotes=False, inner=src[pos + 1:name_end]))
            pos = name_end
            continue

        # --- quote toggle ---
        if ch == '"':
            tokens.append(BatToken(TokenKind.QUOTE, '"', pos, pos + 1, in_quotes=in_quotes))
            in_quotes = not in_quotes
            pos += 1
            continue

        # --- newline ---
        if ch in '\r\n':
            m = re.match(r'\r?\n', src[pos:])
            end = pos + m.end()
            tokens.append(BatToken(TokenKind.NEWLINE, src[pos:end], pos, end, in_quotes=in_quotes))
            pos = end
            continue

        # --- whitespace ---
        if ch in ' \t':
            m = re.match(r'[ \t]+', src[pos:])
            end = pos + m.end()
            tokens.append(BatToken(TokenKind.WS, src[pos:end], pos, end, in_quotes=in_quotes))
            pos = end
            continue

        # --- caret ---
        if ch == '^' and not in_quotes:
            nxt = src[pos + 1:pos + 2]
            if nxt in ('\r', '\n', ''):
                m = re.match(r'\r?\n', src[pos + 1:])
                if m:
                    end = pos + 1 + m.end()
                    tokens.append(BatToken(TokenKind.LINECONT, src[pos:end], pos, end, in_quotes=False))
                    pos = end
                    continue
                # caret at absolute EOF: treat as literal escaped-nothing
                tokens.append(BatToken(TokenKind.CARET_ESC, '^', pos, pos + 1, in_quotes=False, inner=''))
                pos += 1
                continue
            end = pos + 2
            tokens.append(BatToken(TokenKind.CARET_ESC, src[pos:end], pos, end, in_quotes=False, inner=nxt))
            pos = end
            continue

        # --- percent ---
        if ch == '%':
            # %% -> literal % (always, in or out of quotes)
            if src[pos + 1:pos + 2] == '%':
                tokens.append(BatToken(TokenKind.PCT_LIT, '%%', pos, pos + 2, in_quotes=in_quotes, inner='%'))
                pos += 2
                continue
            nxt = src[pos + 1:pos + 2]
            if nxt.isdigit() or nxt == '*':
                end = pos + 2
                tokens.append(BatToken(TokenKind.PCT_ARG, src[pos:end], pos, end, in_quotes=in_quotes, inner=nxt))
                pos = end
                continue
            if nxt == '~':
                m = _MODARG_RE.match(src, pos + 1)
                if m:
                    end = m.end()
                    tokens.append(BatToken(TokenKind.PCT_MODARG, src[pos:end], pos, end,
                                            in_quotes=in_quotes, inner=src[pos + 1:end]))
                    pos = end
                    continue
            # bare %NAME[:modifier]% — scan for the next single % before a newline
            nl = re.search(r'\r?\n', src[pos + 1:])
            search_end = pos + 1 + (nl.start() if nl else n - pos - 1)
            close = src.find('%', pos + 1, search_end + 1)
            if close == -1:
                # unmatched lone % -> deleted (empirically verified: expands to "")
                tokens.append(BatToken(TokenKind.PCT_UNMATCH, '%', pos, pos + 1, in_quotes=in_quotes, inner=''))
                pos += 1
                continue
            end = close + 1
            tokens.append(BatToken(TokenKind.PCT_VAR, src[pos:end], pos, end,
                                    in_quotes=in_quotes, inner=src[pos + 1:close]))
            pos = end
            continue

        # --- bang (delayed-expansion candidate; interpretation deferred) ---
        if ch == '!':
            nl = re.search(r'\r?\n', src[pos + 1:])
            search_end = pos + 1 + (nl.start() if nl else n - pos - 1)
            close = src.find('!', pos + 1, search_end + 1)
            if close == -1:
                tokens.append(BatToken(TokenKind.TEXT, '!', pos, pos + 1, in_quotes=in_quotes))
                pos += 1
                continue
            end = close + 1
            tokens.append(BatToken(TokenKind.BANG_CAND, src[pos:end], pos, end,
                                    in_quotes=in_quotes, inner=src[pos + 1:close]))
            pos = end
            continue

        # --- grammar operators (only significant outside quotes) ---
        if not in_quotes:
            m2 = _OP2_RE.match(src, pos)
            if m2:
                tokens.append(BatToken(TokenKind.OP, m2.group(0), pos, m2.end(), in_quotes=False))
                pos = m2.end()
                continue
            if ch in _OP1:
                tokens.append(BatToken(TokenKind.OP, ch, pos, pos + 1, in_quotes=False))
                pos += 1
                continue

        # --- ordinary text run ---
        # %, !, ", whitespace and newline are always special (quote-toggle,
        # var-expansion candidates, and structural boundaries apply inside
        # quotes too). ^ and the grammar operators are special ONLY outside
        # quotes -- inside a quoted string they are ordinary literal text.
        stop_chars = {' ', '\t', '\r', '\n', '"', '%', '!'}
        if not in_quotes:
            stop_chars |= {'^'} | _OP1
        j = pos
        while j < n and src[j] not in stop_chars and not (not in_quotes and src.startswith(('&&', '||', '>>'), j)):
            j += 1
        if j == pos:
            tokens.append(BatToken(TokenKind.UNKNOWN, src[pos], pos, pos + 1, in_quotes=in_quotes))
            pos += 1
            continue
        tokens.append(BatToken(TokenKind.TEXT, src[pos:j], pos, j, in_quotes=in_quotes))
        pos = j

    return tokens


def code_tokens(tokens: list[BatToken]) -> list[BatToken]:
    """All tokens except WS and COMMENT."""
    return [t for t in tokens if t.kind not in (TokenKind.WS, TokenKind.COMMENT)]


def non_ws(tokens: list[BatToken]) -> list[BatToken]:
    """All tokens except WS (keeps COMMENTs and NEWLINEs)."""
    return [t for t in tokens if t.kind != TokenKind.WS]
