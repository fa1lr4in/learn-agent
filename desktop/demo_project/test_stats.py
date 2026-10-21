import unittest

from src.stats import summarize


class SummarizeTests(unittest.TestCase):
    def test_regular_numbers(self):
        self.assertEqual(summarize([7, 14, 21]), {
            "count": 3, "total": 42, "average": 14.0, "min": 7, "max": 21,
        })

    def test_empty_list(self):
        self.assertEqual(summarize([]), {
            "count": 0, "total": 0, "average": 0.0, "min": None, "max": None,
        })

    def test_single_number(self):
        self.assertEqual(summarize([7]), {
            "count": 1, "total": 7, "average": 7.0, "min": 7, "max": 7,
        })

    def test_negative_numbers(self):
        self.assertEqual(summarize([-5, -2, -9]), {
            "count": 3, "total": -16, "average": -16 / 3, "min": -9, "max": -2,
        })


if __name__ == "__main__":
    unittest.main()
