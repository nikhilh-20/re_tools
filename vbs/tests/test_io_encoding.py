"""Unit tests for vbsdeoblib.io.read_source_text — BOM-aware decoding.

Regression coverage for the bug where every tool's --input read was
hardcoded to utf-8-sig, silently mangling UTF-16 VBS/HTA drops into
NUL-interleaved garbage that no tokenizer could parse (every pass then
reported zero changes with no error).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import tempfile
import unittest
from pathlib import Path

from vbsdeoblib.io import read_source_text

SAMPLE = 'Dim uncannily\r\nuncannily = "hello" & Chr(33)\r\n'


class TestReadSourceText(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _write(self, data: bytes) -> Path:
        p = Path(self._tmpdir.name) / 'sample.vbs'
        p.write_bytes(data)
        return p

    def test_utf8_no_bom(self):
        p = self._write(SAMPLE.encode('utf-8'))
        self.assertEqual(read_source_text(p), SAMPLE)

    def test_utf8_with_bom(self):
        p = self._write(b'\xef\xbb\xbf' + SAMPLE.encode('utf-8'))
        self.assertEqual(read_source_text(p), SAMPLE)

    def test_utf16_le_with_bom(self):
        p = self._write(b'\xff\xfe' + SAMPLE.encode('utf-16-le'))
        self.assertEqual(read_source_text(p), SAMPLE)

    def test_utf16_be_with_bom(self):
        p = self._write(b'\xfe\xff' + SAMPLE.encode('utf-16-be'))
        self.assertEqual(read_source_text(p), SAMPLE)

    def test_utf32_le_with_bom(self):
        p = self._write(b'\xff\xfe\x00\x00' + SAMPLE.encode('utf-32-le'))
        self.assertEqual(read_source_text(p), SAMPLE)

    def test_utf32_be_with_bom(self):
        p = self._write(b'\x00\x00\xfe\xff' + SAMPLE.encode('utf-32-be'))
        self.assertEqual(read_source_text(p), SAMPLE)

    def test_no_bom_defaults_to_utf8(self):
        # Same as pre-fix behavior for the common case: no BOM -> utf-8-sig.
        p = self._write('x = "plain ascii, no bom"\r\n'.encode('utf-8'))
        self.assertEqual(read_source_text(p), 'x = "plain ascii, no bom"\r\n')

    def test_malformed_bytes_do_not_raise(self):
        # Invalid UTF-8 sequence with no recognizable BOM: must degrade via
        # errors='replace', not crash the caller.
        p = self._write(b'x = "\xff\xfe\xfd bad bytes"')
        try:
            text = read_source_text(p)
        except UnicodeDecodeError:
            self.fail('read_source_text raised UnicodeDecodeError instead of replacing')
        self.assertIsInstance(text, str)

    def test_the_reported_sample_pattern_round_trips_utf16(self):
        """The exact shape of bytes that triggered this bug: a BOM'd UTF-16LE
        file whose ASCII content would otherwise decode with a literal NUL
        after every character under a naive utf-8-sig read."""
        src = 'uncannily = (uncannily) & thyreopalatinusotio("A","B","")\r\n'
        p = self._write(b'\xff\xfe' + src.encode('utf-16-le'))
        decoded = read_source_text(p)
        self.assertEqual(decoded, src)
        self.assertNotIn('\x00', decoded)


if __name__ == '__main__':
    unittest.main()
