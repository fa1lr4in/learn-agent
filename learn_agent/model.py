"""DeepSeek HTTP requests and local API-key lookup."""

import getpass
import json
import os
import urllib.request


def call_model(payload: dict, api_key: str) -> dict:
    """Send JSON to the model service and decode the JSON response."""
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def get_api_key() -> str:
    """Read a key from the environment or the local terminal without saving it."""
    try:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            api_key = getpass.getpass("\n请输入 DeepSeek API Key（输入不显示）：").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n已取消输入。") from None
    if not api_key:
        raise SystemExit("未提供 API Key。可以先使用 --preview 学习请求结构。")
    return api_key
