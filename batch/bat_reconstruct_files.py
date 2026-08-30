#!/usr/bin/env python3
"""Reconstructs a payload file that a script assembles on disk with a run of
`echo <chunk>>>"%TARGET%"` lines, then (optionally) decodes it exactly the
way the script itself does -- `certutil -decode` / `-decodehex`, or a plain
base64/hex body -- and writes the result to --outdir with a JSON manifest.

Deviates from the --input/--output convention (like bat_extract_stages.py):
takes --input and --outdir, writes N files, prints a manifest to stdout.
Never executes anything.

First-principles: models cmd.exe's documented `>`/`>>` redirection and
certutil's documented decode verbs; no target name, chunk size, or marker
string is assumed. Run the folding passes first so `%TARGET%` and the echoed
`%VAR%` chunks are already literals.
"""
from __future__ import annotations
import argparse
import base64
import binascii
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import read_source_text
from batdeoblib.tokenizer import tokenize
from batdeoblib.statements import parse_script
from batdeoblib.redirect import (scan_redirections, pathkey,
                                 build_symbolic_table, resolve_symbolic)

_MIN_LINES = 8
_MIN_BYTES = 256
_PEM_LINE = re.compile(r'^-{5}(BEGIN|END) [A-Z0-9 ]+-{5}\s*$')
_B64_BODY = re.compile(r'^[A-Za-z0-9+/\r\n]+={0,2}\s*$')
_HEX_BODY = re.compile(r'^[0-9A-Fa-f\r\n]+$')


def _strip_pem(text: str) -> tuple[str, bool]:
    kept = [ln for ln in text.splitlines() if not _PEM_LINE.match(ln)]
    stripped = '\n'.join(kept)
    return stripped, stripped != text


def _guess_ext(raw: bytes) -> str:
    if raw[:2] == b'MZ':
        return '.bin'
    if raw[:4] == b'PK\x03\x04':
        return '.zip'
    head = raw[:400].decode('latin-1', errors='replace')
    if re.search(r'(?i)\bWScript\.|CreateObject\(|^\s*<\?xml|<job\b|<package\b', head):
        return '.vbs'
    if re.search(r'\$[A-Za-z_]|param\s*\(|function\s+[A-Za-z]|Invoke-', head):
        return '.ps1'
    try:
        raw.decode('utf-8')
        return '.txt'
    except UnicodeDecodeError:
        return '.bin'


def _find_certutil_decoders(text: str, keys: set[str], sym: dict[str, str]) -> dict[str, str]:
    """{pathkey: 'decode'|'decodehex'} for every `certutil ... -decode[hex]
    <in> <out>` whose <in> matches a reconstructed target (after symbolic
    %VAR% expansion, so `certutil -decode "%B64FILE%" ...` pairs with the
    echo lines that wrote `"%B64FILE%"`)."""
    out: dict[str, str] = {}
    for m in re.finditer(r'(?im)\bcertutil(?:\.exe)?\b(.*)$', text):
        argline = m.group(1)
        vm = re.search(r'(?i)-?(decodehex|decode)\b', argline)
        if not vm:
            continue
        verb = vm.group(1).lower()
        rest = argline[vm.end():]
        for tk in re.findall(r'"[^"]*"|\S+', rest):
            tk = tk.strip('"')
            if tk.startswith(('-', '/')):
                continue
            k = pathkey(resolve_symbolic(tk, sym))
            if k in keys:
                out[k] = verb
            break
    return out


def reconstruct_files(text: str, outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    tree = parse_script(tokenize(text))
    vfiles = scan_redirections(tree)
    sym = build_symbolic_table(tree)
    certutil = _find_certutil_decoders(text, set(vfiles), sym)

    stages, skipped = [], []
    n = 0
    for key, vf in vfiles.items():
        body_text = vf.text(newline='\r\n')
        if vf.partial:
            n += 1
            fn = f'reconstructed_{n}.partial.txt'
            (outdir / fn).write_text(body_text, encoding='utf-8', newline='')
            stages.append({'file': fn, 'origin': f'{vf.writes} redirections -> {vf.path}',
                           'decoder': 'none', 'bytes': len(body_text), 'partial': True})
            continue
        if vf.line_count < _MIN_LINES and len(body_text) < _MIN_BYTES:
            skipped.append({'target': vf.path, 'reason': 'too small'})
            continue

        stripped, had_pem = _strip_pem(body_text)
        compact = re.sub(r'\s+', '', stripped)
        verb = certutil.get(key)
        raw, decoder = None, 'raw'
        try:
            if verb == 'decode' or (verb is None and had_pem and _B64_BODY.match(stripped)):
                raw = base64.b64decode(compact + '=' * (-len(compact) % 4))
                decoder = 'certutil-decode (pem-strip+base64)' if verb else 'base64 (pem-armored)'
            elif verb == 'decodehex' or (verb is None and _HEX_BODY.match(stripped) and len(compact) % 2 == 0):
                raw = binascii.unhexlify(compact)
                decoder = 'certutil-decodehex' if verb else 'hex'
            elif verb is None and _B64_BODY.match(stripped) and len(compact) >= 16:
                raw = base64.b64decode(compact + '=' * (-len(compact) % 4))
                decoder = 'base64 (candidate)'
        except (binascii.Error, ValueError):
            raw = None

        if raw is None:
            n += 1
            fn = f'reconstructed_{n}.txt'
            (outdir / fn).write_text(body_text, encoding='utf-8', newline='')
            stages.append({'file': fn, 'origin': f'{vf.writes} redirections -> {vf.path}',
                           'decoder': 'raw', 'bytes': len(body_text), 'partial': False})
            continue

        n += 1
        fn = f'reconstructed_{n}{_guess_ext(raw)}'
        (outdir / fn).write_bytes(raw)
        stages.append({'file': fn,
                       'origin': f'{vf.writes} echo redirections -> {vf.path}',
                       'decoder': decoder, 'bytes': len(raw), 'partial': False})

    return {'stages': stages, 'skipped': skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--outdir', required=True)
    args = ap.parse_args()
    src = read_source_text(args.input)
    print(json.dumps(reconstruct_files(src, Path(args.outdir)), indent=2))


if __name__ == '__main__':
    main()
