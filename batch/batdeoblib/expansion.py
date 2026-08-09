"""The cmd.exe two-phase expansion model.

Every fold pass in this toolkit is a thin consumer of expand_run() /
expand_statement(). Nothing here executes the target script -- it is a pure
function of (tokens, env) built strictly from empirically-verified cmd.exe
semantics (see tokenizer.py's docstring and the toolkit README's
"Verification" section for the probes this was checked against).

Design contract: resolve only what can be *proven*; return Unresolved with a
machine-readable reason for anything else. Callers surface that reason
verbatim in their by_reason stats, the same convention PsFold-MethodChains
uses.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from .tokenizer import BatToken, TokenKind, tokenize
from .env import Env


@dataclass
class Expanded:
    text: str | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None

    @staticmethod
    def resolved(text: str) -> 'Expanded':
        return Expanded(text, None)

    @staticmethod
    def fail(reason: str) -> 'Expanded':
        return Expanded(None, reason)


_MOD_PATH_LETTERS = set('dpnx')
_MOD_FS_LETTERS = set('satz')   # requires real filesystem metadata -- never resolvable statically


def _split_modifier(inner: str) -> tuple[str, str | None]:
    """'NAME' -> (NAME, None); 'NAME:MOD' -> (NAME, MOD). The first ':' is the
    separator -- cmd variable names cannot themselves contain ':'."""
    if ':' not in inner:
        return inner, None
    name, mod = inner.split(':', 1)
    return name, mod


def _apply_substring(value: str, mod: str) -> Expanded:
    # mod is 'start' or 'start,len', both possibly negative/signed.
    m = re.match(r'^(-?\d+)(?:,(-?\d+))?$', mod[1:])
    if not m:
        return Expanded.fail('non-literal substring bounds')
    n = len(value)
    start = int(m.group(1))
    start = max(n + start, 0) if start < 0 else min(start, n)
    if m.group(2) is None:
        end = n
    else:
        length = int(m.group(2))
        end = max(n + length, start) if length < 0 else min(start + length, n)
    return Expanded.resolved(value[start:end])


def _apply_replace(value: str, mod: str) -> Expanded:
    star = mod.startswith('*')
    body = mod[1:] if star else mod
    if '=' not in body:
        return Expanded.fail('malformed search/replace modifier')
    find, repl = body.split('=', 1)
    if find == '':
        return Expanded.fail('empty search pattern')
    if star:
        idx = value.lower().find(find.lower())
        if idx == -1:
            return Expanded.resolved(value)
        return Expanded.resolved(repl + value[idx + len(find):])
    return Expanded.resolved(re.sub(re.escape(find), lambda _m: repl, value, flags=re.IGNORECASE))


def apply_var_modifier(value: str, mod: str | None) -> Expanded:
    """Apply a %NAME:MOD% / !NAME:MOD! modifier to an already-resolved value."""
    if mod is None:
        return Expanded.resolved(value)
    if mod.startswith('~'):
        return _apply_substring(value, mod)
    if '=' in mod:
        return _apply_replace(value, mod)
    return Expanded.fail(f'unrecognized modifier shape: {mod!r}')


def _apply_path_modifier(mods: str, raw_arg: str) -> Expanded:
    if any(c in _MOD_FS_LETTERS for c in mods):
        return Expanded.fail(f'path modifier requires filesystem metadata: {mods!r}')
    if mods == '':
        # bare %~1: value with one layer of surrounding quotes stripped
        v = raw_arg
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        return Expanded.resolved(v)
    letters = 'dpnx' if 'f' in mods else ''.join(c for c in mods if c in _MOD_PATH_LETTERS)
    v = raw_arg.strip('"').replace('/', '\\')
    drive = v[:2] if len(v) >= 2 and v[1] == ':' else ''
    rest = v[len(drive):]
    if '\\' in rest:
        path, _, name = rest.rpartition('\\')
        path = path + '\\'
    else:
        path, name = '', rest
    if '.' in name and not name.startswith('.'):
        base, ext = name.rsplit('.', 1)
        ext = '.' + ext
    else:
        base, ext = name, ''
    parts = {'d': drive, 'p': path, 'n': base, 'x': ext}
    return Expanded.resolved(''.join(parts[c] for c in letters))


def _resolve_pct_var(tok: BatToken, env: Env) -> Expanded:
    name, mod = _split_modifier(tok.inner or '')
    val = env.resolve_read(name)
    if val is None:
        return Expanded.fail(f'variable not statically resolvable: {name}')
    return apply_var_modifier(val, mod)


def _resolve_bang_var(tok: BatToken, env: Env) -> Expanded:
    name, mod = _split_modifier(tok.inner or '')
    val = env.resolve_read(name)
    if val is None:
        return Expanded.fail(f'variable not statically resolvable: {name}')
    return apply_var_modifier(val, mod)


def expand_run(tokens: list[BatToken], env: Env) -> Expanded:
    """Expand one already-sliced run of tokens (normally: everything between
    two statement boundaries) using *env*'s current bindings.

    Handles PCT_LIT / PCT_VAR / PCT_ARG / PCT_MODARG / PCT_UNMATCH always;
    BANG_CAND only when env.delayed_expansion is currently True (per
    empirical verification -- otherwise the raw `!name!` text passes through
    unchanged, exactly as cmd.exe's own parser does when the feature is
    off). CARET_ESC decodes to its escaped character (caret-splitting is
    transparent to *value*, even though bat_strip_carets.py is the pass that
    removes it from the *source text*). QUOTE/OP/TEXT/WS/NEWLINE pass
    through as literal characters -- quote-stripping for `set` assignments
    is command-shape-specific and lives in resolver.py, not here.
    """
    out: list[str] = []
    for tok in tokens:
        if tok.kind == TokenKind.PCT_LIT:
            out.append('%')
        elif tok.kind == TokenKind.PCT_UNMATCH:
            pass  # empirically verified: deleted, not left literal
        elif tok.kind == TokenKind.PCT_VAR:
            r = _resolve_pct_var(tok, env)
            if not r.ok:
                return r
            out.append(r.text)
        elif tok.kind == TokenKind.PCT_ARG:
            argname = '@ARGSTAR' if tok.inner == '*' else f'@ARG{tok.inner}'
            val = env.resolve_read(argname)
            if val is None:
                return Expanded.fail(f'positional arg not statically resolvable: %{tok.inner}')
            out.append(val)
        elif tok.kind == TokenKind.PCT_MODARG:
            body = tok.inner or ''
            m = re.match(r'^~([a-z]*)([0-9]|\*)$', body)
            if not m:
                return Expanded.fail(f'unsupported %~ form: {body!r}')
            mods, digit = m.group(1), m.group(2)
            argname = '@ARGSTAR' if digit == '*' else f'@ARG{digit}'
            raw = env.resolve_read(argname)
            if raw is None:
                return Expanded.fail(f'positional arg not statically resolvable: %~{body}')
            r = _apply_path_modifier(mods, raw)
            if not r.ok:
                return r
            out.append(r.text)
        elif tok.kind == TokenKind.BANG_CAND:
            if not env.delayed_expansion:
                out.append(tok.value)   # literal, unexpanded -- verified via probe
                continue
            r = _resolve_bang_var(tok, env)
            if not r.ok:
                return r
            out.append(r.text)
        elif tok.kind == TokenKind.CARET_ESC:
            # Empirically verified: caret suppresses GRAMMAR meaning (& | < > ( ) etc)
            # permanently, but has NO effect on percent/bang-phase eligibility -- a
            # caret-escaped % is still just as eligible to pair into a %...% reference
            # as a bare %, and cmd.exe's own well-known limitation is that you cannot
            # use ^ to block % expansion. Reproducing that pairing correctly requires
            # re-scanning past this token for a real or caret-escaped closing
            # delimiter, which this single-token-at-a-time function does not attempt.
            # Refuse rather than silently mis-resolve; bat_strip_carets.py (run first
            # in the recommended chain, exactly like PsStrip-Backticks) removes these
            # textually before any fold pass needs to reason about them.
            if tok.inner in ('%', '!'):
                return Expanded.fail('caret-escaped % or ! -- run bat_strip_carets first')
            out.append(tok.inner or '')
        elif tok.kind == TokenKind.LINECONT:
            pass
        else:  # TEXT, QUOTE, OP, WS, NEWLINE, LABEL, COMMENT, UNKNOWN
            out.append(tok.value)
    return Expanded.resolved(''.join(out))


def expand_statement(tokens: list[BatToken], env: Env, *, is_call: bool) -> Expanded:
    """expand_run() plus, when the statement is `call`-prefixed, cmd.exe's
    documented extra expansion round: the round-1 result is re-scanned for
    percent-expansion ONE more time (percent only -- delayed expansion is
    not re-applied). This is the mechanism behind `call set "X=%%!Y!%%"`-
    style indirect variable reads: round 1 collapses %% and resolves !Y!,
    producing e.g. %REALNAME%; round 2 (triggered by `call`) then expands
    that into REALNAME's actual value. Empirically verified against
    cmd.exe directly (see README).
    """
    r1 = expand_run(tokens, env)
    if not r1.ok or not is_call:
        return r1
    round2_tokens = tokenize(r1.text)
    # Round 2 is percent-only: run expand_run with delayed expansion forced
    # off so any literal '!' produced by round 1 is never reinterpreted.
    shadow = env.__class__(delayed_expansion=False)
    shadow.restore(env.snapshot())
    r2 = expand_run(round2_tokens, shadow)
    return r2
