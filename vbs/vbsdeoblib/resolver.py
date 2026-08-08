"""Shared constant-expression resolver for VBScript deobfuscation passes.

resolve_const(tokens, env) -> Const | None

Returns a typed constant (str, int, or float) when the token slice is a
compile-time-constant expression, or None when it cannot be determined.
Never raises — the caller treats None as "leave this expression untouched".

This is the VBS analog of Resolve-Const in _PsDeobLib.ps1: every fold pass
calls it rather than writing its own per-pattern logic, which keeps every
tool generic rather than coupled to any one sample's identifiers.
"""
from __future__ import annotations
from typing import Union, Optional
from .tokenizer import VbsToken, TokenKind

Const = Union[str, int, float]

# ---------------------------------------------------------------------------
# Allowlisted pure VBScript builtins safe to evaluate at analysis time.
# Only scalar-in / scalar-out, no I/O, no environment dependency.
# Each entry is (min_args, max_args, callable).
# ---------------------------------------------------------------------------
def _vbs_chr(n):      return chr(int(n))
def _vbs_asc(s):      return ord(str(s)[0]) if s else 0
def _vbs_len(s):      return len(str(s))
def _vbs_ucase(s):    return str(s).upper()
def _vbs_lcase(s):    return str(s).lower()
def _vbs_trim(s):     return str(s).strip()
def _vbs_ltrim(s):    return str(s).lstrip()
def _vbs_rtrim(s):    return str(s).rstrip()
def _vbs_cstr(v):     return str(v)
def _vbs_cint(v):     return int(float(str(v))) if isinstance(v, str) else int(v)
def _vbs_cdbl(v):     return float(str(v)) if isinstance(v, str) else float(v)
def _vbs_cbool(v):    return bool(v)
def _vbs_hex(n):      return hex(int(n))[2:].upper()
def _vbs_oct(n):      return oct(int(n))[2:]
def _vbs_abs(n):      return abs(n)
def _vbs_int_fn(n):   return int(n)  # VBScript Int() truncates toward -inf
def _vbs_fix(n):      return int(n)  # Fix() truncates toward 0
def _vbs_sqr(n):      return float(n) ** 0.5
def _vbs_strreverse(s): return str(s)[::-1]
def _vbs_space(n):    return ' ' * int(n)

def _vbs_mid(s, start, *rest):
    s = str(s); start = int(start) - 1  # VBS is 1-indexed
    if rest:
        return s[start: start + int(rest[0])]
    return s[start:]

def _vbs_left(s, n):  s = str(s); return s[:int(n)]
def _vbs_right(s, n): s = str(s); return s[max(0, len(s)-int(n)):]

def _vbs_replace(s, find, repl, *rest):
    s = str(s); find = str(find); repl = str(repl)
    start = int(rest[0]) - 1 if rest else 0           # 1-indexed start
    count = int(rest[1]) if len(rest) > 1 else -1     # -1 = all
    compare = int(rest[2]) if len(rest) > 2 else 0    # 0=binary,1=text
    if compare == 1:
        # vbTextCompare: case-insensitive
        import re as _re
        flags = _re.IGNORECASE
        if count == -1:
            return _re.sub(_re.escape(find), repl, s[start:], flags=flags)
        return _re.sub(_re.escape(find), repl, s[start:], count=count, flags=flags)
    # vbBinaryCompare (default)
    if count == -1:
        return s[start:].replace(find, repl)
    result = s[start:]
    for _ in range(count):
        idx = result.find(find)
        if idx == -1:
            break
        result = result[:idx] + repl + result[idx+len(find):]
    return result

def _vbs_instr(*args):
    if len(args) == 2:
        start, s, sub, compare = 1, args[0], args[1], 0
    else:
        start, s, sub = args[0], args[1], args[2]
        compare = args[3] if len(args) > 3 else 0

    start = round(_numeric(start))        # VBS coerces to Long (banker's rounding)
    compare = int(_numeric(compare))
    if start < 1:
        raise ValueError('InStr start < 1 is a VBScript runtime error')
    if compare not in (0, 1):
        raise ValueError('unsupported compare mode')   # e.g. vbDatabaseCompare

    s, sub = str(s), str(sub)
    if s == '':          return 0        # string1 zero-length
    if start > len(s):   return 0        # start past end of string1
    if sub == '':        return start    # string2 zero-length -> start

    hay, needle = (s.lower(), sub.lower()) if compare == 1 else (s, sub)
    idx = hay.find(needle, start - 1)
    return idx + 1 if idx >= 0 else 0  # 1-indexed, 0 = not found

def _vbs_string(n, c):
    n = int(n)
    if isinstance(c, int):
        return chr(c) * n
    return str(c)[0] * n

# name.upper() -> (min_args, max_args, fn)
PURE_BUILTINS: dict[str, tuple[int, int, object]] = {
    'CHR':        (1, 1, _vbs_chr),
    'ASC':        (1, 1, _vbs_asc),
    'LEN':        (1, 1, _vbs_len),
    'UCASE':      (1, 1, _vbs_ucase),
    'LCASE':      (1, 1, _vbs_lcase),
    'TRIM':       (1, 1, _vbs_trim),
    'LTRIM':      (1, 1, _vbs_ltrim),
    'RTRIM':      (1, 1, _vbs_rtrim),
    'CSTR':       (1, 1, _vbs_cstr),
    'CINT':       (1, 1, _vbs_cint),
    'CDBL':       (1, 1, _vbs_cdbl),
    'CBOOL':      (1, 1, _vbs_cbool),
    'HEX':        (1, 1, _vbs_hex),
    'OCT':        (1, 1, _vbs_oct),
    'ABS':        (1, 1, _vbs_abs),
    'INT':        (1, 1, _vbs_int_fn),
    'FIX':        (1, 1, _vbs_fix),
    'SQR':        (1, 1, _vbs_sqr),
    'STRREVERSE': (1, 1, _vbs_strreverse),
    'SPACE':      (1, 1, _vbs_space),
    'MID':        (2, 3, _vbs_mid),
    'LEFT':       (2, 2, _vbs_left),
    'RIGHT':      (2, 2, _vbs_right),
    'REPLACE':    (3, 6, _vbs_replace),
    'INSTR':      (2, 4, _vbs_instr),
    'STRING':     (2, 2, _vbs_string),
}

# ---------------------------------------------------------------------------
# VBScript intrinsic (reserved) constants — always resolvable, never
# user-assignable, so recognised unconditionally like TRUE/FALSE/NOTHING
# rather than routed through the caller-supplied env.
# ---------------------------------------------------------------------------
_INTRINSIC_CONSTANTS: dict[str, Const] = {
    # String/char constants
    'VBCR':            '\r',
    'VBCRLF':          '\r\n',
    'VBFORMFEED':      '\f',
    'VBLF':            '\n',
    'VBNEWLINE':       '\r\n',
    'VBNULLCHAR':      '\0',
    'VBNULLSTRING':    '',
    'VBTAB':           '\t',
    'VBVERTICALTAB':   '\v',
    'VBBACK':          '\b',
    # Comparison-mode constants
    'VBBINARYCOMPARE': 0,
    'VBTEXTCOMPARE':   1,
    'VBDATABASECOMPARE': 2,
}

# ---------------------------------------------------------------------------
# Recursive-descent expression parser / evaluator
# ---------------------------------------------------------------------------

class _Parser:
    """Tiny recursive-descent parser over a token list.
    Skips WS tokens transparently. Returns None on any unrecognised form
    rather than raising — the caller treats None as "unresolvable"."""

    def __init__(self, tokens: list[VbsToken], env: dict[str, Const],
                 user_fns: dict[str, object] | None = None) -> None:
        self._tok = [t for t in tokens
                     if t.kind not in (TokenKind.WS, TokenKind.COMMENT,
                                       TokenKind.NEWLINE, TokenKind.LINECONT)]
        self._pos = 0
        self._env = {k.upper(): v for k, v in (env or {}).items()}
        self._user_fns = user_fns or {}

    def _peek(self) -> VbsToken | None:
        while self._pos < len(self._tok):
            return self._tok[self._pos]
        return None

    def _consume(self) -> VbsToken:
        t = self._tok[self._pos]
        self._pos += 1
        return t

    def _at_end(self) -> bool:
        return self._pos >= len(self._tok)

    # ------------------------------------------------------------------
    # Top-level entry
    # ------------------------------------------------------------------

    def parse(self) -> Const | None:
        result = self._expr()
        if result is None:
            return None
        if not self._at_end():
            return None   # leftover tokens — not a pure expression
        return result

    # ------------------------------------------------------------------
    # Grammar:  expr -> concat_expr
    #           concat_expr -> add_expr ('&' add_expr)*
    #           add_expr    -> mul_expr (('+' | '-') mul_expr)*
    #           mul_expr    -> unary (('*'|'/'|'\'|'Mod') unary)*
    #           unary       -> '-' unary | 'Not' unary | power
    #           power       -> atom ('^' unary)*
    #           atom        -> NUMBER | STRING | '(' expr ')' | call | ident
    # ------------------------------------------------------------------

    def _expr(self) -> Const | None:
        return self._concat()

    def _concat(self) -> Const | None:
        left = self._add()
        if left is None:
            return None
        while True:
            t = self._peek()
            if t is None or not (t.kind == TokenKind.OP and t.value == '&'):
                break
            self._consume()
            right = self._add()
            if right is None:
                return None
            left = str(left) + str(right)
        return left

    def _add(self) -> Const | None:
        left = self._mul()
        if left is None:
            return None
        while True:
            t = self._peek()
            if t is None or t.kind != TokenKind.OP or t.value not in ('+', '-'):
                break
            op = self._consume().value
            right = self._mul()
            if right is None:
                return None
            try:
                # VBS '+' is both numeric add and string concat when both sides are strings
                if op == '+':
                    if isinstance(left, str) and isinstance(right, str):
                        left = left + right
                    else:
                        left = _numeric(left) + _numeric(right)
                else:
                    left = _numeric(left) - _numeric(right)
            except Exception:
                return None
        return left

    def _mul(self) -> Const | None:
        left = self._unary()
        if left is None:
            return None
        while True:
            t = self._peek()
            if t is None:
                break
            if t.kind == TokenKind.OP and t.value in ('*', '/', '\\'):
                op = self._consume().value
            elif t.kind == TokenKind.IDENT and t.upper == 'MOD':
                op = 'MOD'
                self._consume()
            else:
                break
            right = self._unary()
            if right is None:
                return None
            try:
                ln, rn = _numeric(left), _numeric(right)
                if op == '*':
                    left = ln * rn
                elif op == '/':
                    left = ln / rn
                elif op == '\\':
                    left = int(ln) // int(rn)
                else:  # MOD
                    left = int(ln) % int(rn)
            except Exception:
                return None
        return left

    def _unary(self) -> Const | None:
        t = self._peek()
        if t is None:
            return None
        if t.kind == TokenKind.OP and t.value == '-':
            self._consume()
            v = self._unary()
            if v is None:
                return None
            try:
                return -_numeric(v)
            except Exception:
                return None
        if t.kind == TokenKind.IDENT and t.upper == 'NOT':
            self._consume()
            v = self._unary()
            if v is None:
                return None
            try:
                return int(not _truthy(v))  # VBS Not returns integer
            except Exception:
                return None
        return self._power()

    def _power(self) -> Const | None:
        left = self._atom()
        if left is None:
            return None
        while True:
            t = self._peek()
            if t is None or not (t.kind == TokenKind.OP and t.value == '^'):
                break
            self._consume()
            right = self._unary()
            if right is None:
                return None
            try:
                left = _numeric(left) ** _numeric(right)
            except Exception:
                return None
        return left

    def _atom(self) -> Const | None:
        t = self._peek()
        if t is None:
            return None

        # Parenthesised expression
        if t.kind == TokenKind.OP and t.value == '(':
            self._consume()
            v = self._expr()
            if v is None:
                return None
            close = self._peek()
            if close is None or not (close.kind == TokenKind.OP and close.value == ')'):
                return None
            self._consume()
            return v

        # Numeric literal
        if t.kind == TokenKind.NUMBER:
            self._consume()
            return _parse_number(t.value)

        # String literal
        if t.kind == TokenKind.STRING:
            self._consume()
            return _parse_string(t.value)

        # True / False / Nothing / Null / Empty
        if t.kind == TokenKind.IDENT:
            up = t.upper
            if up == 'TRUE':
                self._consume()
                return -1   # VBScript True is -1
            if up == 'FALSE':
                self._consume()
                return 0
            if up in ('NOTHING', 'NULL', 'EMPTY'):
                self._consume()
                return ''
            if up in _INTRINSIC_CONSTANTS:
                self._consume()
                return _INTRINSIC_CONSTANTS[up]

            # Function call or variable reference
            name = t.value
            self._consume()
            next_t = self._peek()
            if next_t is not None and next_t.kind == TokenKind.OP and next_t.value == '(':
                return self._call(name)
            # Variable lookup
            val = self._env.get(name.upper())
            if val is not None:
                return val
            return None

        return None

    def _call(self, name: str) -> Const | None:
        """Parse and evaluate a function call name(arg, arg, ...)."""
        self._consume()  # consume '('
        args: list[Const] = []
        # Empty arg list
        t = self._peek()
        if t is not None and t.kind == TokenKind.OP and t.value == ')':
            self._consume()
        else:
            while True:
                v = self._expr()
                if v is None:
                    return None
                args.append(v)
                t = self._peek()
                if t is None:
                    return None
                if t.kind == TokenKind.OP and t.value == ')':
                    self._consume()
                    break
                if t.kind == TokenKind.OP and t.value == ',':
                    self._consume()
                    continue
                return None

        # Try pure builtin
        entry = PURE_BUILTINS.get(name.upper())
        if entry is not None:
            min_a, max_a, fn = entry
            if min_a <= len(args) <= max_a:
                try:
                    return fn(*args)
                except Exception:
                    return None
            return None

        # Try user-defined inlinable function
        user_fn = self._user_fns.get(name.upper())
        if user_fn is not None:
            try:
                return user_fn(*args)
            except Exception:
                return None

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_number(s: str) -> int | float:
    s = s.rstrip('dDfFsSlLiIuU%')
    if s.upper().startswith('&H'):
        return int(s[2:], 16)
    if s.upper().startswith('&O'):
        return int(s[2:], 8)
    if '.' in s or 'e' in s.lower():
        return float(s)
    return int(s)


def _parse_string(s: str) -> str:
    """Un-escape a VBScript double-quoted string literal (including "" → ")."""
    inner = s[1:-1]          # strip outer quotes
    return inner.replace('""', '"')


def _numeric(v: Const) -> int | float:
    if isinstance(v, (int, float)):
        return v
    try:
        return int(v)
    except (ValueError, TypeError):
        return float(str(v))


def _truthy(v: Const) -> bool:
    if isinstance(v, (int, float)):
        return v != 0
    return bool(v)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_const(
    tokens: list[VbsToken],
    env: dict[str, Const] | None = None,
    user_fns: dict[str, object] | None = None,
) -> Const | None:
    """Try to evaluate *tokens* as a compile-time-constant VBScript expression.

    *env*      : {name_upper -> Const} of variables already resolved to constants.
    *user_fns* : {name_upper -> callable(*args) -> Const} of inlinable user functions.

    Returns the constant value, or None if the expression is not fully resolvable.
    Never raises.
    """
    try:
        return _Parser(tokens, env or {}, user_fns).parse()
    except Exception:
        return None
