"""Small, interactive fixed-task evaluation of the existing Agent CLI."""

import argparse
import ast
import hashlib
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

from . import commands, permissions
from .budget import TOKEN_FIELDS
from .tasks import replay_events


REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "logs"
AGENT_ENTRY = REPO / "tool_call.py"
SCORING_VERSION = 1


def load_suite():
    suite = json.loads((REPO / "evals" / "cases.json").read_text(encoding="utf-8"))
    grader = (REPO / "evals" / "grade.py").read_text(encoding="utf-8")
    content = json.dumps({"suite": suite, "grader": grader, "scoring": SCORING_VERSION}, ensure_ascii=False, sort_keys=True)
    return suite["cases"], grader, hashlib.sha256(content.encode("utf-8")).hexdigest()


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.relative_to(root).parts}


def launch_agent(workspace, prompt, max_model_calls):
    # Inherit the terminal so all existing edit/execution approvals still apply.
    return subprocess.run([sys.executable, "-B", "-X", "utf8", str(AGENT_ENTRY), "--workspace", str(workspace),
                           "--auto-read", "--stream", "--max-model-calls", str(max_model_calls), prompt],
                          cwd=REPO, check=False).returncode


def find_run(workspace, previous):
    candidates = []
    for path in set(LOG_DIR.glob("run-*.jsonl")) - previous:
        if ".child-" in path.name:
            continue
        entries = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]
        if entries and entries[0].get("workspace") == str(workspace):
            candidates.append((path, entries))
    if len(candidates) != 1:
        raise ValueError("未找到唯一对应本题 workspace 的运行日志。")
    path, entries = candidates[0]
    status, _ = replay_events(entries)
    if not status.ended:
        raise ValueError("Agent 日志没有完整结束，不能当作任务完成。")
    return path, entries


def passed_process(result):
    return (result.get("ok") is True and result.get("exit_code") == 0 and not result.get("error")
            and not any(result.get(key) for key in ("timed_out", "stdout_truncated", "stderr_truncated"))
            and result.get("cleanup_succeeded") is not False)


def ran_file(result, name):
    if result.get("tool") == "run_python":
        return Path(result.get("path", "")).as_posix() == name
    argv = result.get("command", [])
    return name in argv or (name.startswith("test_") and "unittest" in argv and "-m" in argv)


def grade(case, workspace, before, entries, grader):
    results = [e["result"] for e in entries if e["event"] == "tool_result"]
    responses = [e["response"]["choices"][0] for e in entries if e["event"] == "model_response"]
    after = snapshot(workspace)
    editable = set(case["editable"])
    checks = {
        "normal_stop": entries[-1].get("reason") == "stop" and bool(responses) and responses[-1]["finish_reason"] == "stop",
        "unchanged_other_files": set(after) == set(before) and all(after[p] == value for p, value in before.items() if p not in editable),
    }
    acceptance = None
    if case["id"] == "read_facts":
        checks["read_source"] = any(r.get("tool") == "read_file" and Path(r.get("path", "")).as_posix() == "project.txt"
                                     and r.get("ok") is True and r.get("content") == case["files"]["project.txt"] for r in results)
        try:
            answer = json.loads(responses[-1]["message"]["content"])
        except (ValueError, TypeError, IndexError, KeyError):
            answer = None
        checks["exact_json_answer"] = answer == case["answer"]
        checks["no_execution"] = not any(e["event"] == "tool_started" and e["tool"] not in ("read_file", "search_text", "list_files") for e in entries)
    else:
        checks["changed_target"] = any(after.get(p) != before[p] for p in editable)
        executions = [r for r in results if r.get("tool") in ("run_command", "run_python") and passed_process(r)]
        checks["agent_ran_tests"] = any(ran_file(r, case["test_file"]) and "Ran 4 tests" in r.get("stderr", "") and "OK" in r.get("stderr", "") for r in executions)
        expected_entry = ast.literal_eval(case["entry_stdout"])
        checks["agent_ran_entry"] = False
        for result in executions:
            if ran_file(result, "main.py"):
                try:
                    checks["agent_ran_entry"] |= ast.literal_eval(result.get("stdout", "").strip()) == expected_entry
                except (ValueError, SyntaxError):
                    pass
        if entries[-1].get("reason") == "stop":
            with permissions.use_workspace(workspace):
                acceptance = commands.run_command([sys.executable, "-I", "-B", "-X", "utf8", "-c", grader, case["id"], str(workspace)], timeout=10)
        checks["fixed_acceptance"] = (acceptance is not None and passed_process(acceptance)
                                      and "LEARN_AGENT_FIXED_ACCEPTANCE_PASSED" in acceptance["stdout"].splitlines())
    final = snapshot(workspace)
    checks["unchanged_other_files"] &= set(final) == set(before) and all(final[p] == value for p, value in before.items() if p not in editable)
    return checks, acceptance


def aggregate(rows, planned):
    usage = {key: sum(row["usage"].get(key) or 0 for row in rows)
             if rows and all(type(row["usage"].get(key)) is int for row in rows) else None for key in TOKEN_FIELDS}
    return {"planned": planned, "attempted": len(rows), "passed": sum(row["passed"] for row in rows),
            "failed": sum(not row["passed"] for row in rows),
            "complete": len(rows) == planned and not any(row["reason"] in ("cancelled", "user_denied") for row in rows),
            "success_rate": sum(row["passed"] for row in rows) / len(rows) if rows else None,
            "mean_agent_seconds": statistics.mean(row["agent_seconds"] for row in rows) if rows else None,
            "usage": usage, "known_total_tokens": sum(row.get("reported_usage", row["usage"]).get("total_tokens") or 0 for row in rows),
            "usage_complete": all(value is not None for value in usage.values())}


def compare(report, baseline):
    if not isinstance(baseline, dict) or baseline.get("schema_version") != 1 or baseline.get("suite_hash") != report["suite_hash"]:
        raise ValueError("报告格式、固定题目或验收版本不同，不能比较。")
    if baseline.get("config", {}).get("repeat") != report["config"]["repeat"]:
        raise ValueError("重复次数不同，不能作为本次固定任务对照。")
    old, new = baseline["summary"], report["summary"]
    if not old["complete"] or not new["complete"]:
        return {"comparable": False, "reason": "至少一组未完成全部题目，保留记录，不计算差值。"}
    return {"comparable": True, "baseline_label": baseline["label"],
            "success_rate_delta": new["success_rate"] - old["success_rate"],
            "mean_agent_seconds_delta": new["mean_agent_seconds"] - old["mean_agent_seconds"],
            "total_tokens_delta": new["usage"]["total_tokens"] - old["usage"]["total_tokens"]
            if type(new["usage"]["total_tokens"]) is int and type(old["usage"]["total_tokens"]) is int else None}


def save_report(report, folder):
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = report["summary"]
    rate = f"{summary['success_rate']:.1%}" if summary["success_rate"] is not None else "未知"
    lines = [f"# 固定任务评测：{report['label']}", "", f"已尝试 {summary['attempted']}/{summary['planned']}；通过 {summary['passed']}，失败 {summary['failed']}；已尝试任务成功率 {rate}。",
             f"全组完成：{summary['complete']}；用量完整：{summary['usage_complete']}；总 token：{summary['usage']['total_tokens']}。", "",
             "耗时包含人工确认等待。验收与修改范围由固定条件判断，模型声明完成不计为通过。", "",
             "| 题目 | 重复 | 通过 | 原因 | Agent 秒数 | 总 token |", "| --- | --- | --- | --- | --- | --- |"]
    for row in report["runs"]:
        lines.append(f"| {row['task_id']} | {row['repeat']} | {row['passed']} | {row['reason']} | {row['agent_seconds']:.3f} | {row['usage'].get('total_tokens')} |")
    for row in report["runs"]:
        lines += ["", f"## {row['task_id']} / {row['repeat']}", "", f"工作目录：`{row['workspace']}`", f"日志：`{row['log']}`",
                  "", "```json", json.dumps(row["checks"], ensure_ascii=False, indent=2), "```"]
        if row.get("error"):
            lines += ["", row["error"]]
        if row.get("acceptance") is not None:
            lines += ["", "固定验收的实际结果：", "", "```json", json.dumps(row["acceptance"], ensure_ascii=False, indent=2), "```"]
    if report.get("comparison"):
        lines += ["", "## 对照", "", "```json", json.dumps(report["comparison"], ensure_ascii=False, indent=2), "```"]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_suite(label, repeat=1, max_model_calls=8, baseline=None):
    cases, grader, suite_hash = load_suite()
    report = {"schema_version": 1, "suite_hash": suite_hash, "label": label,
              "started_at": datetime.now(timezone.utc).isoformat(), "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
              "config": {"repeat": repeat, "max_model_calls": max_model_calls, "auto_read": True}, "runs": []}
    report["summary"] = aggregate([], len(cases) * repeat)
    if baseline is not None:
        compare(report, baseline)  # Check compatibility before any model calls.
    try:
        report["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
        report["code_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain", "--", "learn_agent", "tool_call.py", "eval.py", "evals"], cwd=REPO))
    except (OSError, subprocess.CalledProcessError):
        report["git_head"], report["code_dirty"] = None, None
    folder = LOG_DIR / "evals" / ("eval-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + uuid4().hex[:8])
    folder.mkdir(parents=True)
    save_report(report, folder)
    print(f"[评测报告] {folder / 'report.json'}\n固定 {len(cases)} 题，每题 {repeat} 次；逐题新建 workspace，修改/执行仍需确认，结束后评测器运行固定验收。")
    cancelled = False
    for number in range(1, repeat + 1):
        for case in cases:
            workspace = folder / f"{case['id']}-{number}" / "workspace"
            workspace.mkdir(parents=True)
            for name, text in case["files"].items():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            before = snapshot(workspace)
            previous = set(LOG_DIR.glob("run-*.jsonl"))
            row = {"task_id": case["id"], "repeat": number, "workspace": str(workspace), "log": None,
                   "passed": False, "reason": "error", "checks": {}, "usage": dict.fromkeys(TOKEN_FIELDS), "agent_seconds": 0.0}
            start = monotonic()
            print(f"\n[固定任务] {case['title']}；第 {number}/{repeat} 次")
            try:
                row["agent_exit_code"] = launch_agent(workspace, case["prompt"], max_model_calls)
                row["agent_seconds"] = monotonic() - start
                path, entries = find_run(workspace, previous)
                row.update(log=str(path), reason=entries[-1]["reason"], usage=entries[-1].get("summary", {}).get("usage", dict.fromkeys(TOKEN_FIELDS)))
                row["reported_usage"] = entries[-1].get("summary", {}).get("reported_usage", row["usage"])
                row["model_calls"] = entries[-1].get("summary", {}).get("model_calls")
                row["checks"], row["acceptance"] = grade(case, workspace, before, entries, grader)
                row["checks"]["cli_exit_zero"] = row["agent_exit_code"] == 0
                row["passed"] = all(row["checks"].values())
                cancelled = row["reason"] in ("cancelled", "user_denied")
                if row["acceptance"] and row["acceptance"].get("cleanup_succeeded") is False:
                    cancelled = True
                    row["error"] = "验收进程清理未确认，停止后续评测。"
            except KeyboardInterrupt:
                row.update(reason="cancelled", agent_seconds=monotonic() - start, error="评测已取消；保留已完成结果，不启动后续题目。")
                cancelled = True
            except (ValueError, OSError) as error:
                row["error"] = str(error)
                if row["agent_seconds"] == 0.0:
                    row["agent_seconds"] = monotonic() - start
            report["runs"].append(row)
            report["summary"] = aggregate(report["runs"], len(cases) * repeat)
            if cancelled:
                report["summary"]["complete"] = False
            if baseline is not None:
                report["comparison"] = compare(report, baseline)
            save_report(report, folder)
            failed = [name for name, passed in row["checks"].items() if not passed]
            print(f"[判定] {case['id']}：{'通过' if row['passed'] else '未通过'}；停止原因={row['reason']}；未通过检查={', '.join(failed) or row.get('error') or '无'}")
            if cancelled:
                break
        if cancelled:
            break
    print(f"\n[评测汇总] {json.dumps(report['summary'], ensure_ascii=False)}\n[JSON] {folder / 'report.json'}\n[Markdown] {folder / 'report.md'}")
    return folder / "report.json", report


def main():
    parser = argparse.ArgumentParser(description="用固定任务评测当前 Agent；会调用真实模型并执行固定验收")
    parser.add_argument("--label", required=True, help="给本组结果命名，不用于构造文件路径")
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 6), help="每题独立重复次数，默认1，最多5")
    parser.add_argument("--max-model-calls", type=int, default=8, choices=range(1, 33), help="每题模型请求上限，默认8，最多32")
    parser.add_argument("--compare", metavar="REPORT", help="对照同题目和重复次数的旧 JSON 报告")
    args = parser.parse_args()
    try:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8")) if args.compare else None
        run_suite(args.label, args.repeat, args.max_model_calls, baseline)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SystemExit(f"评测停止：{error}") from None
