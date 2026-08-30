"""C1 -- bat_reconstruct_files: rebuild a payload assembled by `echo >> FILE`
and decode it the way the script itself does.
"""
import base64
import unittest

from tests._harness import run_cli_outdir


def _echo_b64_source(raw: bytes, *, var='%TEMP%\\p.b64', pem=False,
                     certutil='decode', chunk=64, cleanup_del=True):
    b64 = base64.b64encode(raw).decode()
    lines = [b64[i:i + chunk] for i in range(0, len(b64), chunk)]
    if pem:
        lines = ['-----BEGIN CERTIFICATE-----', *lines, '-----END CERTIFICATE-----']
    src = f'set "F={var}"\r\nif exist "%F%" del "%F%"\r\n'
    for ln in lines:
        src += f'echo {ln}>>"%F%"\r\n'
    if certutil:
        src += f'certutil -{certutil} "%F%" "%TEMP%\\p.out" >nul\r\n'
    if cleanup_del:
        src += 'del "%F%"\r\n'
    src += 'start "" "%TEMP%\\p.out"\r\n'
    return src


class TestReconstruct(unittest.TestCase):
    def test_echo_chain_rebuilds_and_certutil_decodes_to_pe(self):
        raw = b'MZ' + b'\x90\x00' * 60 + b'PE\x00\x00' + b'rest of the fake binary ' * 10
        r = run_cli_outdir('bat_reconstruct_files.py', _echo_b64_source(raw))
        self.addCleanup(r.cleanup)
        bins = [s for s in r.manifest['stages'] if s['file'].endswith('.bin')]
        self.assertEqual(len(bins), 1)
        self.assertIn('certutil-decode', bins[0]['decoder'])
        self.assertEqual(r.read(bins[0]['file'])[:2], b'MZ')

    def test_fake_pem_armor_is_stripped_before_decode(self):
        raw = b'MZ' + b'ABCD' * 80
        r = run_cli_outdir('bat_reconstruct_files.py', _echo_b64_source(raw, pem=True))
        self.addCleanup(r.cleanup)
        bins = [s for s in r.manifest['stages'] if s['file'].endswith('.bin')]
        self.assertEqual(len(bins), 1)
        self.assertEqual(r.read(bins[0]['file'])[:2], b'MZ')

    def test_cleanup_del_after_decode_does_not_lose_content(self):
        raw = b'MZ' + b'payload bytes ' * 40
        with_del = run_cli_outdir('bat_reconstruct_files.py', _echo_b64_source(raw, cleanup_del=True))
        self.addCleanup(with_del.cleanup)
        self.assertTrue(any(s['file'].endswith('.bin') for s in with_del.manifest['stages']))

    def test_unresolvable_echo_marks_partial_and_does_not_decode(self):
        # one chunk references an unknown variable -> the file is partial,
        # never decoded (a bogus "decode" of truncated data is worse than none)
        src = ('set "F=%TEMP%\\p.b64"\r\n'
               'echo AAAAAAAAAAAAAAAAAAAAAAAAAAAA>>"%F%"\r\n'
               'echo %MYSTERYCHUNK%>>"%F%"\r\n'
               'echo BBBBBBBBBBBBBBBBBBBBBBBBBBBB>>"%F%"\r\n'
               'echo CCCCCCCCCCCCCCCCCCCCCCCCCCCC>>"%F%"\r\n'
               'echo DDDDDDDDDDDDDDDDDDDDDDDDDDDD>>"%F%"\r\n'
               'echo EEEEEEEEEEEEEEEEEEEEEEEEEEEE>>"%F%"\r\n'
               'echo FFFFFFFFFFFFFFFFFFFFFFFFFFFF>>"%F%"\r\n'
               'echo GGGGGGGGGGGGGGGGGGGGGGGGGGGG>>"%F%"\r\n'
               'certutil -decode "%F%" "%TEMP%\\p.out"\r\n')
        r = run_cli_outdir('bat_reconstruct_files.py', src)
        self.addCleanup(r.cleanup)
        partials = [s for s in r.manifest['stages'] if s.get('partial')]
        self.assertTrue(partials)
        self.assertTrue(all(s['decoder'] == 'none' for s in partials))
        self.assertFalse(any(s['file'].endswith('.bin') for s in r.manifest['stages']))

    def test_nul_redirects_are_not_treated_as_files(self):
        raw = b'MZ' + b'x' * 100
        r = run_cli_outdir('bat_reconstruct_files.py', _echo_b64_source(raw))
        self.addCleanup(r.cleanup)
        self.assertFalse(any('nul' in s['origin'].lower() for s in r.manifest['stages']))


if __name__ == '__main__':
    unittest.main()
