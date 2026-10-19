"""Per-run usage accounting and response-boundary budget checks."""

from dataclasses import dataclass, field
from time import monotonic


TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass
class RunBudget:
    max_model_calls: int
    max_total_tokens: int | None = None
    model_calls: int = field(default=0, init=False)
    responses_received: int = field(default=0, init=False)
    reported_usage: dict = field(default_factory=lambda: dict.fromkeys(TOKEN_FIELDS, 0), init=False)
    usage_reports: dict = field(default_factory=lambda: dict.fromkeys(TOKEN_FIELDS, 0), init=False)
    started_at: float = field(default_factory=lambda: monotonic(), init=False)

    def __post_init__(self):
        if type(self.max_model_calls) is not int or self.max_model_calls <= 0:
            raise ValueError("模型请求次数预算必须是正整数。")
        if self.max_total_tokens is not None and (type(self.max_total_tokens) is not int or self.max_total_tokens <= 0):
            raise ValueError("总 token 预算必须是正整数。")

    def observe(self, response: dict) -> None:
        self.responses_received += 1
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        values = []
        for name in TOKEN_FIELDS:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                self.reported_usage[name] += value
                self.usage_reports[name] += 1
                values.append(str(value))
            else:
                values.append("未提供")
        print(f"[本轮用量] 输入 {values[0]}，输出 {values[1]}，合计 {values[2]} token")

    def pause_reason(self) -> str | None:
        if self.max_total_tokens is None:
            return None
        if self.usage_reports["total_tokens"] != self.model_calls:
            return "usage_unavailable"
        if self.reported_usage["total_tokens"] >= self.max_total_tokens:
            return "max_total_tokens"
        return None

    def summary(self) -> dict:
        usage = {name: self.reported_usage[name] if self.model_calls and self.usage_reports[name] == self.model_calls else None
                 for name in TOKEN_FIELDS}
        return {
            "model_calls": self.model_calls,
            "responses_received": self.responses_received,
            "elapsed_seconds": round(monotonic() - self.started_at, 3),
            "usage": usage,
            "usage_complete": all(value is not None for value in usage.values()),
            "reported_usage": dict(self.reported_usage),
            "usage_reports": dict(self.usage_reports),
        }


def print_summary(summary: dict) -> None:
    print(f"\n[任务用量] 模型请求 {summary['model_calls']} 次，完整回复 {summary['responses_received']} 次，耗时 {summary['elapsed_seconds']:.3f} 秒（包含等待确认）")
    for name, label in zip(TOKEN_FIELDS, ("输入", "输出", "合计")):
        value = summary["usage"][name]
        if value is None:
            print(f"{label} token：未知；已报告部分合计 {summary['reported_usage'][name]}，覆盖 {summary['usage_reports'][name]}/{summary['model_calls']} 次请求")
        else:
            print(f"{label} token：{value}")
