"""vbs_extract_variables — analysis-only JSON report of variables.

For each variable: all assignment sites with decoded preview, whether it
reaches a sink (CreateObject, .Run, Execute/ExecuteGlobal, RegWrite, .Open/.Send),
and a suggested human-readable name.

Analog of PsExtract-Variables. Writes no output file; prints JSON to stdout.

Usage:
    python vbs_extract_variables.py --input in.vbs
"""
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
import re, json
from collections import defaultdict
from vbsdeoblib import tokenize, TokenKind, run_tool
from vbsdeoblib.resolver import resolve_const, Const, _parse_string


# Sink patterns: calls/access that execute or exfiltrate content.
_SINKS = frozenset("""
EXECUTE EXECUTEGLOBAL EVAL RUN EXEC REGWRITE SEND OPEN
""".split())

_URL_RE   = re.compile(r'https?://', re.I)
_PATH_RE  = re.compile(r'[A-Za-z]:\\|%\w+%', re.I)
_B64_RE   = re.compile(r'^[A-Za-z0-9+/]{20,}={0,2}$')


def _suggest_name(name: str, preview: str | None) -> str:
    if preview is None:
        return name.lower()
    p = preview
    if _URL_RE.search(p):
        return 'c2_url'
    if re.search(r'(?i)createobject', p):
        return 'com_obj'
    if _PATH_RE.search(p):
        return 'file_path'
    if re.search(r'(?i)regwrite|hkcu|hklm', p):
        return 'reg_key'
    if _B64_RE.match(p):
        return 'b64_payload'
    if re.search(r'(?i)powershell|cmd\.exe|wscript', p):
        return 'shell_cmd'
    return name.lower()


def run(src: str, **_) -> tuple[str, dict]:
    tokens = tokenize(src)

    assigns: dict[str, list] = defaultdict(list)  # name_upper -> [preview|None]
    reads:   set[str] = set()
    sinks:   set[str] = set()

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == TokenKind.IDENT:
            up = t.upper
            if up in _SINKS:
                # Mark arguments as sink-reachable.
                # Collect tokens until end of statement.
                j = i + 1
                while j < len(tokens) and tokens[j].kind not in (TokenKind.NEWLINE, TokenKind.COMMENT):
                    if tokens[j].kind == TokenKind.IDENT:
                        sinks.add(tokens[j].upper)
                    j += 1
            # Is this an assignment LHS?
            j = i + 1
            while j < len(tokens) and tokens[j].kind == TokenKind.WS:
                j += 1
            if (j < len(tokens) and tokens[j].kind == TokenKind.OP
                    and tokens[j].value == '='):
                # Collect RHS tokens.
                k = j + 1
                rhs: list = []
                while k < len(tokens) and tokens[k].kind not in (TokenKind.NEWLINE,):
                    rhs.append(tokens[k])
                    k += 1
                val = resolve_const(rhs)
                preview = str(val) if val is not None else None
                assigns[up].append({'line': _line_of(src, t.start), 'preview': preview})
            else:
                reads.add(up)
        elif t.kind == TokenKind.OP and t.value == '.' :
            # obj.Method — mark method names as potential sinks.
            j = i + 1
            while j < len(tokens) and tokens[j].kind == TokenKind.WS:
                j += 1
            if j < len(tokens) and tokens[j].kind == TokenKind.IDENT:
                if tokens[j].upper in _SINKS:
                    # The receiver object variable
                    k = i - 1
                    while k >= 0 and tokens[k].kind == TokenKind.WS:
                        k -= 1
                    if k >= 0 and tokens[k].kind == TokenKind.IDENT:
                        sinks.add(tokens[k].upper)
        i += 1

    variables = []
    for name_up, assign_list in sorted(assigns.items()):
        if name_up in _KW:
            continue
        preview = assign_list[-1]['preview'] if assign_list else None
        variables.append({
            'name': name_up,
            'assignments': assign_list,
            'reaches_sink': name_up in sinks or name_up in reads and name_up in sinks,
            'decoded_preview': preview,
            'suggested_name': _suggest_name(name_up, preview),
        })

    report = {'total_count': len(variables), 'variables': variables}
    # analysis_only=True path — run_tool will print JSON to stdout.
    return '', report


def _line_of(src: str, offset: int) -> int:
    return src[:offset].count('\n') + 1


_KW = frozenset("""
AND BYREF BYVAL CALL CASE CLASS CONST DIM DO EACH ELSE ELSEIF END ERASE ERROR
EXECUTE EXECUTEGLOBAL EXIT FALSE FOR FUNCTION GET IF IN IS LET LOOP MOD NEW
NEXT NOT NOTHING NULL OBJECT ON OPTION OR PRESERVE PRIVATE PUBLIC RANDOMIZE REDIM
REM RESUME SELECT SET STEP STOP SUB THEN TO TRUE UNTIL WEND WHILE WITH XOR
""".split())


if __name__ == '__main__':
    run_tool(run, description='Emit a JSON report of variables, their values, and sink reachability',
             analysis_only=True)
