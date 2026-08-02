"""vbs_collapse_blanklines — strip blank/whitespace-only lines; squeeze 3+ blank runs to one.

Analog of PsCollapse-BlankLines. Run after removal/inline passes.

Usage:
    python vbs_collapse_blanklines.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import run_tool


def run(src: str, **_) -> tuple[str, dict]:
    # Pass 1: delete whitespace-only lines (lines with only spaces/tabs).
    step1 = re.sub(r'^[ \t]+$', '', src, flags=re.MULTILINE)
    # Pass 2: squeeze 3+ consecutive blank lines to 1.
    step2 = re.sub(r'\n{3,}', '\n\n', step1)
    changed = 1 if step2 != src else 0
    return step2, {'changed': changed}


if __name__ == '__main__':
    run_tool(run, description='Remove blank lines and squeeze consecutive blank runs to one')
