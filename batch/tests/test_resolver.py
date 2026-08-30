"""batdeoblib.resolver -- the set /a arithmetic evaluator and the if-condition
truth evaluator. Both must fold what they can prove and refuse (ArithError /
CondResult.unknown) everything else, never emit a maybe-wrong value.
"""
import unittest

from tests._harness import TOOL_DIR  # noqa: F401
from batdeoblib.resolver import eval_arith, eval_condition, ArithError


class TestEvalArith(unittest.TestCase):
    def test_precedence_and_parens(self):
        self.assertEqual(eval_arith('(18+18-(13-17))+32'), 72)
        self.assertEqual(eval_arith('2+3*4'), 14)
        self.assertEqual(eval_arith('(2+3)*4'), 20)

    def test_hex_and_octal(self):
        self.assertEqual(eval_arith('0xFF'), 255)
        self.assertEqual(eval_arith('010'), 8)

    def test_bitwise_and_shift(self):
        self.assertEqual(eval_arith('1<<4'), 16)
        self.assertEqual(eval_arith('0xF0 | 0x0F'), 255)
        self.assertEqual(eval_arith('12 ^ 10'), 6)

    def test_32bit_signed_wraparound(self):
        self.assertEqual(eval_arith('2147483647 + 1'), -2147483648)

    def test_c_style_truncating_division(self):
        self.assertEqual(eval_arith('-7 / 2'), -3)   # toward zero, not floor
        self.assertEqual(eval_arith('-7 % 2'), -1)

    def test_bare_identifier_via_callback(self):
        env = {'Y': 4}
        self.assertEqual(eval_arith('y+1', lambda n: env.get(n.upper())), 5)

    def test_bare_identifier_without_callback_raises(self):
        with self.assertRaises(ArithError):
            eval_arith('y+1')

    def test_callback_returning_none_raises(self):
        with self.assertRaises(ArithError):
            eval_arith('y+1', lambda n: None)

    def test_division_by_zero_raises(self):
        with self.assertRaises(ArithError):
            eval_arith('5/0')

    def test_trailing_garbage_raises(self):
        with self.assertRaises(ArithError):
            eval_arith('1 2 3')

    def test_empty_raises(self):
        with self.assertRaises(ArithError):
            eval_arith('   ')


class TestEvalCondition(unittest.TestCase):
    def test_string_equality(self):
        self.assertIs(eval_condition('"1"=="1"').value, True)
        self.assertIs(eval_condition('"a"=="b"').value, False)

    def test_case_insensitive(self):
        self.assertIs(eval_condition('"ABC"=="abc"', case_insensitive=True).value, True)
        self.assertIs(eval_condition('"ABC"=="abc"').value, False)

    def test_numeric_ops(self):
        self.assertIs(eval_condition('5 GTR 3').value, True)
        self.assertIs(eval_condition('5 LSS 3').value, False)
        self.assertIs(eval_condition('4 EQU 4').value, True)

    def test_not_prefix(self):
        self.assertIs(eval_condition('not "1"=="1"').value, False)
        self.assertIs(eval_condition('not 1 GTR 9').value, True)

    def test_numeric_operand_not_literal_is_unknown(self):
        self.assertIsNone(eval_condition('%X% GTR 3').value)

    def test_unrecognized_shape_is_unknown(self):
        self.assertIsNone(eval_condition('exist C:\\foo').value)
        self.assertIsNone(eval_condition('errorlevel 1').value)


if __name__ == '__main__':
    unittest.main()
