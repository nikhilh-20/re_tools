"""batdeoblib.env -- the three-state variable lattice and the setlocal/endlocal
scope stack. The KNOWN / UNKNOWN / UNSET distinction is load-bearing: UNSET
resolves to '' (foldable), UNKNOWN refuses (None).
"""
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.env import Env, VState, AMBIENT_UNKNOWN_VARS


class TestThreeStateLattice(unittest.TestCase):
    def test_unset_resolves_to_empty(self):
        e = Env()
        self.assertEqual(e.resolve_read('NEVERSET'), '')
        self.assertEqual(e.get('NEVERSET').state, VState.UNSET)

    def test_known_resolves_to_value(self):
        e = Env()
        e.set_known('X', 'hello')
        self.assertEqual(e.resolve_read('X'), 'hello')

    def test_unknown_refuses(self):
        e = Env()
        e.set_unknown('X')
        self.assertIsNone(e.resolve_read('X'))

    def test_case_insensitive_names(self):
        e = Env()
        e.set_known('Path', 'C:\\')
        self.assertEqual(e.resolve_read('PATH'), 'C:\\')
        self.assertEqual(e.resolve_read('path'), 'C:\\')

    def test_ambient_vars_seeded_unknown(self):
        e = Env()
        self.assertIsNone(e.resolve_read('SystemRoot'))
        self.assertIsNone(e.resolve_read('TEMP'))
        self.assertIn('APPDATA', AMBIENT_UNKNOWN_VARS)

    def test_seed_ambient_off(self):
        e = Env(seed_ambient=False)
        self.assertEqual(e.resolve_read('TEMP'), '')   # now just Unset


class TestScopeStack(unittest.TestCase):
    def test_setlocal_endlocal_restores(self):
        e = Env()
        e.set_known('X', 'outer')
        e.setlocal()
        e.set_known('X', 'inner')
        self.assertEqual(e.resolve_read('X'), 'inner')
        e.endlocal()
        self.assertEqual(e.resolve_read('X'), 'outer')

    def test_setlocal_inherits_parent_bindings(self):
        e = Env()
        e.set_known('A', '1')
        e.setlocal()
        self.assertEqual(e.resolve_read('A'), '1')

    def test_endlocal_without_setlocal_is_noop_not_raise(self):
        e = Env()
        e.endlocal()   # must not raise
        e.endlocal()

    def test_delayed_expansion_is_scoped(self):
        e = Env()
        self.assertFalse(e.delayed_expansion)
        e.setlocal(enable_delayed=True)
        self.assertTrue(e.delayed_expansion)
        e.endlocal()
        self.assertFalse(e.delayed_expansion)


if __name__ == '__main__':
    unittest.main()
