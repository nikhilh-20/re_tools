"""CLI harness for Batch deobfuscation tools.

Every tool calls run_tool(main_fn, extra_args) which handles:
  --input FILE   (required)
  --output FILE  (required for most tools)
  --aggressive   (optional flag)
  Extra per-tool args defined in extra_args.

On success: writes transformed text to --output and prints a compact JSON
stats dict to stdout (mirrors the PS/VBS toolkits' --input/--output + JSON
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


_BOM_TABLE = [
    (b'\xff\xfe\x00\x00', 'utf-32-le'),   # must precede utf-16-le: shares its FF FE prefix
    (b'\x00\x00\xfe\xff', 'utf-32-be'),
    (b'\xef\xbb\xbf',     'utf-8-sig'),   # codec strips this BOM itself
    (b'\xff\xfe',         'utf-16-le'),
    (b'\xfe\xff',         'utf-16-be'),
]


def read_source_text(path) -> str:
    """Decode a batch source file, sniffing a BOM to pick the right encoding.

    Obfuscated .bat/.cmd drops are sometimes saved as UTF-16 (cmd.exe doesn't
    care), which a hardcoded utf-8-sig read silently mangles into
    NUL-interleaved garbage that no downstream tokenizer can parse -- every
    pass then reports zero changes with no error. Detection is BOM-based only
    (deterministic); a UTF-16 file without a BOM is still read as UTF-8, same
    as before this function existed. Every decode uses errors='replace' so a
    malformed file never raises."""
    data = Path(path).read_bytes()
    for bom, encoding in _BOM_TABLE:
        if data.startswith(bom):
            if encoding == 'utf-8-sig':
                return data.decode(encoding, errors='replace')
            return data[len(bom):].decode(encoding, errors='replace')
    return data.decode('utf-8-sig', errors='replace')


def run_tool(
    fn: Callable[..., tuple[str, dict[str, Any]]],
    description: str = '',
    extra_args: list[dict] | None = None,
    *,
    analysis_only: bool = False,   # True for extract-variables (no --output)
) -> None:
    """Parse CLI args, call fn(text, **kwargs) -> (new_text, stats), emit results."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--input', required=True, metavar='FILE', help='Input batch file')
    if not analysis_only:
        parser.add_argument('--output', required=True, metavar='FILE', help='Output batch file')
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

    src = read_source_text(args.input)

    try:
        new_text, stats = fn(src, **kwargs)
    except Exception:
        msg = f'ERROR: {traceback.format_exc().splitlines()[-1]}'
        if not analysis_only:
            # newline='' -- never let text-mode write translate \n to os.linesep;
            # a source already carrying \r\n would otherwise gain one \r per pass.
            Path(args.output).write_text(msg, encoding='utf-8', newline='')
        print(msg, file=sys.stderr)
        sys.exit(1)

    if analysis_only:
        print(json.dumps(stats, indent=2))
    else:
        Path(args.output).write_text(new_text, encoding='utf-8', newline='')
        stats['input_bytes'] = len(src.encode('utf-8'))
        stats['output_bytes'] = len(new_text.encode('utf-8'))
        stats['output_path'] = str(args.output)
        print(json.dumps(stats, separators=(',', ':')))


def apply_edits(src: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply a list of (start, end, replacement) edits to *src*.

    Rebuilt in a single left-to-right pass (sort ascending, slice between edit
    boundaries, join) -- O(n + total edit text), not the O(edits x filesize) a
    repeated ``src = src[:s] + r + src[e:]`` splice would cost on a multi-MB
    dropper with thousands of fold sites.

    Nested edits are resolved the way the old right-to-left splice did: an
    edit fully contained in another's span is dropped, the outer one wins
    (e.g. a pass that inlines `%A%%B%` into an assignment AND then deletes the
    whole now-dead assignment). A *partial* overlap has no well-defined
    meaning and raises.
    """
    if not edits:
        return src
    out: list[str] = []
    cursor = 0
    for start, end, repl in sorted(set(edits), key=lambda e: (e[0], -(e[1] - e[0]))):
        if start < cursor:
            if end <= cursor:
                continue   # fully inside an edit already applied -- outer wins
            raise ValueError(f'partially overlapping edit at offset {start} (cursor at {cursor})')
        out.append(src[cursor:start])
        out.append(repl)
        cursor = end
    out.append(src[cursor:])
    return ''.join(out)
