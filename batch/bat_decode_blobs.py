#!/usr/bin/env python3
"""Decodes an encoded data blob held in a statically-known string variable.
The Batch analogue of PsInline-Base64 / PsDecode-ByteArray.

Two independent decoders, each firing only when it can PROVE the encoding
from the script itself (never executes anything, including certutil):

  --mode base64 (default): any Known variable whose value is plausibly
    base64 (correct charset, length a multiple of 4 once padding is
    counted) is decoded. If the result is printable text it's inlined as a
    literal (as a comment annotation, since inlining raw bytes into batch
    source safely would need re-escaping this pass doesn't attempt);
    otherwise it's reported as non-printable and left alone.

  --mode xor-hex --key N: decodes a Known variable holding a pure hex
    string (even length, hex digits only) by XORing each byte with the
    given single-byte key N. This is a GENERIC single-byte-XOR-over-hex
    decoder, not tied to any particular sample's variable names or loop
    shape -- but it does need the key told to it explicitly. Auto-
    discovering which of possibly many `set /a ... ^ ...` expressions
    elsewhere in a script is "the" key for a given hex blob is exactly the
    kind of sample-shape-specific guess this toolkit's design principle
    rules out; a human (or bat_extract_variables.py's report) supplies the
    key once they've identified it, the same way a reverse engineer would.

Analysis-only in spirit even though it writes output: it reports EVERY
candidate blob it finds via `candidates` in the stats, and only rewrites
the source (annotating with a decoded-comment, never replacing code) when
the result is printable. Nothing is ever executed.
"""
from __future__ import annotations
import base64
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool
from batdeoblib.tokenizer import tokenize
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate
from batdeoblib.env import Env, VState

_B64_RE = re.compile(r'^[A-Za-z0-9+/]+={0,2}$')
_HEX_RE = re.compile(r'^[0-9A-Fa-f]+$')
_MIN_LEN = 16


def _printable_ratio(b: bytes) -> float:
    if not b:
        return 0.0
    good = sum(1 for c in b if 32 <= c < 127 or c in (9, 10, 13))
    return good / len(b)


def _collect_known_vars(tree) -> dict[str, str]:
    """Run the straight-line simulation to completion and return the FINAL
    known value of every variable (last assignment wins)."""
    env = Env()
    for _step in simulate(tree, env):
        pass
    values: dict[str, str] = {}
    for name, v in env.snapshot().items():
        if v.state == VState.KNOWN and v.value:
            values[name] = v.value
    return values


def decode_blobs(text: str, *, mode: str = 'base64', key: int = -1, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    known = _collect_known_vars(tree)

    candidates = []
    annotations: list[str] = []

    def _already(name: str) -> bool:
        # idempotency: don't re-append an annotation this pass already added
        # (the `set` line stays Known every run, so without this the chain
        # never converges).
        return f'rem <<<DECODED {name} (' in text

    if mode == 'base64':
        for name, val in known.items():
            if len(val) < _MIN_LEN or not _B64_RE.match(val) or _already(name):
                continue
            try:
                raw = base64.b64decode(val, validate=True)
            except Exception:
                continue
            ratio = _printable_ratio(raw)
            entry = {'variable': name, 'encoding': 'base64', 'length': len(val), 'printable_ratio': round(ratio, 3)}
            if ratio >= 0.85:
                preview = raw.decode('latin-1')
                entry['decoded_preview'] = preview[:200]
                annotations.append(f'rem <<<DECODED {name} (base64)>>>\nrem > {preview[:500]!r}\nrem <<<END>>>\n')
            candidates.append(entry)
    elif mode == 'xor-hex':
        if not (0 <= key <= 255):
            return text, {'changed': 0, 'candidates': [], 'error': '--key required (0-255) for --mode xor-hex'}
        for name, val in known.items():
            if len(val) < _MIN_LEN or len(val) % 2 != 0 or not _HEX_RE.match(val) or _already(name):
                continue
            raw = bytes(int(val[i:i + 2], 16) ^ key for i in range(0, len(val), 2))
            ratio = _printable_ratio(raw)
            entry = {'variable': name, 'encoding': 'xor-hex', 'key': key, 'length': len(val), 'printable_ratio': round(ratio, 3)}
            if ratio >= 0.85:
                preview = raw.decode('latin-1')
                entry['decoded_preview'] = preview[:200]
                annotations.append(f'rem <<<DECODED {name} (xor-hex key={key})>>>\nrem > {preview[:500]!r}\nrem <<<END>>>\n')
            candidates.append(entry)
    else:
        return text, {'changed': 0, 'candidates': [], 'error': f'unknown mode: {mode}'}

    new_text = text
    if annotations:
        new_text = text.rstrip('\n') + '\n\n' + '\n'.join(annotations)

    return new_text, {'changed': len(annotations), 'candidates': candidates}


if __name__ == '__main__':
    run_tool(
        decode_blobs,
        description='Decode a statically-known base64 or single-byte-XOR-over-hex blob.',
        extra_args=[
            {'flags': ['--mode'], 'choices': ['base64', 'xor-hex'], 'default': 'base64'},
            {'flags': ['--key'], 'type': int, 'default': -1, 'help': 'single-byte XOR key (0-255), required for --mode xor-hex'},
        ],
    )
