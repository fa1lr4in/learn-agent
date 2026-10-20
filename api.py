"""Launch a single local API process; never accept model keys from HTTP clients."""

import argparse
import json
import os
import secrets
import socket
from pathlib import Path

import uvicorn

from learn_agent.api import create_app
from learn_agent.permissions import DEFAULT_WORKSPACE


LOG_DIR = Path(__file__).resolve().parent / "logs"


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 learn-agent 本地后端 API（仅限本机访问）")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="启动时固定的已有项目目录")
    args = parser.parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise SystemExit("后端没有读取到 DEEPSEEK_API_KEY；请在配置该环境变量的终端启动。不要将模型密钥发送给 API。")
    token = secrets.token_urlsafe(32)
    info_path = LOG_DIR / ".api-server.json"
    # Bind before writing launch information, so a busy port cannot impersonate this instance.
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 8000))
            listener.listen(128)
        except OSError as error:
            raise SystemExit(f"无法监听 127.0.0.1:8000（{type(error).__name__}）；请先检查端口占用。") from None
        try:
            app = create_app(args.workspace, LOG_DIR, key, token)
        except (OSError, ValueError) as error:
            raise SystemExit(f"无法启动后端：{error}") from None
        info_path.write_text(json.dumps({"url": "http://127.0.0.1:8000", "token": token,
                                         "workspace": str(args.workspace.resolve()), "pid": os.getpid()},
                                        ensure_ascii=False), encoding="utf-8")
        print("本地工作台：http://127.0.0.1:8000/", flush=True)
        print("本地 API：http://127.0.0.1:8000/docs", flush=True)
        print(f"本地访问令牌保存在 {info_path}；仅供本机客户端使用，每次重启更换。", flush=True)
        print("终端保留运行，Ctrl+C 关闭；正在执行的操作会在返回后响应取消。", flush=True)
        try:
            uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000, access_log=False,
                                         timeout_graceful_shutdown=2)).run(sockets=[listener])
        except KeyboardInterrupt:
            print("\n本地后端已关闭。", flush=True)
        finally:
            if info_path.exists() and json.loads(info_path.read_text(encoding="utf-8")).get("token") == token:
                info_path.unlink()


if __name__ == "__main__":
    main()
