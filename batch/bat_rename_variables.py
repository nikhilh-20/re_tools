#!/usr/bin/env python3
"""Applies an old->new rename map (JSON, case-insensitive keys) to every
occurrence of each variable -- both assignment and read sites. The Batch
analogue of PsRename-Variables / vbs_rename_variables. Pair it with
bat_extract_variables.py, whose report gives you names and suggestions.

Renames every %NAME%/!NAME!/%~mods-on-argN (argN forms are positional, not
named, so never touched) reference, plus the NAME portion of `set NAME=...`
assignments -- both the bare and quoted forms.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten


def rename_variables(text: str, *, renames_file: str, **_opts) -> tuple[str, dict]:
    raw_map = json.loads(Path(renames_file).read_text(encoding='utf-8'))
    rename_map = {k.upper(): v for k, v in raw_map.items()}

    tokens = tokenize(text)
    tree = parse_script(tokens)
    stmts = flatten(tree)

    edits: list[tuple[int, int, str]] = []
    occurrence_count: dict[str, int] = {}

    for s in stmts:
        ct = s.code_tokens()
        if ct and ct[0].kind == TokenKind.TEXT and ct[0].value.lstrip('@').upper() == 'SET':
            rest = ct[1:]
            if not (rest and rest[0].kind == TokenKind.TEXT and rest[0].value.startswith('/')):
                wraps = rest and rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
                body = rest[1:-1] if wraps else rest
                for t in body:
                    if t.kind == TokenKind.TEXT and '=' in t.value:
                        eq = t.value.index('=')
                        name = t.value[:eq]
                        key = name.strip().upper()
                        if key in rename_map:
                            new_name = rename_map[key]
                            edits.append((t.start, t.start + eq, new_name))
                            occurrence_count[key] = occurrence_count.get(key, 0) + 1
                        break

        for t in s.tokens:
            if t.kind not in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                continue
            inner = t.inner or ''
            name, _, mod = inner.partition(':')
            key = name.upper()
            if key not in rename_map:
                continue
            new_name = rename_map[key]
            new_inner = new_name + (':' + mod if ':' in inner else '')
            new_value = t.value[0] + new_inner + t.value[0]
            edits.append((t.start, t.end, new_value))
            occurrence_count[key] = occurrence_count.get(key, 0) + 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'renamed': len(occurrence_count), 'occurrences': occurrence_count}


if __name__ == '__main__':
    run_tool(
        rename_variables,
        description='Apply an old->new variable rename map (JSON) to every occurrence.',
        extra_args=[{'flags': ['--renames'], 'required': True, 'dest': 'renames_file'}],
    )
