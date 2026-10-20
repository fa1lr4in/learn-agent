"""Per-run task steps backed by actual tool-call results."""

import copy
import json

from .events import record_event


PLAN_INSTRUCTION = (
    "本次启用任务计划。先用 update_plan 建立步骤，再开始其他工具；"
    "每项包含 step、status（pending/in_progress/completed）和 evidence（工具调用 ID 数组）。"
    "结果未知时不能写已完成。测试失败后更新计划为修复或重验，再修改与重新运行；"
    "完成步骤必须引用本次真实成功结果的调用 ID，不能引用 update_plan 自己。"
    "读取源码不等于测试通过，计划更新本身不会执行项目代码。"
    "源码修改后需要新运行结果，最终回答前将所有步骤更新为 completed。"
    "新任务的计划从空开始，不重放历史步骤，不沿用旧工具结果作为本次完成证据。"
)
PLAN_SCHEMA = {"type": "function", "function": {
    "name": "update_plan", "description": "更新本次任务的完整步骤列表，不修改项目文件。completed 必须引用本次已成功执行的工具调用 ID；失败结果可用于进行中的诊断。",
    "parameters": {"type": "object", "properties": {"steps": {
        "type": "array", "minItems": 1, "maxItems": 10, "items": {
            "type": "object", "properties": {
                "step": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                "evidence": {"type": "array", "items": {"type": "string"}},
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
        if not isinstance(item, dict) or set(item) != {"step", "status", "evidence"}:
            raise ValueError("每个计划步骤必须包含 step、status、evidence。")
        if not isinstance(item["step"], str) or not item["step"].strip() or len(item["step"]) > 200:
            raise ValueError("步骤说明必须是 1 至 200 字符的非空文本。")
        if item["status"] not in ("pending", "in_progress", "completed"):
            raise ValueError("计划状态无效。")
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

    def check_evidence(self, steps: list[dict]) -> None:
        for item in steps:
            evidence = item["evidence"]
            if any(call_id not in self.results or call_id in self.ambiguous_ids for call_id in evidence):
                raise PlanError("证据必须引用本次已经返回结果的工具调用 ID；不能引用计划工具或未知 ID。")
            if item["status"] != "completed":
                continue
            if not evidence or any(self.results[call_id][0].get("ok") is not True for call_id in evidence):
                raise PlanError("completed 必须有实际成功的工具结果；失败或没有证据不能标为完成。")
            if any(self.results[call_id][0]["tool"] in ("run_python", "run_command") and
                   (self.results[call_id][1] != self.revision or
                    self.latest_runs[self.run_key(self.results[call_id][0])] != call_id) for call_id in evidence):
                raise PlanError("运行证据早于后续文件修改或同一运行的新结果，请按最新结果重新验证并更新计划。")

    def update(self, steps: list[dict]) -> dict:
        self.check_evidence(steps)
        self.steps = copy.deepcopy(steps)
        self.version += 1
        record_event(self.events, "plan_updated", version=self.version, revision=self.revision, steps=self.steps)
        print("\n[任务计划]")
        for index, item in enumerate(self.steps, 1):
            print(f"{index}. [{item['status']}] {item['step']}；证据：{', '.join(item['evidence']) or '无'}")
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
        }, ensure_ascii=False)

    def as_tool(self):
        from .tools import PreparedTool, Tool
        return Tool(schema=PLAN_SCHEMA, validate=validate_steps,
                    prepare=lambda args: PreparedTool("更新任务计划", lambda: self.update(args["steps"])),
                    internal=True)
