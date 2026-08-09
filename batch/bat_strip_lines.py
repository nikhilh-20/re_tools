#!/usr/bin/env python3
"""General-purpose scalpel: removes every line matching a regex. The Batch
analogue of PsStrip-Lines -- for filler the other passes don't specifically
target. Unlike bat_strip_comments.py this is a blind line filter with no
string/comment awareness, so point it carefully.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool

_FLAG_MAP = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL}


def strip_lines(text: str, *, pattern: str, flags: str = '', **_opts) -> tuple[str, dict]:
    re_flags = 0
    for c in flags:
        if c in _FLAG_MAP:
            re_flags |= _FLAG_MAP[c]
    rx = re.compile(pattern, re_flags)

    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    removed = 0
    for line in lines:
        if rx.search(line.rstrip('\r\n')):
            removed += 1
            continue
        kept.append(line)

    return ''.join(kept), {'removed_lines': removed, 'kept_lines': len(kept)}


if __name__ == '__main__':
    run_tool(
        strip_lines,
        description='Remove every line matching --pattern.',
        extra_args=[
            {'flags': ['--pattern'], 'required': True},
            {'flags': ['--flags'], 'default': ''},
        ],
    )
