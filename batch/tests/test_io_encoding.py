"""H1 -- batdeoblib.io.read_source_text: BOM-aware decoding.

Obfuscated .bat/.cmd drops are sometimes saved as UTF-16 (cmd.exe runs them
fine). The old hardcoded `read_text(encoding='utf-8-sig')` turned those into
NUL-interleaved mojibake that no pass could parse, so every pass reported
`changed:0` with no error. read_source_text sniffs the BOM instead.
"""
import tempfile
import unittest
from pathlib import Path

from tests._harness import TOOL_DIR  # noqa: F401  (path bootstrap)
from batdeoblib.io import read_source_text

SAMPLE = 'set "A=hello"\r\nset "B=%A% world"\r\necho %B%\r\n'


def _write(raw: bytes) -> str:
    d = tempfile.mkdtemp()
    p = Path(d) / 'sample.bat'
    p.write_bytes(raw)
    return str(p)


class TestBomRoundTrip(unittest.TestCase):
    def test_utf8_no_bom(self):
        self.assertEqual(read_source_text(_write(SAMPLE.encode('utf-8'))), SAMPLE)

    def test_utf8_with_bom(self):
        self.assertEqual(read_source_text(_write(b'\xef\xbb\xbf' + SAMPLE.encode('utf-8'))), SAMPLE)

    def test_utf16_le_with_bom(self):
        self.assertEqual(read_source_text(_write(b'\xff\xfe' + SAMPLE.encode('utf-16-le'))), SAMPLE)

    def test_utf16_be_with_bom(self):
        self.assertEqual(read_source_text(_write(b'\xfe\xff' + SAMPLE.encode('utf-16-be'))), SAMPLE)

    def test_utf32_le_with_bom(self):
        self.assertEqual(read_source_text(_write(b'\xff\xfe\x00\x00' + SAMPLE.encode('utf-32-le'))), SAMPLE)

    def test_utf32_be_with_bom(self):
        self.assertEqual(read_source_text(_write(b'\x00\x00\xfe\xff' + SAMPLE.encode('utf-32-be'))), SAMPLE)

    def test_utf16_le_decoded_has_no_nul(self):
        # the exact failure shape: a UTF-16 file decoded as utf-8 is full of \x00
        out = read_source_text(_write(b'\xff\xfe' + SAMPLE.encode('utf-16-le')))
        self.assertNotIn('\x00', out)


class TestFallbackAndRobustness(unittest.TestCase):
    def test_no_bom_defaults_to_utf8(self):
        # BOM-less input keeps the prior behavior exactly (utf-8-sig).
        self.assertEqual(read_source_text(_write(SAMPLE.encode('utf-8'))), SAMPLE)

    def test_malformed_bytes_do_not_raise(self):
        out = read_source_text(_write(b'set "A=\xff\xfe\x9c" & echo done\r\n'))
        self.assertIsInstance(out, str)
        self.assertIn('echo done', out)

    def test_non_ascii_identifiers_survive_utf8(self):
        # sample 48a200... names variables with multibyte identifiers
        src = 'set "ééé=x"\r\necho %ééé%\r\n'
        self.assertEqual(read_source_text(_write(src.encode('utf-8'))), src)


if __name__ == '__main__':
    unittest.main()
