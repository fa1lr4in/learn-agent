"""Lesson 01: inspect a single DeepSeek model request and response."""

import argparse
import getpass
import json
import os
import urllib.error
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


def main() -> None:
    parser = argparse.ArgumentParser(description="第一课：观察一次模型调用")
    parser.add_argument("prompt", nargs="?", default="请用两句话解释什么是 Agent。")
    parser.add_argument("--preview", action="store_true", help="只查看请求体，不连接 API")
    args = parser.parse_args()

    messages = [
        {"role": "system", "content": "你是一位编程老师，用简洁的中文回答问题。"},
        {"role": "user", "content": args.prompt},
    ]
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "messages": messages,
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 512,
    }

    print("\n[发送给模型的请求体]")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.preview:
        print("\n预览结束，没有发送网络请求。")
        return

    try:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            api_key = getpass.getpass("\n请输入 DeepSeek API Key（输入不显示）：").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n已取消输入。") from None
    if not api_key:
        raise SystemExit("未提供 API Key。可以先使用 --preview 学习请求结构。")

    try:
        response = call_model(payload, api_key)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"DeepSeek 返回 HTTP {error.code}，请按第一课的错误排查说明检查。") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise SystemExit(f"连接模型服务失败：{error}") from None

    print("\n[模型服务返回的 JSON]")
    print(json.dumps(response, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
