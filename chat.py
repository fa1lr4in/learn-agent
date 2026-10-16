"""Chat with DeepSeek and inspect requests and responses."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description="调用 DeepSeek 并查看请求与响应")
    parser.add_argument("prompt", nargs="?", default="请用两句话解释什么是 Agent。")
    parser.add_argument("--preview", action="store_true", help="只查看请求体，不连接 API")
    parser.add_argument("--chat", action="store_true", help="继续对话，输入 /clear 清空历史，/exit 退出")
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

    api_key = get_api_key()
    while True:
        try:
            response = call_model(payload, api_key)
        except urllib.error.HTTPError as error:
            raise SystemExit(f"DeepSeek 返回 HTTP {error.code}，请检查密钥、余额、模型参数或请求频率。") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise SystemExit(f"连接模型服务失败：{error}") from None

        print("\n[模型服务返回的 JSON]")
        print(json.dumps(response, ensure_ascii=False, indent=2))

        choice = response["choices"][0]
        content = choice["message"].get("content") or ""
        print("\n[助手回答]")
        print(content or "（本次没有返回文本回答）")
        print(f"\n停止原因：{choice['finish_reason']}")
        if choice["finish_reason"] == "length":
            print("提示：输出达到长度限制，回答可能不完整。")

        usage = response.get("usage") or {}
        print(
            f"Token 用量：输入 {usage.get('prompt_tokens', '未知')}，"
            f"输出 {usage.get('completion_tokens', '未知')}，"
            f"合计 {usage.get('total_tokens', '未知')}"
        )
        messages.append({"role": "assistant", "content": content})
        if not args.chat:
            return

        try:
            while True:
                prompt = input("\n你> ").strip()
                if not prompt:
                    continue
                if prompt.lower() == "/exit":
                    print("对话已结束。")
                    return
                if prompt.lower() == "/clear":
                    del messages[1:]
                    print("已清空对话历史，保留系统指令。")
                    continue
                break
        except (EOFError, KeyboardInterrupt):
            print("\n对话已结束。")
            return

        messages.append({"role": "user", "content": prompt})
        print("\n[发送给模型的请求体]")
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
