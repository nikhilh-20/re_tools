#!/usr/bin/env python3
"""One statement per line: splits `&`, `&&`, `||`, `|` connectors outside
quotes/blocks onto their own lines, and re-indents `( ... )` block bodies.
The Batch analogue of PsExpand-Semicolons -- usually the first thing you run
on a minified single-line dropper.

Uses the shared statement/block tree (batdeoblib.statements) rather than
regex splitting, so a connector character inside a quoted string or inside a
`%VAR:...%` modifier is never mistaken for a statement separator.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batdeoblib.io import run_tool
from batdeoblib.tokenizer import tokenize, TokenKind
from batdeoblib.statements import parse_script, Statement, Block


def _stmt_text(s: Statement) -> str:
    parts = []
    for t in s.tokens:
        if t.kind == TokenKind.NEWLINE:
            continue
        parts.append(t.value)
    return ''.join(parts).strip()


def _ends_with_newline(s: Statement) -> bool:
    return bool(s.tokens) and s.tokens[-1].kind == TokenKind.NEWLINE


def _render(nodes: list, indent: str, out: list[str]) -> int:
    # The connector (&, &&, ||, |) that introduced a statement is kept as a
    # prefix on that statement's own line, never dropped -- && and || are
    # CONDITIONAL execution (run only if the previous command succeeded /
    # failed), not just a sequencing separator like `;` in other languages,
    # so silently collapsing it to a bare newline would change behavior.
    # Statement->Statement transitions always start a fresh line: the parser
    # only ever splits two statements apart at a NEWLINE or a connector OP,
    # both already rendered explicitly, so there is never an ambiguous gap.
    #
    # A Block's opening '(' is the one case that CAN be "mid-line" relative
    # to what preceded it: Block boundaries come from seeing an OP '(' while
    # still mid-segment (`for %%A in (1 2) do (...)`, `if "%x%"=="1" (...)`),
    # not from a NEWLINE/connector split. Only glue the '(' onto the prior
    # line when the immediately preceding sibling was NOT itself terminated
    # by a real newline -- moving such a paren onto its own fresh line is a
    # correctness risk, since `for`/`if` do not reliably tolerate their own
    # clause being broken before the opening paren is seen.
    changed = 0
    prev_stmt: Statement | None = None
    for node in nodes:
        if isinstance(node, Statement):
            text = _stmt_text(node)
            if not text:
                continue
            prefix = f'{node.connector_before} ' if node.connector_before else ''
            out.append(f'{indent}{prefix}{text}')
            if node.connector_before:
                changed += 1
            prev_stmt = node
        else:  # Block
            prefix = f'{node.connector_before} ' if node.connector_before else ''
            glue = (node.connector_before is None and prev_stmt is not None
                    and not _ends_with_newline(prev_stmt) and out)
            if glue:
                out[-1] = f'{out[-1]} {prefix}('
            else:
                out.append(f'{indent}{prefix}(')
            if node.connector_before:
                changed += 1
            changed += _render(node.body, indent + '    ', out)
            out.append(f'{indent})')
            prev_stmt = None
    return changed


def expand_lines(text: str, *, indent_string: str = '    ', **_opts) -> tuple[str, dict]:
    tokens = tokenize(text)
    tree = parse_script(tokens)
    out: list[str] = []
    changed = _render(tree, '', out)
    new_text = '\n'.join(out) + ('\n' if out else '')
    return new_text, {'changed': changed, 'lines_out': len(out)}


if __name__ == '__main__':
    run_tool(
        expand_lines,
        description='Split &/&&/||/| connectors and (...) blocks onto separate, indented lines.',
        extra_args=[{'flags': ['--indent-string'], 'default': '    ', 'dest': 'indent_string'}],
    )
