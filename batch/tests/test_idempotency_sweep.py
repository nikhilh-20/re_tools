"""Every wrapper transform is a single pass -- running it on its own output
must be a no-op. This sweep drives all of them (via their real CLI) over one
shared, deliberately messy sample and asserts `changed:0` on the second run
and byte-identical output.

Cheap insurance against a fold that re-triggers on the text it just emitted --
the class of bug vbs guards with "skip a trivial (5)", "only fold runs of >= 2
atoms", "look back 200 chars before re-adding a marker".
"""
import unittest

from tests._harness import run_cli

_SAMPLE = (
    '@echo off\r\n'
    'set "A=po"\r\n'
    'set "B=wer"\r\n'
    'set "C=shell"\r\n'
    'set "S=___abcdefTARGETxyz___"\r\n'
    'set /a "N=(2+3)*4"\r\n'
    'set "CMD=%A%%B%%C%"\r\n'
    'rem a junk banner line\r\n'
    ':: another junk comment\r\n'
    'p^o^w^e^r^s^h^e^l^l -NoProfile -c "echo hi"\r\n'
    'if "1"=="1" echo always\r\n'
    'if "x"=="y" echo never\r\n'
    'call :sub 42\r\n'
    'goto :done\r\n'
    'echo unreachable\r\n'
    ':sub\r\n'
    'echo sub got %1\r\n'
    'goto :eof\r\n'
    ':done\r\n'
    'set "PART=%S:~9,6%"\r\n'
    'echo %PART% %N% %CMD%\r\n'
    '\r\n'
    '\r\n'
    '\r\n'
)

# (script, extra args)  -- one entry per two-argument wrapper
_TOOLS = [
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
    ('bat_strip_lines.py', ['--pattern', r'^\s*rem GEN', '--flags', 'i']),
    # bat_rename_variables needs a --renames FILE (its own test covers it)
]


class TestIdempotencySweep(unittest.TestCase):
    pass


def _mk(script, extra):
    def test(self):
        out1, _ = run_cli(script, _SAMPLE, *extra)
        out2, stats2 = run_cli(script, out1, *extra)
        self.assertEqual(stats2.get('changed', 0), 0, f'{script} not idempotent (changed)')
        self.assertEqual(out1, out2, f'{script} not idempotent (text)')
    return test


for _s, _e in _TOOLS:
    setattr(TestIdempotencySweep, 'test_' + _s[:-3], _mk(_s, _e))


if __name__ == '__main__':
    unittest.main()
