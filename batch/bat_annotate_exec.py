#!/usr/bin/env python3
"""Non-destructive: leaves the code intact and appends decoded/resolved
payloads as `rem` comments after an exec-sink command, so you can read what
it actually runs without executing anything. The Batch analogue of
PsAnnotate-Iex / vbs_annotate_execute. A documentation-only final pass.

Recognizes, when the relevant argument is statically resolvable (via the
shared simulator):
  - `powershell`/`pwsh` `-EncodedCommand`/`-enc <base64>` -- base64 +
    UTF-16LE decoded.
  - `powershell`/`pwsh` `-Command`/`-c <string>` -- annotated as-is.
  - `cmd`/`cmd.exe` `/c <string>` -- annotated as-is.
  - `mshta`/`wscript`/`cscript`/`rundll32`/`start` -- the full resolved
    command line is annotated as-is.

Markers match bat_strip_comments.py's preserved-annotation allowlist, so
running that pass afterward never discards what this one just recovered.
"""
from __future__ import annotations
import base64
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool, apply_edits
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script
from batdeoblib.simulate import simulate, _expand_mixed
from batdeoblib.env import Env

_EXEC_SINKS = {'POWERSHELL', 'PWSH', 'CMD', 'MSHTA', 'WSCRIPT', 'CSCRIPT', 'RUNDLL32', 'START'}
_ENC_ARG_RE = re.compile(r'(?i)-e(?:nc(?:odedcommand)?)?\s+([A-Za-z0-9+/=]{8,})')
_ANNOT_HERE_RE = re.compile(r'\s*rem <<<EXEC PAYLOAD BEGIN>>>')


def _strip_exe_suffix(word: str) -> str:
    return word[:-4] if word.upper().endswith('.EXE') else word


def annotate_exec(text: str, **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)

    edits: list[tuple[int, int, str]] = []
    changed = 0

    for step in simulate(tree, Env()):
        full = [t for t in step.stmt.tokens if t.kind != TokenKind.NEWLINE]
        if not full:
            continue
        # find the sink word anywhere in the statement (it may follow a
        # leading `call`/path prefix, or sit inside a quoted invocation).
        sink_idx = None
        for i, t in enumerate(full):
            if t.kind == TokenKind.TEXT:
                base = _strip_exe_suffix(t.value.lstrip('@')).upper()
                if base in _EXEC_SINKS:
                    sink_idx = i
                    break
        if sink_idx is None:
            continue

        rest = full[sink_idx + 1:]
        r = _expand_mixed(rest, step.env, step.pct_env, step.stmt.in_block)
        if not r.ok or not r.text.strip():
            continue

        arg_text = r.text
        # For cmd/cmd.exe specifically, strip a leading /c or /k switch --
        # it's the invocation flag, not part of the command that runs.
        if _strip_exe_suffix(full[sink_idx].value.lstrip('@')).upper() == 'CMD':
            m0 = re.match(r'^\s*/[ck]\s+', arg_text, re.IGNORECASE)
            if m0:
                arg_text = arg_text[m0.end():]
        decoded = None
        m = _ENC_ARG_RE.search(arg_text)
        if m:
            try:
                raw = base64.b64decode(m.group(1) + '=' * (-len(m.group(1)) % 4))
                decoded = raw.decode('utf-16-le')
            except Exception:
                decoded = None
        if decoded is None:
            decoded = arg_text.strip()
        if not decoded.strip():
            continue

        body = [t for t in step.stmt.tokens if t.kind != TokenKind.NEWLINE]
        insert_at = body[-1].end
        # Idempotency: don't re-annotate a sink this pass already annotated on
        # an earlier run -- otherwise the chain never reaches a fixpoint
        # (annotate adds the block, a later strip-comments trims its prose
        # line, annotate re-adds it, ...).
        if _ANNOT_HERE_RE.match(text, insert_at):
            continue
        comment_lines = ['rem <<<EXEC PAYLOAD BEGIN>>>']
        for line in decoded.splitlines() or ['']:
            comment_lines.append(f'rem > {line}')
        comment_lines.append('rem <<<EXEC PAYLOAD END>>>')
        annotation = '\n' + '\n'.join(comment_lines)
        edits.append((insert_at, insert_at, annotation))
        changed += 1

    new_text = apply_edits(text, edits) if edits else text
    return new_text, {'changed': changed}


if __name__ == '__main__':
    run_tool(annotate_exec, description='Append decoded/resolved exec-sink payloads as rem comments (non-destructive).')
