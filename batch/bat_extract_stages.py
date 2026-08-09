#!/usr/bin/env python3
"""Writes each recovered embedded stage to its own file, plus a JSON
manifest describing where each came from. The handoff utility that lets the
PowerShell/VBS toolkits pick up a dropped stage directly, instead of
copy-pasting a decoded blob out by hand.

Deviates from the --input/--output convention (like vbs_extract_variables.py):
takes --input and --outdir, writes N stage files, and prints a JSON manifest
to stdout. Never executes anything, including the recovered stages.

Two recovery sources, both GENERIC techniques rather than tied to any one
sample's shape:
  - `-EncodedCommand` / `-enc` arguments to `powershell`/`pwsh` anywhere in
    the script -- base64 + UTF-16LE decoded (PowerShell's own documented
    -EncodedCommand format) and written out as a .ps1 stage.
  - Statically-known base64 string variables (same detection
    bat_decode_blobs.py uses) that decode to printable text -- written out
    with a .txt extension by default, or .ps1/.vbs when the decoded content
    is unambiguously recognizable as one (a `#!`/`param(`/`$`-heavy prelude
    for PowerShell, or a `WScript.`/`CreateObject` prelude for VBScript).
"""
from __future__ import annotations
import argparse
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.tokenizer import tokenize
from batdeoblib.statements import parse_script
from bat_decode_blobs import _collect_known_vars, _B64_RE, _printable_ratio, _MIN_LEN


def _guess_extension(text: str) -> str:
    head = text[:400]
    if re.search(r'(?i)\bWScript\.|CreateObject\(', head):
        return '.vbs'
    if re.search(r'\$[A-Za-z_]|param\s*\(|Invoke-', head):
        return '.ps1'
    return '.txt'


def _find_encoded_command_args(text: str) -> list[tuple[str, int]]:
    """Find `-EncodedCommand <base64>` / `-enc <base64>` style arguments.
    Operates at the string level over already-expanded/foldable source --
    if the base64 argument is itself a %VAR% reference, run
    bat_propagate_constants.py first so it appears as a literal here."""
    out = []
    for m in re.finditer(r'(?i)-e(?:nc(?:odedcommand)?)?\s+([A-Za-z0-9+/=]{20,})', text):
        out.append((m.group(1), m.start()))
    return out


def extract_stages(text: str, outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    stages = []
    n = 0

    for b64, offset in _find_encoded_command_args(text):
        try:
            raw = base64.b64decode(b64 + '=' * (-len(b64) % 4))
            decoded = raw.decode('utf-16-le')
        except Exception:
            continue
        if _printable_ratio(decoded.encode('latin-1', errors='ignore')) < 0.8:
            continue
        n += 1
        fname = f'stage{n}.ps1'
        (outdir / fname).write_text(decoded, encoding='utf-8')
        stages.append({'file': fname, 'origin': f'-EncodedCommand argument at offset {offset}',
                        'decoder': 'base64/utf16le'})

    tokens = tokenize(text)
    tree = parse_script(tokens)
    known = _collect_known_vars(tree)
    for name, val in known.items():
        if len(val) < _MIN_LEN or not _B64_RE.match(val):
            continue
        try:
            raw = base64.b64decode(val, validate=True)
        except Exception:
            continue
        if _printable_ratio(raw) < 0.85:
            continue
        decoded = raw.decode('latin-1')
        ext = _guess_extension(decoded)
        n += 1
        fname = f'stage{n}{ext}'
        (outdir / fname).write_text(decoded, encoding='utf-8')
        stages.append({'file': fname, 'origin': f'base64 literal held in variable {name}',
                        'decoder': 'base64'})

    return {'stages': stages}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--outdir', required=True)
    args = ap.parse_args()

    src = Path(args.input).read_text(encoding='utf-8-sig', errors='replace')
    manifest = extract_stages(src, Path(args.outdir))
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
