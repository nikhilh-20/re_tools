"""H7 -- 'never raise; return malformed rather than partially parse'. Every
wrapper must survive garbage input (unbalanced quotes/parens, `set "a=b=c"`,
truncated statements, binary noise) without crashing, and prefer a no-op over
a half-applied transform.
"""
import unittest

from tests._harness import run_cli, TOOL_DIR  # noqa: F401
from batdeoblib.simulate import _quote_stripped_set_value, simulate
from batdeoblib.tokenizer import tokenize
from batdeoblib.statements import parse_script
from batdeoblib.env import Env

_GARBAGE = [
    'set "a=b=c"\r\n',                       # two '=' in the assignment
    'set "unterminated\r\necho next\r\n',     # unbalanced quote
    'if 1==1 ( echo x\r\n',                   # unclosed block
    'echo x ) ) )\r\n',                       # stray closes
    'for %%i in (\r\n',                       # truncated for
    'call set "X=%%\r\n',                     # truncated indirection
    ':::::\r\n',                              # colons only
    '%%%%%%\r\n',                             # percent soup
    '\x00\x01\x02 set "A=1" \xff\xfe\r\n',    # binary noise around code
    'set /a "x=)(+*/"\r\n',                   # nonsense arithmetic
]

_TWO_ARG_TOOLS = [
    ('bat_strip_carets.py', []),
    ('bat_expand_lines.py', []),
    ('bat_strip_comments.py', []),
    ('bat_collapse_blanklines.py', []),
    ('bat_normalize_set.py', []),
    ('bat_fold_substrings.py', []),
    ('bat_fold_strsub.py', []),
    ('bat_fold_concat.py', []),
    ('bat_fold_arithmetic.py', []),
    ('bat_fold_for_loops.py', []),
    ('bat_resolve_indirection.py', []),
    ('bat_propagate_constants.py', []),
    ('bat_inline_constants.py', []),
    ('bat_unwrap_trueif.py', []),
    ('bat_unflatten_goto.py', []),
    ('bat_inline_subroutines.py', []),
    ('bat_remove_deadcode.py', []),
    ('bat_unwrap_call.py', []),
    ('bat_annotate_exec.py', []),
    ('bat_decode_blobs.py', []),
]


class TestWrappersSurviveGarbage(unittest.TestCase):
    pass


def _mk(script, extra, src):
    def test(self):
        out, stats = run_cli(script, src, *extra)   # run_cli asserts exit 0
        self.assertNotIn('ERROR:', out[:7])
    return test


for _i, _src in enumerate(_GARBAGE):
    for _s, _e in _TWO_ARG_TOOLS:
        setattr(TestWrappersSurviveGarbage, f'test_{_s[:-3]}_g{_i}', _mk(_s, _e, _src))


class TestDeclaratorParsersReturnNoneNotPartial(unittest.TestCase):
    def test_double_equals_assignment_is_not_half_parsed(self):
        # `set "a=b=c"` -- name is `a`, value is `b=c` (first '=' splits).
        # _quote_stripped_set_value must give a clean (name, value), not choke.
        stmt = parse_script(tokenize('set "a=b=c"\r\n'))[0]
        r = _quote_stripped_set_value(stmt, Env(), Env())
        self.assertIsNotNone(r)
        name, expanded = r
        self.assertEqual(name, 'a')
        self.assertEqual(expanded.text, 'b=c')

    def test_no_equals_returns_none(self):
        stmt = parse_script(tokenize('set JUSTNAME\r\n'))[0]
        self.assertIsNone(_quote_stripped_set_value(stmt, Env(), Env()))

    def test_set_slash_forms_return_none(self):
        for src in ('set /a "x=1"\r\n', 'set /p "y=? "\r\n'):
            stmt = parse_script(tokenize(src))[0]
            self.assertIsNone(_quote_stripped_set_value(stmt, Env(), Env()))


class TestSimulateSurvivesGarbage(unittest.TestCase):
    def test_simulate_never_raises(self):
        for src in _GARBAGE:
            list(simulate(parse_script(tokenize(src)), Env()))


if __name__ == '__main__':
    unittest.main()
