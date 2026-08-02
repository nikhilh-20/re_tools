"""CLI harness for VBS deobfuscation tools.

Every tool calls run_tool(main_fn, extra_args) which handles:
  --input FILE   (required)
  --output FILE  (required for most tools)
  --aggressive   (optional flag)
  Extra per-tool args defined in extra_args.

On success: writes transformed text to --output and prints a compact JSON
stats dict to stdout (mirrors the PS toolkit's -InputFile/-OutputFile + JSON
stats convention).

On failure: writes "ERROR: <message>" to --output and exits non-zero.
"""
from __future__ import annotations
import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Callable, Any


def run_tool(
    fn: Callable[..., tuple[str, dict[str, Any]]],
    description: str = '',
    extra_args: list[dict] | None = None,
    *,
    analysis_only: bool = False,   # True for extract-variables (no --output)
) -> None:
    """Parse CLI args, call fn(text, **kwargs) -> (new_text, stats), emit results."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--input',  required=True,  metavar='FILE', help='Input VBS file')
    if not analysis_only:
        parser.add_argument('--output', required=True, metavar='FILE', help='Output VBS file')
    parser.add_argument('--aggressive', action='store_true', default=False,
                        help='Enable wider (but still safe) transforms')
    for spec in (extra_args or []):
        flags = spec.pop('flags')
        parser.add_argument(*flags, **spec)
        # restore for re-use
        spec['flags'] = flags

    args = parser.parse_args()
    kwargs: dict[str, Any] = {k: v for k, v in vars(args).items()
                               if k not in ('input', 'output')}

    src = Path(args.input).read_text(encoding='utf-8-sig', errors='replace')

    try:
        new_text, stats = fn(src, **kwargs)
    except Exception:
        msg = f'ERROR: {traceback.format_exc().splitlines()[-1]}'
        if not analysis_only:
            Path(args.output).write_text(msg, encoding='utf-8')
        print(msg, file=sys.stderr)
        sys.exit(1)

    if analysis_only:
        print(json.dumps(stats, indent=2))
    else:
        Path(args.output).write_text(new_text, encoding='utf-8')
        stats['input_bytes']  = len(src.encode('utf-8'))
        stats['output_bytes'] = len(new_text.encode('utf-8'))
        stats['output_path']  = str(args.output)
        print(json.dumps(stats, separators=(',', ':')))


def apply_edits(src: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply a list of (start, end, replacement) edits to *src*.

    Edits must be non-overlapping.  Applied right-to-left so earlier offsets
    remain valid while later ones are patched first.
    """
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        src = src[:start] + repl + src[end:]
    return src


def quote_vbs(s: str) -> str:
    """Return *s* as a VBScript double-quoted string literal."""
    return '"' + s.replace('"', '""') + '"'


def format_number(v: int | float) -> str:
    """Format a numeric constant back to VBScript source."""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)
