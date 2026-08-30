"""Virtual-filesystem model of output redirection.

Malware routinely assembles a payload FILE on disk with hundreds or thousands
of `echo <chunk>>>"%TARGET%"` lines and then decodes it (`certutil -decode`,
a PowerShell `[Convert]::FromBase64String((Get-Content ...))`, ...). The rest
of this toolkit only ever inspects *variables*, so that payload is invisible.

`scan_redirections` walks a parsed tree and reconstructs, for every
redirection target it can pin down, the byte content that would land in it --
modelling only cmd.exe's documented `>` / `>>` semantics (truncate / append)
plus `del` (remove). It never executes anything.

Target and chunk text are resolved with a *symbolic* variable table: a
`%VAR%` whose value the script sets is substituted (recursively); an ambient
`%TEMP%` / `%APPDATA%` that the script never assigns is kept verbatim, so the
echo lines and a later `certutil`/PowerShell reader that use the same
expression still key to the same file. First-principles: no target name,
chunk size, or marker string is assumed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tokenizer import tokenize, TokenKind
from .statements import parse_script, Statement, Block

_VARREF = re.compile(r'%([^%\r\n:]+?)(?::[^%\r\n]*)?%')
# DOS device names -- a `>nul` / `2>nul` / `>con` is a discard, not a file.
_DEVICES = re.compile(r'(?i)^(nul|con|prn|aux|clock\$|lpt\d|com\d)\d*$')


@dataclass
class VirtualFile:
    path: str                       # the resolved (possibly symbolic) target
    lines: list[str] = field(default_factory=list)   # current accumulation
    best: list[str] = field(default_factory=list)    # high-water content ever held
    resolved: bool = True           # target path fully literal (no residual %VAR%)
    partial: bool = False           # >= 1 contributing echo could not be resolved
    writes: int = 0

    def _bump(self):
        if len(self.lines) > len(self.best):
            self.best = list(self.lines)

    def text(self, newline: str = '\r\n') -> str:
        # a script that `del`s or re-truncates the file AFTER decoding it
        # (cleanup) must not erase what we reconstructed -- emit the largest
        # content the target ever held.
        content = self.best if len(self.best) >= len(self.lines) else self.lines
        return newline.join(content) + (newline if content else '')

    @property
    def line_count(self) -> int:
        return max(len(self.lines), len(self.best))


def _flatten(nodes):
    for nd in nodes:
        if isinstance(nd, Statement):
            yield nd
        elif isinstance(nd, Block):
            yield from _flatten(nd.body)


def _tok_text(tokens) -> str:
    return ''.join(t.value for t in tokens
                   if t.kind not in (TokenKind.NEWLINE, TokenKind.COMMENT))


def _expand_sym(s: str, sym: dict[str, str], depth: int = 0) -> str:
    """Substitute %VAR% using *sym*; leave an unknown %VAR% verbatim."""
    if depth > 12 or '%' not in s:
        return s
    def repl(m):
        name = m.group(1).strip().upper()
        if name in sym:
            return _expand_sym(sym[name], sym, depth + 1)
        return m.group(0)
    return _VARREF.sub(repl, s)


def pathkey(s: str) -> str:
    return s.strip().strip('"').replace('/', '\\').casefold()


# kept as a module name other code imported
_pathkey = pathkey


def _set_assignment(stmt: Statement):
    """(NAME, VALUE) for a `set "NAME=VALUE"` / `set NAME=VALUE`; else None.
    /a and /p are not string assignments for our purposes."""
    ct = stmt.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT or ct[0].value.lstrip('@').upper() != 'SET':
        return None
    rest = ct[1:]
    if rest and rest[0].kind == TokenKind.TEXT and rest[0].value[:2].lower() in ('/a', '/p'):
        return None
    wraps = rest and rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
    body = rest[1:-1] if wraps else rest
    txt = ''.join(t.value for t in body)
    if '=' not in txt:
        return None
    name, val = txt.split('=', 1)
    name = name.strip()
    return (name.upper(), val) if name else None


def _redir_split(code_toks):
    """(cmd_tokens, op, target_text) at the first stdout `>` / `>>`, or None.
    `2>` / `2>>` (stderr) returns None."""
    for i, t in enumerate(code_toks):
        if t.kind != TokenKind.OP or t.value not in ('>', '>>'):
            continue
        if i and code_toks[i - 1].kind == TokenKind.TEXT and code_toks[i - 1].value in ('1', '2'):
            if code_toks[i - 1].value == '2':
                return None
            cmd = code_toks[:i - 1]
        else:
            cmd = code_toks[:i]
        target = code_toks[i + 1:]
        for j, tt in enumerate(target):
            if tt.kind == TokenKind.OP:
                target = target[:j]
                break
        return cmd, t.value, _tok_text(target).strip().strip('"')
    return None


def _echo_payload(stmt: Statement) -> str | None:
    """The literal text an `echo` command would print, up to the first `>`.
    None if the command is not an `echo`. `echo.`/`echo(`/`echo:` -> ''."""
    ct = stmt.code_tokens()
    if not ct or ct[0].kind != TokenKind.TEXT:
        return None
    head = ct[0].value.lstrip('@')
    if head[:4].upper() != 'ECHO':
        return None
    suffix = head[4:]
    # collect full (WS-inclusive) tokens after the echo head, up to `>` / `>>`
    toks = []
    for t in stmt.tokens:
        if t.kind in (TokenKind.NEWLINE, TokenKind.COMMENT):
            continue
        if t.kind == TokenKind.OP and t.value in ('>', '>>'):
            break
        toks.append(t)
    # drop the echo head token itself
    if toks and toks[0].kind == TokenKind.TEXT and toks[0].value.lstrip('@')[:4].upper() == 'ECHO':
        toks = toks[1:]
    if suffix in ('.', ':', '(', '/', '\\', ';', ','):
        return _tok_text(toks)
    if suffix == '':
        # `echo <space> text` : cmd drops exactly one delimiter
        if toks and toks[0].kind == TokenKind.WS:
            toks = toks[1:]
        return _tok_text(toks) if toks else None
    return None


def build_symbolic_table(tree) -> dict[str, str]:
    """name -> symbolic value for every `set` string assignment, with %VAR%
    refs resolved through earlier entries and unknown/ambient %VAR% kept."""
    sym: dict[str, str] = {}
    for stmt in _flatten(tree):
        asn = _set_assignment(stmt)
        if asn is None:
            continue
        name, val = asn
        if val == '':
            sym.pop(name, None)
        else:
            sym[name] = _expand_sym(val, sym)
    return sym


def resolve_symbolic(s: str, sym: dict[str, str]) -> str:
    return _expand_sym(s, sym)


def scan_redirections(tree_or_text) -> dict[str, VirtualFile]:
    """Accepts a parsed tree (list of Statement/Block) or raw source text.
    Returns {pathkey: VirtualFile}."""
    if isinstance(tree_or_text, str):
        tree = parse_script(tokenize(tree_or_text))
    else:
        tree = tree_or_text

    sym: dict[str, str] = {}
    files: dict[str, VirtualFile] = {}

    for stmt in _flatten(tree):
        ct = stmt.code_tokens()
        if not ct:
            continue
        first = ct[0].value.lstrip('@').upper() if ct[0].kind == TokenKind.TEXT else ''

        asn = _set_assignment(stmt)
        if asn is not None:
            name, val = asn
            if val == '':
                sym.pop(name, None)
            else:
                sym[name] = _expand_sym(val, sym)
            continue

        if first == 'DEL':
            # A `del` clears the *current* accumulation (so a later re-echo
            # starts fresh) but keeps the high-water content -- a cleanup
            # `del` after the payload was already decoded must not erase it.
            arg = _expand_sym(_tok_text(ct[1:]).strip(), sym)
            for piece in arg.split():
                dvf = files.get(pathkey(piece.strip('"')))
                if dvf is not None:
                    dvf._bump()
                    dvf.lines = []
            continue

        split = _redir_split(ct)
        if split is None:
            continue
        _cmd, op, target_raw = split
        if not target_raw or '!' in target_raw:
            continue
        target = _expand_sym(target_raw, sym).strip().strip('"')
        if not target:
            continue
        base = target.replace('/', '\\').rsplit('\\', 1)[-1]
        if _DEVICES.match(base):
            continue   # `>nul` etc. is a discard, not a payload file
        key = pathkey(target)
        vf = files.get(key)
        if vf is None:
            vf = files[key] = VirtualFile(path=target, resolved='%' not in target)
        vf.writes += 1
        if op == '>':
            vf._bump()
            vf.lines = []
            vf.partial = False

        payload = _echo_payload(stmt)
        if payload is None:
            vf.partial = True          # non-echo writer -- content not modelled
            continue
        line = _expand_sym(payload, sym)
        if '%' in line or '!' in line:
            vf.partial = True
        else:
            vf.lines.append(line)
            vf._bump()

    return files
