#!/usr/bin/env python3
"""Analysis only -- writes no output file. Emits a JSON report describing
every variable: where it's assigned, its final statically-known value (auto
base64-decoding a likely-base64 literal), whether it flows into an
execution sink (`call`, `%COMSPEC%`, `powershell`, `cmd /c`, `start`,
`mshta`, `wscript`, `rundll32`), and a suggested human-readable name. The
Batch analogue of PsExtract-Variables / vbs_extract_variables -- use it to
understand a sample and plan a rename map for bat_rename_variables.py.

Deviates from the --input/--output convention, like its VBS/PS siblings:
takes only --input; prints the report to stdout.
"""
from __future__ import annotations
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, flatten
from batdeoblib.simulate import simulate
from batdeoblib.env import Env, VState

_SINK_WORDS = {'CALL', 'POWERSHELL', 'PWSH', 'CMD', 'MSHTA', 'WSCRIPT', 'CSCRIPT', 'RUNDLL32', 'START'}


def _decode_preview(val: str) -> str | None:
    if len(val) >= 12 and len(val) % 4 in (0,) and all(c.isalnum() or c in '+/=' for c in val):
        try:
            raw = base64.b64decode(val, validate=True)
            text = raw.decode('utf-8')
            if text.isprintable():
                return text
        except Exception:
            pass
    return None


def _suggested_name(name: str, value: str | None, reaches_sink: bool) -> str:
    if reaches_sink:
        return 'sinkArg_' + name
    if value:
        low = value.lower()
        if low.startswith(('http://', 'https://')):
            return 'c2Url'
        if all(c in '0123456789.' for c in value) and value.count('.') == 3:
            return 'c2Ip'
        if '\\' in value or '/' in value:
            return 'dropPath'
    return name


def extract_variables(text: str) -> dict:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    stmts = flatten(tree)

    assign_sites: dict[str, list[int]] = {}
    read_sites: dict[str, list[int]] = {}
    sink_reads: set[str] = set()

    for idx, s in enumerate(stmts):
        ct = s.code_tokens()
        first = ct[0].value.lstrip('@').upper() if ct and ct[0].kind == TokenKind.TEXT else None
        is_sink_stmt = first in _SINK_WORDS
        # A statement whose OWN first token is a %VAR%/!VAR! reference means
        # the variable's value is invoked AS the command itself -- the
        # indirect-call idiom bat_unwrap_call.py targets, and every bit as
        # much a sink as an explicit powershell/cmd word.
        indirect_command_var = None
        if ct and ct[0].kind in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
            indirect_command_var = (ct[0].inner or '').split(':', 1)[0].upper()

        for t in s.tokens:
            if t.kind in (TokenKind.PCT_VAR, TokenKind.BANG_CAND):
                name = (t.inner or '').split(':', 1)[0].upper()
                read_sites.setdefault(name, []).append(idx)
                if is_sink_stmt or name == indirect_command_var:
                    sink_reads.add(name)

        if first == 'SET':
            rest = ct[1:]
            wraps = rest and rest[0].kind == TokenKind.QUOTE and rest[-1].kind == TokenKind.QUOTE and len(rest) > 1
            body = rest[1:-1] if wraps else rest
            for t in body:
                if t.kind == TokenKind.TEXT and '=' in t.value:
                    name = t.value.split('=', 1)[0].strip().upper()
                    if name:
                        assign_sites.setdefault(name, []).append(idx)
                    break

    env = Env()
    for _step in simulate(tree, env):
        pass
    final_values = {name: v.value for name, v in env.snapshot().items() if v.state == VState.KNOWN}

    all_names = sorted(set(assign_sites) | set(read_sites))
    variables = []
    for name in all_names:
        value = final_values.get(name)
        reaches_sink = name in sink_reads
        preview = _decode_preview(value) if value else None
        variables.append({
            'name': name,
            'assign_sites': assign_sites.get(name, []),
            'read_sites': read_sites.get(name, []),
            'final_value': value,
            'decoded_preview': preview,
            'reaches_sink': reaches_sink,
            'suggested_name': _suggested_name(name, value, reaches_sink),
        })

    return {'total_count': len(variables), 'variables': variables}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    args = ap.parse_args()
    src = Path(args.input).read_text(encoding='utf-8-sig', errors='replace')
    print(json.dumps(extract_variables(src), indent=2))


if __name__ == '__main__':
    main()
