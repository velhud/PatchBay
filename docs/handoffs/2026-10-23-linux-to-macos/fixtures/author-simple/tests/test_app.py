import unittest
from app import add

class AdditionTest(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(add(2, 3), 5)
