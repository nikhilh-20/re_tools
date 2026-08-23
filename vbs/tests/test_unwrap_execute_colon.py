"""Regression tests for vbs_unwrap_execute.py's colon-terminator handling.

vbs_unwrap_execute previously re-derived "did this Execute call end with a
colon" by inspecting arg_toks[-1] directly, because code_tokens() used to
leave the terminating COLON in place. Now that code_tokens() strips it (see
vbsdeoblib/statements.py), the tool reads StatementSpan.ends_with_colon
instead. These tests guard the behavior the old inline check existed for:
a colon-joined Execute must keep its colon (not gain a trailing comment that
would swallow the rest of the physical line), while a plain Execute gets the
marker comment.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import unittest

import vbs_unwrap_execute as tool


class TestColonTerminatorPreserved(unittest.TestCase):

    def test_colon_joined_execute_keeps_colon_no_comment(self):
        src = 'Execute "x = 1": WScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('x = 1:', out)
        self.assertIn('WScript.Echo x', out)
        # The trailing comment form must not appear here: it would swallow
        # the rest of the physical line, deleting the colon-joined call.
        self.assertNotIn("'", out)

    def test_newline_terminated_execute_gets_marker_comment(self):
        src = 'Execute "x = 1"\nWScript.Echo x\n'
        out, stats = tool.run(src)
        self.assertGreater(stats['changed'], 0)
        self.assertIn('x = 1', out)
        self.assertIn("unwrapped Execute", out)


if __name__ == '__main__':
    unittest.main()
