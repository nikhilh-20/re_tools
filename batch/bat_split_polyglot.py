#!/usr/bin/env python3
"""Splits a polyglot / trailing-payload batch file into the part cmd.exe
actually executes and the embedded foreign script (PowerShell / VBScript /
JScript) that a second interpreter runs.

Two generic shapes, no marker string or sample name assumed:

  * Head polyglot: the file opens with `<#` (a PowerShell block comment that
    cmd.exe survives). The batch region is the `<# ... #>` prologue; the body
    after `#>` is the PowerShell payload.

  * Trailing payload: cmd.exe stops at the first reachable, top-level,
    unconditional `exit` / `exit /b` / `goto :eof`. Anything after that which
    does not itself look like batch is an embedded stage -- the `iex((gc ...)
    -replace '^.*__MARKER__','')` relaunch idiom, `powershell -Command "& {
    ... }"` here-strings, etc.

Writes `batch_region.cmd` (the cmd-executable prefix) + `stage_trailer.<ext>`
to --outdir with a JSON manifest. Never executes anything.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import read_source_text
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script
from batdeoblib.cfg import build_cfg
from bat_remove_deadcode import _compute_reachable, _first_word

_BATCHY = re.compile(r'(?im)^\s*(@?echo\s+(on|off)|@echo|setlocal|endlocal|goto\s|call\s+:|'
                     r'set\s+["/]|:[A-Za-z_]|if\s+(exist|defined|not|/i|")|for\s+/|rem\s|pushd\s|popd\s)')
_MARKER = re.compile(r'^\s*(__[A-Za-z0-9]+__|::[A-Za-z0-9_]+::|#{2,}[A-Za-z0-9_]+#{2,})\s*$')
_MIN_TRAILER_LINES = 3


def _guess_ext(body: str) -> str:
    head = body[:600]
    if re.search(r'(?i)\bDim\b|\bWScript\b|CreateObject\(|\bSet\s+\w+\s*=\s*|\bWend\b|\bMsgBox\b', head):
        return '.vbs'
    if re.search(r'\$[A-Za-z_{]|function\s+[A-Za-z]|param\s*\(|\[[A-Za-z.]+\]::|Invoke-|-Enc(odedCommand)?\b', head):
        return '.ps1'
    if re.search(r'\bvar\s+\w+\s*=|=>|function\s*\(|console\.', head):
        return '.js'
    return '.txt'


def _first_nonblank(lines):
    for i, ln in enumerate(lines):
        if ln.strip():
            return i, ln
    return None, ''


def _emit(outdir: Path, batch_region: str, trailer: str, origin: str, marker):
    outdir.mkdir(parents=True, exist_ok=True)
    lines = trailer.splitlines(keepends=True)
    idx, first = _first_nonblank(lines)
    if idx is not None and _MARKER.match(first):
        marker = first.strip()
        lines = lines[idx + 1:]
        trailer = ''.join(lines)
    trailer = trailer.lstrip('\r\n')

    non_blank = [ln for ln in trailer.splitlines() if ln.strip()]
    if len(non_blank) < _MIN_TRAILER_LINES or _BATCHY.match(trailer):
        return {'stages': [], 'note': 'no foreign trailer (short, or looks like batch)'}

    ext = _guess_ext(trailer)
    (outdir / f'stage_trailer{ext}').write_text(trailer, encoding='utf-8', newline='')
    (outdir / 'batch_region.cmd').write_text(batch_region, encoding='utf-8', newline='')
    entry = {'file': f'stage_trailer{ext}', 'origin': origin, 'bytes': len(trailer)}
    if marker:
        entry['marker'] = marker
    return {'stages': [entry]}


def split_polyglot(text: str, outdir: Path) -> dict:
    # --- head polyglot: <# ... #> prologue ---
    if text.lstrip()[:2] == '<#':
        close = text.find('#>')
        if close != -1:
            after = close + 2
            return _emit(outdir, text[:after], text[after:],
                         'PowerShell body after the <# ... #> polyglot prologue', None)

    # --- trailing payload after the batch region's EOF ---
    tree = parse_script(tokenize(text))
    cfg = build_cfg(tree)
    reachable, _info = _compute_reachable(cfg)
    stmts = cfg.statements

    cut = None
    for idx, s in enumerate(stmts):
        if idx not in reachable or s.in_block:
            continue
        w = _first_word(s)
        g = next((e for e in cfg.gotos if e.index == idx), None)
        is_exit = w == 'EXIT'
        is_goto_eof = w == 'GOTO' and g is not None and g.target == 'EOF'
        if is_exit or is_goto_eof:
            cut = s.end   # s.end already includes the statement's terminating newline
            break

    if cut is None or cut >= len(text):
        return {'stages': [], 'note': 'no reachable top-level exit / goto :eof'}

    return _emit(outdir, text[:cut], text[cut:],
                 f'bytes after the batch region ends (offset {cut})', None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--outdir', required=True)
    args = ap.parse_args()
    src = read_source_text(args.input)
    print(json.dumps(split_polyglot(src, Path(args.outdir)), indent=2))


if __name__ == '__main__':
    main()
