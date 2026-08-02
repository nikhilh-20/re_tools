"""vbs_rename_variables — apply a JSON old→new rename map to all variable occurrences.

Case-insensitive matching. Keys in the JSON should be the current variable names
(without $); values are the replacement names.

Analog of PsRename-Variables.

Usage:
    python vbs_rename_variables.py --input in.vbs --output out.vbs --renames renames.json
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import json
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits


def run(src: str, renames: str = '', **_) -> tuple[str, dict]:
    if not renames:
        return src, {'changed': 0, 'renamed': {}}
    with open(renames, encoding='utf-8') as f:
        rename_map: dict[str, str] = {k.upper(): v for k, v in json.load(f).items()}

    tokens = tokenize(src)
    edits: list[tuple[int, int, str]] = []
    counts: dict[str, int] = {}

    for tok in tokens:
        if tok.kind == TokenKind.IDENT and tok.upper in rename_map:
            new_name = rename_map[tok.upper]
            edits.append((tok.start, tok.end, new_name))
            counts[tok.upper] = counts.get(tok.upper, 0) + 1

    if not edits:
        return src, {'changed': 0, 'renamed': {}}
    new_src = apply_edits(src, edits)
    return new_src, {'changed': len(edits), 'renamed': counts}


if __name__ == '__main__':
    run_tool(run, description='Rename variables using a JSON old→new map',
             extra_args=[{'flags': ['--renames'], 'required': True, 'metavar': 'FILE',
                          'help': 'JSON file mapping old names to new names'}])
