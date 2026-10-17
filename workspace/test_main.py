"""Check sum_to(n) against fixed examples, including the boundary n=0."""

import runpy
import unittest
from pathlib import Path


namespace = runpy.run_path(str(Path(__file__).with_name("main.py")))


class SumToTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(namespace["sum_to"](0), 0)

    def test_one(self):
        self.assertEqual(namespace["sum_to"](1), 1)

    def test_ten(self):
        self.assertEqual(namespace["sum_to"](10), 55)

    def test_one_hundred(self):
        self.assertEqual(namespace["sum_to"](100), 5050)


if __name__ == "__main__":
    unittest.main()
