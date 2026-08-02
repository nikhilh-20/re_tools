"""vbs_unwrap_trueif — collapse If <always-true> Then ... End If to just the body.

Only handles single-clause, no ElseIf/Else If/Else forms.
Conditions resolved via the shared constant evaluator.

Analog of PsUnwrap-TrueIf.

Usage:
    python vbs_unwrap_trueif.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.io import apply_edits
from vbsdeoblib.resolver import resolve_const


def run(src: str, **_) -> tuple[str, dict]:
    changed_total = 0
    for _ in range(50):
        src, n = _one_pass(src)
        changed_total += n
        if n == 0:
            break
    return src, {'changed': changed_total}


# Match a full block If with no ElseIf/Else:
#   If <cond> Then\n
#     <body>
#   End If
_IF_BLOCK = re.compile(
    r'(?im)^([ \t]*)If\s+(.+?)\s+Then\s*\r?\n'   # If cond Then
    r'((?:(?!(?:[ \t]*(?:ElseIf|Else\s+If|Else|End\s+If)))[^\r\n]*\r?\n)*)'   # body (no Else/End)
    r'[ \t]*End\s+If\b[^\r\n]*',                 # End If
)


def _truthy(v) -> bool:
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.lower() not in ('', 'false', '0')
    return bool(v)


def _one_pass(src: str) -> tuple[str, int]:
    edits: list[tuple[int, int, str]] = []

    for m in _IF_BLOCK.finditer(src):
        indent   = m.group(1)
        cond_txt = m.group(2).strip()
        body     = m.group(3)

        # Tokenise and resolve the condition.
        ctoks = tokenize(cond_txt)
        val = resolve_const(ctoks)
        if val is None or not _truthy(val):
            continue

        # Replace the whole If...End If with just the body (dedented by one level if possible).
        # We strip the outermost indentation to keep nesting reasonable.
        body_lines = []
        for line in body.splitlines(keepends=True):
            # Remove one level of indentation matching the If's indent.
            if line.startswith(indent + '    '):
                body_lines.append(line[len(indent)+4:])
            elif line.startswith(indent + '\t'):
                body_lines.append(line[len(indent)+1:])
            else:
                body_lines.append(line)
        replacement = ''.join(body_lines)
        edits.append((m.start(), m.end(), replacement))

    if not edits:
        return src, 0
    # Apply non-overlapping edits (the regex finds non-overlapping matches by default).
    return apply_edits(src, edits), len(edits)


if __name__ == '__main__':
    run_tool(run, description='Collapse statically-true single-clause If blocks to their body')
