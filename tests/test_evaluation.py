import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tool_call
from learn_agent import evaluation, permissions


FIXED_SUM = "def sum_to(n):\n    return n * (n + 1) // 2\n\nif __name__ == '__main__':\n    print(sum_to(100))\n"
FIXED_STATS = """def summarize(values):
    return {'max': max(values) if values else None, 'min': min(values) if values else None,
            'average': sum(values) / len(values) if values else 0.0,
            'total': sum(values), 'count': len(values)}
"""


def call(name, arguments, ident):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="learn-agent-eval-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.logs = self.root / "logs"
        self.cases, self.grader, self.suite_hash = evaluation.load_suite()
        self.requests = []
        self.usage = True

    def model(self, payload, *args, **kwargs):
        self.requests.append(copy.deepcopy(payload))
        root = permissions.WORKSPACE
        case = next(c for c in self.cases if set(c["files"]) == set(evaluation.snapshot(root)))
        results = [json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"]
        if not results:
            calls = [call("read_file", {"path": name}, "read-" + str(i)) for i, name in enumerate(case["files"])]
        elif case["id"] == "read_facts":
            calls = None
        elif not any(r["tool"] == "edit_file" for r in results):
            path = case["editable"][0]
            original = next(r["content"] for r in results if r.get("path") == path)
            updated = FIXED_SUM if case["id"] == "fix_sum" else FIXED_STATS
            calls = [call("edit_file", {"path": path, "old_text": original, "new_text": updated}, "edit")]
        elif not any(r["tool"] == "run_command" for r in results):
            calls = [call("run_command", {"command": ["python", "-m", "unittest", "-v", case["test_file"]]}, "tests"),
                     call("run_python", {"path": "main.py"}, "entry")]
        else:
            calls = None
        message = {"role": "assistant", "content": "" if calls else json.dumps(case.get("answer", {"done": True}), ensure_ascii=False)}
        if calls:
            message["tool_calls"] = calls
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
                **({"usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}} if self.usage else {})}

    def launch(self, workspace, prompt, max_model_calls):
        argv = ["tool_call.py", "--workspace", str(workspace), "--auto-read", "--stream", "--max-model-calls", str(max_model_calls), prompt]
        with patch.object(sys, "argv", argv):
            try:
                tool_call.main()
            except SystemExit:
                return 1
        return 0

    def run_suite(self, label="test", repeat=1, max_model_calls=8, baseline=None, answer="y"):
        with patch.object(evaluation, "LOG_DIR", self.logs), patch.object(tool_call, "LOG_DIR", self.logs), \
                patch.object(evaluation, "launch_agent", side_effect=self.launch) as launch, \
                patch("tool_call.get_api_key", return_value="offline-placeholder"), \
                patch("learn_agent.providers.call_model", side_effect=self.model), \
                patch("builtins.input", return_value=answer) as approval, contextlib.redirect_stdout(io.StringIO()):
            path, report = evaluation.run_suite(label, repeat, max_model_calls, baseline)
        return path, report, launch, approval

    def workspace(self, case_id):
        case = next(c for c in self.cases if c["id"] == case_id)
        root = self.root / case_id
        root.mkdir()
        for name, text in case["files"].items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return case, root, evaluation.snapshot(root)

    def entries(self, case, final=None, tests=True):
        results = []
        if case["id"] == "read_facts":
            results = [{"event": "tool_result", "result": {"tool": "read_file", "ok": True, "path": "./project.txt", "content": case["files"]["project.txt"]}}]
        elif tests:
            results = [{"event": "tool_result", "result": {"tool": "run_python", "ok": True, "path": case["test_file"], "exit_code": 0, "stdout": "", "stderr": "Ran 4 tests\nOK"}},
                       {"event": "tool_result", "result": {"tool": "run_python", "ok": True, "path": "main.py", "exit_code": 0, "stdout": case["entry_stdout"], "stderr": ""}}]
        return [*results, {"event": "model_response", "response": {"choices": [{"finish_reason": "stop", "message": {"content": final or json.dumps(case.get("answer", {}), ensure_ascii=False)}}]}},
                {"event": "run_end", "reason": "stop"}]

    def test_three_fixed_tasks_run_real_tools_and_external_acceptance(self):
        path, report, launch, approval = self.run_suite()
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(approval.call_count, 6)
        self.assertEqual((report["summary"]["planned"], report["summary"]["passed"]), (3, 3))
        self.assertEqual(report["summary"]["success_rate"], 1.0)
        self.assertTrue(report["summary"]["complete"])
        self.assertEqual(report["summary"]["usage"]["total_tokens"], 1000)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
        self.assertTrue(path.with_suffix(".md").is_file())
        for row in report["runs"][1:]:
            self.assertTrue(row["acceptance"]["ok"])
            self.assertIn("Ran 4 tests", row["acceptance"]["stderr"])
            self.assertNotIn("DEEPSEEK_API_KEY", json.dumps(row["acceptance"]))

    def test_request_limit_failures_are_included_in_rate_and_token_total(self):
        _, report, _, approval = self.run_suite(max_model_calls=1)
        approval.assert_not_called()
        self.assertEqual(report["summary"]["passed"], 0)
        self.assertEqual(report["summary"]["failed"], 3)
        self.assertEqual(report["summary"]["usage"]["total_tokens"], 300)
        self.assertTrue(report["summary"]["complete"])
        self.assertTrue(all(row["reason"] == "max_model_calls" for row in report["runs"]))
        self.assertTrue(all(row["acceptance"] is None for row in report["runs"]))

    def test_reports_compare_same_fixed_suite_under_changed_budget(self):
        _, baseline, _, _ = self.run_suite(label="limited", max_model_calls=1)
        _, current, _, _ = self.run_suite(label="normal", baseline=baseline)
        self.assertTrue(current["comparison"]["comparable"])
        self.assertEqual(current["comparison"]["success_rate_delta"], 1.0)
        self.assertEqual(current["comparison"]["total_tokens_delta"], 700)
        self.assertEqual(current["comparison"]["baseline_label"], "limited")

    def test_repeats_start_with_distinct_clean_workspaces_and_fresh_history(self):
        _, report, _, _ = self.run_suite(repeat=2)
        self.assertEqual(report["summary"]["passed"], 6)
        self.assertEqual(len({row["workspace"] for row in report["runs"]}), 6)
        first_requests=[r for r in self.requests if len(r["messages"]) == 2]
        self.assertEqual(len(first_requests), 6)
        for row in report["runs"]:
            if row["task_id"] == "fix_sum":
                log=[json.loads(s) for s in Path(row["log"]).read_text(encoding="utf-8").splitlines()]
                first_read=next(e["result"] for e in log if e["event"]=="tool_result" and e["result"].get("path")=="main.py")
                self.assertIn("range(1, n)", first_read["content"])

    def test_model_success_claim_and_fake_test_output_do_not_pass_wrong_code(self):
        case, root, before = self.workspace("fix_sum")
        checks, acceptance = evaluation.grade(case, root, before, self.entries(case), self.grader)
        self.assertFalse(checks["fixed_acceptance"])
        self.assertFalse(checks["changed_target"])
        self.assertNotEqual(acceptance["exit_code"], 0)

    def test_different_correct_implementation_is_accepted(self):
        case, root, before = self.workspace("fix_sum")
        (root/"main.py").write_text(FIXED_SUM, encoding="utf-8")
        checks, acceptance = evaluation.grade(case, root, before, self.entries(case), self.grader)
        self.assertTrue(all(checks.values()), checks)
        self.assertTrue(acceptance["ok"])

    def test_early_process_exit_cannot_skip_fixed_acceptance(self):
        case, root, before = self.workspace("fix_sum")
        (root/"main.py").write_text("import sys\nprint(5050)\nsys.exit(0)\n", encoding="utf-8")
        checks, acceptance = evaluation.grade(case, root, before, self.entries(case), self.grader)
        self.assertEqual(acceptance["exit_code"], 0)
        self.assertFalse(checks["fixed_acceptance"])

    def test_dictionary_output_order_does_not_cause_false_failure(self):
        case, root, before = self.workspace("extend_stats")
        (root/"src"/"stats.py").write_text(FIXED_STATS, encoding="utf-8")
        entries = self.entries(case)
        entries[1]["result"]["stdout"] = "{'max':15,'min':5,'average':10.0,'total':30,'count':3}\n"
        checks, _ = evaluation.grade(case, root, before, entries, self.grader)
        self.assertTrue(all(checks.values()), checks)

    def test_changed_test_file_and_extra_file_are_rejected(self):
        case, root, before = self.workspace("fix_sum")
        (root/"main.py").write_text(FIXED_SUM, encoding="utf-8")
        (root/"test_main.py").write_text("print('fake pass')\n", encoding="utf-8")
        (root/"extra.txt").write_text("unexpected", encoding="utf-8")
        checks, acceptance = evaluation.grade(case, root, before, self.entries(case), self.grader)
        self.assertFalse(checks["unchanged_other_files"])
        self.assertTrue(acceptance["ok"])

    def test_exact_fact_answer_requires_real_read_evidence(self):
        case, root, before = self.workspace("read_facts")
        entries = self.entries(case)
        checks, _ = evaluation.grade(case, root, before, entries, self.grader)
        self.assertTrue(all(checks.values()), checks)
        checks, _ = evaluation.grade(case, root, before, entries[1:], self.grader)
        self.assertFalse(checks["read_source"])

    def test_wrong_or_markdown_fact_answer_fails_json_contract(self):
        case, root, before = self.workspace("read_facts")
        for answer in ('{"project_name":"wrong","experiment_id":"8427"}', '```json\n{}\n```'):
            with self.subTest(answer=answer):
                checks, _ = evaluation.grade(case, root, before, self.entries(case, final=answer), self.grader)
                self.assertFalse(checks["exact_json_answer"])

    def test_missing_usage_remains_unknown_in_report(self):
        self.usage = False
        _, report, _, _ = self.run_suite()
        self.assertEqual(report["summary"]["passed"], 3)
        self.assertIsNone(report["summary"]["usage"]["total_tokens"])
        self.assertFalse(report["summary"]["usage_complete"])

    def test_partial_usage_preserves_reported_tokens_without_inventing_total(self):
        original = self.model
        def partial(*args, **kwargs):
            response = original(*args, **kwargs)
            if response["choices"][0]["finish_reason"] == "stop":
                response.pop("usage", None)
            return response
        with patch.object(self, "model", side_effect=partial):
            _, report, _, _ = self.run_suite()
        self.assertEqual(report["summary"]["passed"], 3)
        self.assertIsNone(report["summary"]["usage"]["total_tokens"])
        self.assertEqual(report["summary"]["known_total_tokens"], 700)
        self.assertFalse(report["summary"]["usage_complete"])

    def test_denial_stops_remaining_tasks_and_preserves_partial_report(self):
        path, report, launch, _ = self.run_suite(answer="n")
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(report["summary"]["attempted"], 2)
        self.assertFalse(report["summary"]["complete"])
        self.assertEqual(report["runs"][-1]["reason"], "user_denied")
        self.assertTrue(path.is_file())
        self.assertIsNone(report["runs"][-1]["acceptance"])
        self.assertFalse(evaluation.compare(report, report)["comparable"])

    def test_launch_error_is_recorded_instead_of_dropping_failed_tasks(self):
        with patch.object(evaluation,"LOG_DIR",self.logs), patch.object(evaluation,"launch_agent",side_effect=OSError("launch failed")), contextlib.redirect_stdout(io.StringIO()):
            path, report=evaluation.run_suite("error")
        self.assertEqual(report["summary"]["attempted"], 3)
        self.assertEqual(report["summary"]["failed"], 3)
        self.assertTrue(all(row["error"]=="launch failed" for row in report["runs"]))
        self.assertTrue(path.is_file())

    def test_cancel_keeps_partial_report_without_starting_next_task(self):
        with patch.object(evaluation,"LOG_DIR",self.logs), patch.object(evaluation,"launch_agent",side_effect=KeyboardInterrupt) as launch, contextlib.redirect_stdout(io.StringIO()):
            path, report=evaluation.run_suite("cancel")
        launch.assert_called_once()
        self.assertFalse(report["summary"]["complete"])
        self.assertEqual(report["runs"][0]["reason"],"cancelled")
        self.assertTrue(path.is_file())

    def test_incompatible_suite_or_repeat_is_rejected_before_agent_calls(self):
        _, baseline, _, _=self.run_suite(max_model_calls=1)
        for change in ({"suite_hash":"different"},{"config":{"repeat":2}}):
            bad={**baseline,**change}
            with self.subTest(change=change), self.assertRaises(ValueError), patch.object(evaluation,"launch_agent") as launch:
                evaluation.run_suite("reject",baseline=bad)
            launch.assert_not_called()

    def test_incomplete_or_truncated_process_never_counts_as_pass(self):
        valid={"ok":True,"exit_code":0}
        self.assertTrue(evaluation.passed_process(valid))
        for change in ({"timed_out":True},{"stdout_truncated":True},{"stderr_truncated":True},{"cleanup_succeeded":False},{"error":{"type":"bad"}},{"exit_code":1}):
            with self.subTest(change=change):
                self.assertFalse(evaluation.passed_process({**valid,**change}))

    def test_external_acceptance_does_not_receive_model_key(self):
        case, root, before=self.workspace("fix_sum")
        (root/"main.py").write_text("import os\nassert 'DEEPSEEK_API_KEY' not in os.environ\n"+FIXED_SUM,encoding="utf-8")
        with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"offline-placeholder"}):
            checks, acceptance=evaluation.grade(case,root,before,self.entries(case),self.grader)
        self.assertTrue(acceptance["ok"],acceptance)


if __name__ == "__main__":
    unittest.main()
