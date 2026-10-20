"""Fixed acceptance checks, loaded by the evaluator outside the Agent workspace."""

import runpy
import sys
import unittest
from pathlib import Path


case_id, directory = sys.argv[1:]
root = Path(directory).resolve()
# Import the test framework before making the fixture importable.
sys.path.insert(0, str(root))
if case_id == "fix_sum":
    function = runpy.run_path(str(root / "main.py"))["sum_to"]
    examples = [(0, 0), (1, 1), (10, 55), (100, 5050)]
elif case_id == "extend_stats":
    function = runpy.run_path(str(root / "src" / "stats.py"))["summarize"]
    examples = [
        ([5, 10, 15], {"count": 3, "total": 30, "average": 10.0, "min": 5, "max": 15}),
        ([], {"count": 0, "total": 0, "average": 0.0, "min": None, "max": None}),
        ([-3, 0, 9], {"count": 3, "total": 6, "average": 2.0, "min": -3, "max": 9}),
        ([7], {"count": 1, "total": 7, "average": 7.0, "min": 7, "max": 7}),
    ]
else:
    raise ValueError("未知固定验收题目")


def check(argument, expected):
    unittest.TestCase().assertEqual(function(argument), expected)


suite = unittest.TestSuite(
    unittest.FunctionTestCase(lambda argument=argument, expected=expected: check(argument, expected),
                              description=f"input={argument!r}, expected={expected!r}")
    for argument, expected in examples
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
passed = result.wasSuccessful() and result.testsRun == 4
if passed:
    print("LEARN_AGENT_FIXED_ACCEPTANCE_PASSED")
sys.exit(0 if passed else 1)
