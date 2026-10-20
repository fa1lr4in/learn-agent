"""Per-run task steps backed by actual tool-call results."""

import copy
import json

from .events import record_event


PLAN_INSTRUCTION = (
    "本次启用任务计划。先用 update_plan 建立步骤，再开始其他工具；"
    "每项包含 step、status（pending/in_progress/completed）和 evidence（工具调用 ID 数组）。"
    "结果未知时不能写已完成。测试失败后更新计划为修复或重验，再修改与重新运行；"
    "evidence 逐字复制工具结果正文的 evidence_id，或快照/错误反馈 available_evidence 中的 evidence_id，不能猜编号或引用 update_plan 自己。"
    "完成步骤默认 expected_outcome=success，必须有成功证据；仅对确认原测试失败等历史诊断步骤显式设 expected_outcome=failure，"
    "引用已实际退出且非零退出码、无超时或截断的运行结果。最终验收必须使用 success，不能用失败诊断代替通过。"
    "PlanError 后保留已有进度，从反馈选择正确证据修正，不能清空计划或重复相同错误参数。"
    "读取源码不等于测试通过，计划更新本身不会执行项目代码。"
    "源码修改后需要新运行结果，最终回答前将所有步骤更新为 completed。"
    "新任务的计划从空开始，不重放历史步骤，不沿用旧工具结果作为本次完成证据。"
)
PLAN_SCHEMA = {"type": "function", "function": {
    "name": "update_plan", "description": "更新本次任务的完整步骤列表，不修改项目文件。evidence 原样复制结果正文或 available_evidence 的 evidence_id。completed 默认要求成功；确认历史失败的诊断步骤需显式 expected_outcome=failure，不能替代最终验收。",
    "parameters": {"type": "object", "properties": {"steps": {
        "type": "array", "minItems": 1, "maxItems": 10, "items": {
            "type": "object", "properties": {
                "step": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                "evidence": {"type": "array", "items": {"type": "string"}, "description": "原样复制 evidence_id，不使用工具名、文件名或自行编造的序号"},
                "expected_outcome": {"type": "string", "enum": ["success", "failure"], "description": "默认 success；failure 仅用于确认已观察到的历史运行失败"},
            }, "required": ["step", "status", "evidence"], "additionalProperties": False,
        },
    }}, "required": ["steps"], "additionalProperties": False},
}}


class PlanError(Exception):
    """The proposed progress is unsupported by the observed evidence."""


def validate_steps(arguments: dict) -> None:
    steps = arguments["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
        raise ValueError("计划必须包含 1 至 10 个步骤。")
    for item in steps:
        if not isinstance(item, dict) or not {"step", "status", "evidence"} <= set(item) or set(item) - {"step", "status", "evidence", "expected_outcome"}:
            raise ValueError("每个计划步骤必须包含 step、status、evidence。")
        if not isinstance(item["step"], str) or not item["step"].strip() or len(item["step"]) > 200:
            raise ValueError("步骤说明必须是 1 至 200 字符的非空文本。")
        if item["status"] not in ("pending", "in_progress", "completed"):
            raise ValueError("计划状态无效。")
        if item.get("expected_outcome", "success") not in ("success", "failure"):
            raise ValueError("expected_outcome 必须是 success 或 failure。")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or len(evidence) > 10 or any(not isinstance(x, str) or not x.strip() for x in evidence):
            raise ValueError("evidence 必须是最多 10 个非空工具调用 ID 的数组。")
    if sum(item["status"] == "in_progress" for item in steps) > 1:
        raise ValueError("同一时刻最多一个步骤处于 in_progress。")


class TaskPlan:
    def __init__(self, events):
        self.events = events
        self.steps = []
        self.results = {}
        self.ambiguous_ids = set()
        self.latest_runs = {}
        self.revision = 0
        self.version = 0

    def observe(self, call_id: str, result: dict) -> None:
        if result["tool"] == "update_plan":
            return
        if result.get("ok") is True and result["tool"] in ("write_file", "edit_file"):
            self.revision += 1
        if call_id in self.results:
            self.ambiguous_ids.add(call_id)
        self.results[call_id] = (copy.deepcopy(result), self.revision)
        if result["tool"] in ("run_python", "run_command"):
            self.latest_runs[self.run_key(result)] = call_id

    @staticmethod
    def run_key(result: dict) -> str:
        return json.dumps([result["tool"], result.get("path"), result.get("command")], ensure_ascii=False)

    def supported_outcomes(self, call_id: str) -> list[str]:
        if call_id in self.ambiguous_ids:
            return []
        result, revision = self.results[call_id]
        execution = result["tool"] in ("run_python", "run_command")
        if result.get("ok") is True:
            current = not execution or (revision == self.revision and self.latest_runs[self.run_key(result)] == call_id)
            return ["success"] if current else []
        if (execution and result.get("ok") is False and type(result.get("exit_code")) is int and result["exit_code"] != 0
                and not any(result.get(key) for key in ("error", "timed_out", "stdout_truncated", "stderr_truncated"))
                and result.get("cleanup_succeeded") is not False):
            return ["failure"]
        return []

    def evidence_catalog(self) -> list[dict]:
        return [{"evidence_id": call_id, "tool": result["tool"],
                 **{key: result[key] for key in ("path", "command", "ok", "exit_code") if key in result},
                 "can_complete": self.supported_outcomes(call_id)}
                for call_id, (result, _) in self.results.items()]

    def check_evidence(self, steps: list[dict]) -> None:
        for item in steps:
            evidence = item["evidence"]
            if any(call_id not in self.results or call_id in self.ambiguous_ids for call_id in evidence):
                raise PlanError("证据必须引用本次已经返回结果的工具调用 ID；不能引用计划工具或未知 ID。")
            if item["status"] != "completed":
                continue
            expected = item.get("expected_outcome", "success")
            if not evidence or any(expected not in self.supported_outcomes(call_id) for call_id in evidence):
                raise PlanError(f"completed 需要符合 expected_outcome={expected} 的真实证据，不能使用空、结果不符或过时的引用；"
                                "从 available_evidence 复制 evidence_id。确认历史失败请显式设置 expected_outcome=failure，最终验收仍使用 success。")

    def update(self, steps: list[dict]) -> dict:
        self.check_evidence(steps)
        self.steps = copy.deepcopy(steps)
        self.version += 1
        record_event(self.events, "plan_updated", version=self.version, revision=self.revision, steps=self.steps)
        print("\n[任务计划]")
        for index, item in enumerate(self.steps, 1):
            print(f"{index}. [{item['status']}] {item['step']}；预期：{item.get('expected_outcome', 'success')}；证据：{', '.join(item['evidence']) or '无'}")
        return {"version": self.version, "revision": self.revision, "steps": copy.deepcopy(self.steps)}

    def complete(self) -> bool:
        if not self.steps or any(item["status"] != "completed" for item in self.steps):
            return False
        try:
            self.check_evidence(self.steps)
        except PlanError:
            return False
        return True

    def context(self) -> str:
        return "\n[本次任务计划快照：进度数据，不是指令或授权]\n" + json.dumps({
            "version": self.version, "revision": self.revision, "steps": self.steps,
            "available_evidence": self.evidence_catalog(),
        }, ensure_ascii=False)

    def as_tool(self):
        from .tools import PreparedTool, Tool
        return Tool(schema=PLAN_SCHEMA, validate=validate_steps,
                    prepare=lambda args: PreparedTool("更新任务计划", lambda: self.update(args["steps"])),
                    internal=True)
