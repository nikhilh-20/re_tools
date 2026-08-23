"""vbs_collapse_blanklines — strip blank/whitespace-only lines; squeeze 3+ blank runs to one.

Analog of PsCollapse-BlankLines. Run after removal/inline passes.

Usage:
    python vbs_collapse_blanklines.py --input in.vbs --output out.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re
from vbsdeoblib import run_tool

# Same newline-token definition the tokenizer uses (vbsdeoblib/tokenizer.py):
# obfuscated VBS sources sometimes carry bare \r line endings mixed with
# real \r\n ones, which plain \n-anchored regexes (re.MULTILINE, \n{3,})
# never see as line boundaries at all.
_NL = r'\r\n|\r|\n'
_BLANK_WS_RE = re.compile(r'(\A|' + _NL + r')[ \t]+(?=' + _NL + r'|\Z)')
_NL_RUN_RE = re.compile(r'(?:' + _NL + r'){3,}')
_NL_TOKEN_RE = re.compile(_NL)


def _squeeze(m: re.Match) -> str:
    # Keep only the first two newline tokens of the run (= 1 blank line).
    tokens = _NL_TOKEN_RE.findall(m.group(0))
    return ''.join(tokens[:2])


def run(src: str, **_) -> tuple[str, dict]:
    # Pass 1: delete whitespace-only lines (lines with only spaces/tabs),
    # for any newline style.
    step1 = _BLANK_WS_RE.sub(r'\1', src)
    # Pass 2: squeeze runs of 3+ consecutive newline tokens (2+ blank
    # lines) down to 2 tokens (1 blank line).
    step2 = _NL_RUN_RE.sub(_squeeze, step1)
    changed = 1 if step2 != src else 0
    return step2, {'changed': changed}


if __name__ == '__main__':
    run_tool(run, description='Remove blank lines and squeeze consecutive blank runs to one')
