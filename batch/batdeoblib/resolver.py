"""Shared constant evaluators: `set /a` arithmetic and `if` condition truth.

Both are pure functions over already-expanded text (callers run
expand_statement() first) -- these evaluators never touch the environment or
perform their own %/! substitution.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# set /a arithmetic
# ---------------------------------------------------------------------------
# Precedence (low to high), matching cmd.exe's documented `set /a` grammar:
#   ,  (expression sequence -- evaluates each, result is the last)
#   = += -= *= /= %= &= |= ^= <<= >>=
#   ||
#   &&
#   |
#   ^
#   &
#   == != <> <= >= < >          (not documented on MSDN's set/a page but
#                                 accepted in practice; kept out of scope --
#                                 set /a's real domain is arithmetic, and a
#                                 comparison-shaped input is refused below)
#   << >>
#   + -
#   * / %
#   unary + - ~ !
# Comma-sequences and compound-assignment targets are handled by the
# 'assignment' helpers in this module; _eval_expr below handles one bare
# arithmetic expression (the right-hand side).

_TOKEN_RE = re.compile(r'''
      0[xX][0-9A-Fa-f]+        # hex
    | 0[0-7]+                  # octal (leading zero, no 8/9)
    | [0-9]+                   # decimal
    | [A-Za-z_]\w*              # bare identifier -- set /a resolves these as
                                 # variable reads directly, no %/! needed
    | <<|>>|&&|\|\||==|!=|<=|>=
    | [()+\-*/%&|^~!]
    | \s+
''', re.VERBOSE)


class ArithError(Exception):
    pass


def _lex(expr: str) -> list[str]:
    toks = []
    pos = 0
    for m in _TOKEN_RE.finditer(expr):
        if m.start() != pos:
            raise ArithError(f'unrecognized token near {expr[pos:pos+10]!r}')
        t = m.group(0)
        if not t.isspace():
            toks.append(t)
        pos = m.end()
    if pos != len(expr):
        raise ArithError(f'unrecognized token near {expr[pos:pos+10]!r}')
    return toks


def _parse_int(tok: str) -> int:
    if tok.lower().startswith('0x'):
        return int(tok, 16)
    if len(tok) > 1 and tok[0] == '0' and tok.isdigit():
        return int(tok, 8)
    return int(tok, 10)


class _Parser:
    """Small recursive-descent parser/evaluator over a flat token list.
    32-bit signed wraparound, matching cmd.exe's `set /a` (which operates
    on native 32-bit signed ints)."""

    def __init__(self, toks: list[str], resolve_var=None) -> None:
        self.toks = toks
        self.i = 0
        self.resolve_var = resolve_var

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _eat(self, expected: str | None = None) -> str:
        t = self._peek()
        if t is None or (expected is not None and t != expected):
            raise ArithError(f'expected {expected!r}, got {t!r}')
        self.i += 1
        return t

    @staticmethod
    def _wrap(v: int) -> int:
        v &= 0xFFFFFFFF
        if v >= 0x80000000:
            v -= 0x100000000
        return v

    def parse(self) -> int:
        v = self._or_expr()
        if self.i != len(self.toks):
            raise ArithError(f'trailing tokens: {self.toks[self.i:]}')
        return v

    def _or_expr(self) -> int:
        v = self._and_expr()
        while self._peek() == '||':
            self._eat('||')
            r = self._and_expr()
            v = 1 if (v != 0 or r != 0) else 0
        return v

    def _and_expr(self) -> int:
        v = self._bitor_expr()
        while self._peek() == '&&':
            self._eat('&&')
            r = self._bitor_expr()
            v = 1 if (v != 0 and r != 0) else 0
        return v

    def _bitor_expr(self) -> int:
        v = self._bitxor_expr()
        while self._peek() == '|':
            self._eat('|')
            v = self._wrap(v | self._bitxor_expr())
        return v

    def _bitxor_expr(self) -> int:
        v = self._bitand_expr()
        while self._peek() == '^':
            self._eat('^')
            v = self._wrap(v ^ self._bitand_expr())
        return v

    def _bitand_expr(self) -> int:
        v = self._shift_expr()
        while self._peek() == '&':
            self._eat('&')
            v = self._wrap(v & self._shift_expr())
        return v

    def _shift_expr(self) -> int:
        v = self._add_expr()
        while self._peek() in ('<<', '>>'):
            op = self._eat()
            r = self._add_expr() & 0x1F
            v = self._wrap((v << r) if op == '<<' else (v >> r if v >= 0 else -((-v - 1) >> r) - 1))
        return v

    def _add_expr(self) -> int:
        v = self._mul_expr()
        while self._peek() in ('+', '-'):
            op = self._eat()
            r = self._mul_expr()
            v = self._wrap(v + r if op == '+' else v - r)
        return v

    def _mul_expr(self) -> int:
        v = self._unary_expr()
        while self._peek() in ('*', '/', '%'):
            op = self._eat()
            r = self._unary_expr()
            if op in ('/', '%') and r == 0:
                raise ArithError('division by zero')
            if op == '*':
                v = self._wrap(v * r)
            elif op == '/':
                q = abs(v) // abs(r)
                v = self._wrap(q if (v < 0) == (r < 0) else -q)
            else:
                rem = abs(v) % abs(r)
                v = self._wrap(rem if v >= 0 else -rem)
        return v

    def _unary_expr(self) -> int:
        if self._peek() in ('+', '-', '~', '!'):
            op = self._eat()
            v = self._unary_expr()
            if op == '-':
                return self._wrap(-v)
            if op == '~':
                return self._wrap(~v)
            if op == '!':
                return 0 if v != 0 else 1
            return v
        return self._primary()

    def _primary(self) -> int:
        t = self._peek()
        if t == '(':
            self._eat('(')
            v = self._or_expr()
            self._eat(')')
            return v
        if t is None:
            raise ArithError('unexpected end of expression')
        self._eat()
        if t[0].isalpha() or t[0] == '_':
            # bare identifier -- `set /a` resolves these as variable reads
            # directly, no %/! wrapping needed (verified empirically:
            # `set /a "x=y+1"` reads y's numeric value; an unset or
            # non-numeric variable contributes 0, not an error).
            if self.resolve_var is None:
                raise ArithError(f'bare identifier not resolvable: {t!r}')
            v = self.resolve_var(t)
            if v is None:
                raise ArithError(f'variable not statically resolvable: {t!r}')
            return self._wrap(v)
        try:
            return self._wrap(_parse_int(t))
        except ValueError:
            raise ArithError(f'not a numeric literal: {t!r}')


def eval_arith(expr: str, resolve_var=None) -> int:
    """Evaluate a `set /a` arithmetic expression. *resolve_var*, if given, is
    called as resolve_var(name) -> int|None for each bare identifier
    encountered (set /a's own variable-read syntax, distinct from %/!
    expansion) -- return None to refuse the whole expression. Without a
    callback, any bare identifier is a refusal. Raises ArithError for
    anything unsupported (non-literal operand, division by zero, trailing
    garbage, unresolvable identifier) -- callers should catch this and
    refuse."""
    toks = _lex(expr.strip())
    if not toks:
        raise ArithError('empty expression')
    return _Parser(toks, resolve_var).parse()


def format_arith(v: int) -> str:
    return str(v)


# ---------------------------------------------------------------------------
# if-condition evaluation
# ---------------------------------------------------------------------------

@dataclass
class CondResult:
    value: bool | None    # None = not statically decidable
    reason: str | None = None

    @staticmethod
    def true() -> 'CondResult':
        return CondResult(True)

    @staticmethod
    def false() -> 'CondResult':
        return CondResult(False)

    @staticmethod
    def unknown(reason: str) -> 'CondResult':
        return CondResult(None, reason)


_STR_CMP_RE = re.compile(r'^(.*?)\s*(==)\s*(.*)$')
_NUM_CMP_RE = re.compile(r'^(.+?)\s+(EQU|NEQ|LSS|LEQ|GTR|GEQ)\s+(.+)$', re.IGNORECASE)
_INT_RE = re.compile(r'^[+-]?\d+$')


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def eval_condition(text: str, *, case_insensitive: bool = False) -> CondResult:
    """Evaluate a single, already-expanded `if` condition body (the part
    between `if [/i] [not] ` and the true-branch command). Recognizes:
      - `defined NAME`             -- requires the caller to have already
                                       substituted this to a literal
                                       true/false marker; NOT handled here
                                       (env-dependent -- see bat_unwrap_trueif).
      - string equality: `"A"=="B"` / `A==B`
      - numeric comparison: `A EQU B`, `NEQ`, `LSS`, `LEQ`, `GTR`, `GEQ`
      - bare truthy literal: a non-empty, non-zero constant (rare, but valid
        as e.g. `if 1 (...)` is NOT legal cmd.exe syntax on its own --
        this branch exists only for already-reduced sub-expressions).
    Returns CondResult.unknown(reason) for anything else -- `if exist`,
    `if errorlevel`, and comparisons against something not proven constant
    are environment-dependent and never resolved here.
    """
    t = text.strip()

    not_prefix = False
    m = re.match(r'(?i)^not\s+', t)
    if m:
        not_prefix = True
        t = t[m.end():]

    result: CondResult
    m = _STR_CMP_RE.match(t)
    if m:
        lhs, _, rhs = m.groups()
        a, b = _strip_quotes(lhs), _strip_quotes(rhs)
        if case_insensitive:
            a, b = a.lower(), b.lower()
        result = CondResult.true() if a == b else CondResult.false()
    else:
        m = _NUM_CMP_RE.match(t)
        if m:
            lhs, op, rhs = m.groups()
            lhs, rhs = _strip_quotes(lhs), _strip_quotes(rhs)
            if not (_INT_RE.match(lhs) and _INT_RE.match(rhs)):
                return CondResult.unknown('numeric comparison operand not a literal integer')
            a, b = int(lhs), int(rhs)
            op = op.upper()
            result = CondResult(
                {'EQU': a == b, 'NEQ': a != b, 'LSS': a < b,
                 'LEQ': a <= b, 'GTR': a > b, 'GEQ': a >= b}[op]
            )
        else:
            return CondResult.unknown(f'unrecognized condition shape: {text!r}')

    if not_prefix and result.value is not None:
        return CondResult(not result.value)
    return result
