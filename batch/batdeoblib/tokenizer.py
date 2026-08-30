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

# Module-level compiled patterns, matched against the WHOLE source with a `pos`
# argument -- never against a `src[pos:]` slice. On a multi-MB file with few
# newlines (a one-line dropper, or very long lines) re-slicing the tail at every
# `%`/`!`/newline is O(n^2); anchored matching is O(1) per call.
#
# NEWLINE recognises a bare `\r` (old-Mac ending) and the leading `\r` of a
# doubled `\r\r\n` -- the old `\r?\n` matched neither, so `re.match(r'\r?\n',
# ...)` returned None and `m.end()` raised AttributeError, breaking the "never
# raises" contract; a `\r\r\n` line-ending yields two NEWLINE tokens (`\r`,
# then `\r\n`).
_NL_RE = re.compile(r'\r\n|\r|\n')
_WS_RUN_RE = re.compile(r'[ \t]+')
_WS_OPT_RE = re.compile(r'[ \t]*')
_REM_RE = re.compile(r'(?i)rem(?=[ \t]|\r|\n|$)')
# Maximal run of ordinary text. Inside quotes only whitespace / newline / " / %
# / ! break a run; outside quotes the caret and every grammar operator do too.
_TEXT_RUN_INQ_RE = re.compile(r'[^ \t\r\n"%!]+')
_TEXT_RUN_OUTQ_RE = re.compile(r'[^ \t\r\n"%!^&|<>(),;]+')


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
    lead_ws_end = _WS_OPT_RE.match(src, pos).end()

    # `::` — the classic double-colon comment idiom.
    if src.startswith('::', lead_ws_end):
        end_m = _NL_RE.search(src, lead_ws_end)
        end = end_m.start() if end_m else n
        return BatToken(TokenKind.COMMENT, src[pos:end], pos, end, in_quotes=False)

    # `rem` (case-insensitive), followed by WS, EOL, or EOF.
    rem_m = _REM_RE.match(src, lead_ws_end)
    if rem_m:
        end_m = _NL_RE.search(src, lead_ws_end)
        end = end_m.start() if end_m else n
        return BatToken(TokenKind.COMMENT, src[pos:end], pos, end, in_quotes=False)

    return None


def tokenize(src: str) -> list[BatToken]:
    """Tokenize *src* into a list of BatTokens (including WS/NEWLINE tokens).
    Never raises — unrecognised characters are emitted as UNKNOWN tokens."""
    tokens: list[BatToken] = []
    pos = 0
    n = len(src)
    in_quotes = False

    # Offset of the next line break at/after the current scan position, or n if
    # none remain. `%VAR%` / `!VAR!` pairs cannot span a line, so the % and !
    # branches bound their closing-delimiter search by this. Recomputed only
    # when `pos` crosses it -- once per line, O(n) total -- instead of an
    # `_NL_RE.search(src, pos)` at every % / ! (O(n^2) on a newline-sparse
    # multi-MB one-liner).
    line_end = _NL_RE.search(src, 0)
    line_end = line_end.start() if line_end else n

    while pos < n:
        if pos >= line_end:
            m = _NL_RE.search(src, pos)
            line_end = m.start() if m else n
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

        # --- newline (\r\n, bare \r, or bare \n; a \r\r\n yields two tokens) ---
        if ch == '\r' or ch == '\n':
            end = _NL_RE.match(src, pos).end()
            tokens.append(BatToken(TokenKind.NEWLINE, src[pos:end], pos, end, in_quotes=in_quotes))
            pos = end
            continue

        # --- whitespace ---
        if ch in ' \t':
            end = _WS_RUN_RE.match(src, pos).end()
            tokens.append(BatToken(TokenKind.WS, src[pos:end], pos, end, in_quotes=in_quotes))
            pos = end
            continue

        # --- caret ---
        if ch == '^' and not in_quotes:
            nxt = src[pos + 1:pos + 2]
            if nxt in ('\r', '\n', ''):
                m = _NL_RE.match(src, pos + 1)
                if m:
                    end = m.end()
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
            close = src.find('%', pos + 1, line_end)
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
            close = src.find('!', pos + 1, line_end)
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
        # A compiled character class instead of a Python per-char loop, so a
        # multi-MB literal run is one C-level scan, not millions of iterations.
        run_re = _TEXT_RUN_INQ_RE if in_quotes else _TEXT_RUN_OUTQ_RE
        m = run_re.match(src, pos)
        if m is None:
            tokens.append(BatToken(TokenKind.UNKNOWN, src[pos], pos, pos + 1, in_quotes=in_quotes))
            pos += 1
            continue
        tokens.append(BatToken(TokenKind.TEXT, src[pos:m.end()], pos, m.end(), in_quotes=in_quotes))
        pos = m.end()

    return tokens


def code_tokens(tokens: list[BatToken]) -> list[BatToken]:
    """All tokens except WS and COMMENT."""
    return [t for t in tokens if t.kind not in (TokenKind.WS, TokenKind.COMMENT)]


def non_ws(tokens: list[BatToken]) -> list[BatToken]:
    """All tokens except WS (keeps COMMENTs and NEWLINEs)."""
    return [t for t in tokens if t.kind != TokenKind.WS]
