#!/usr/bin/env python3
"""Removes comment-only lines (`rem ...` and `:: ...`). The Batch analogue of
PsStrip-Comments / vbs_strip_comments -- driven by the tokenizer's COMMENT
spans, so a `::`/`rem` inside a quoted string is never mistaken for one.

Unlike a plain `rem` line (virtually never used as anything but prose),
malware routinely parks encoded data payloads in `::` lines -- a `::` line
IS, structurally, just a mislabeled label that cmd.exe skips without
evaluating, which makes it a free place to hide arbitrary text. By default
this pass only strips a `::` line when its content looks like ordinary
prose; long, unbroken, high-entropy-looking runs (a plausible data carrier)
are left alone unless --include-data is passed. Plain `rem` lines are always
eligible. Annotation markers left by bat_annotate_exec.py are always kept,
so this pass never throws away a payload another tool just recovered.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind

_ANNOTATION_MARKERS = ('<<<EXEC PAYLOAD BEGIN>>>', '<<<EXEC PAYLOAD END>>>')

_MAX_WORD_LEN = 40
_MAX_LINE_LEN = 200


def _looks_like_data(body: str) -> bool:
    words = body.split()
    if any(len(w) > _MAX_WORD_LEN for w in words):
        return True
    if len(body) > _MAX_LINE_LEN:
        return True
    return False


def strip_comments(text: str, *, include_data: bool = False, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    edits: list[tuple[int, int, str]] = []
    rem_removed = 0
    dc_removed = 0
    kept = 0

    for tok in tokens:
        if tok.kind != TokenKind.COMMENT:
            continue
        if any(marker in tok.value for marker in _ANNOTATION_MARKERS):
            kept += 1
            continue
        is_dc = tok.value.lstrip()[:2] == '::'
        body = tok.value.lstrip()[2 if is_dc else 3:].strip() if is_dc else \
            re.sub(r'(?i)^rem\b', '', tok.value.lstrip(), count=1).strip()
        if is_dc and not include_data and _looks_like_data(body):
            kept += 1
            continue
        # remove the comment line, including its leading indentation and the
        # trailing newline that terminates it (line-leading removal, same
        # convention as PsStrip-Comments).
        start = tok.start
        while start > 0 and text[start - 1] in ' \t':
            start -= 1
        end = tok.end
        nl = re.match(r'\r?\n', text[end:])
        if nl:
            end += nl.end()
        edits.append((start, end, ''))
        if is_dc:
            dc_removed += 1
        else:
            rem_removed += 1

    new_text = apply_edits(text, edits) if edits else text
    changed = rem_removed + dc_removed
    return new_text, {
        'changed': changed,
        'rem_lines_removed': rem_removed,
        'data_comment_lines_removed': dc_removed,
        'comments_kept': kept,
    }


if __name__ == '__main__':
    run_tool(
        strip_comments,
        description='Remove comment-only lines (rem / ::), keeping plausible data-carrier :: lines by default.',
        extra_args=[{'flags': ['--include-data'], 'action': 'store_true', 'default': False, 'dest': 'include_data'}],
    )
