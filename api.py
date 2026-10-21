"""Launch a single local API process; never accept model keys from HTTP clients."""

import argparse
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from threading import Thread
from uuid import uuid4

import uvicorn

from learn_agent.api import create_app
from learn_agent.permissions import DEFAULT_WORKSPACE


LOG_DIR = Path(__file__).resolve().parent / "logs"


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 learn-agent 本地后端 API（仅限本机访问）")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="启动时固定的已有项目目录")
    parser.add_argument("--port", type=int, default=8000, help="监听端口；0 表示由系统分配")
    parser.add_argument("--managed", action="store_true", help="由桌面父进程管理；标准输入关闭时收尾退出")
    parser.add_argument("--instance-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log-dir", type=Path, default=LOG_DIR, help="日志和会话数据库目录")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port 必须在 0 至 65535 之间。")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise SystemExit("后端没有读取到 DEEPSEEK_API_KEY；请在配置该环境变量的终端启动。不要将模型密钥发送给 API。")
    token = secrets.token_urlsafe(32)
    log_dir = args.log_dir.resolve()
    info_path = log_dir / ".api-server.json"
    instance_id = args.instance_id or uuid4().hex
    # Bind before writing launch information, so a busy port cannot impersonate this instance.
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
            listener.listen(128)
        except OSError as error:
            raise SystemExit(f"无法监听 127.0.0.1:{args.port}（{type(error).__name__}）；请先检查端口占用。") from None
        port = listener.getsockname()[1] if args.port == 0 else args.port
        try:
            app = create_app(args.workspace, log_dir, key, token, port=port, instance_id=instance_id)
        except (OSError, ValueError) as error:
            raise SystemExit(f"无法启动后端：{error}") from None
        info = {"url": f"http://127.0.0.1:{port}", "token": token,
                "workspace": str(args.workspace.resolve()), "pid": os.getpid(),
                "parent_pid": os.getppid(), "instance_id": instance_id}
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False,
                                              timeout_graceful_shutdown=2))
        if args.managed:
            def watch_parent():
                # EOF is an owned pipe, not a public HTTP shutdown capability.
                try:
                    while sys.stdin.buffer.read(1):
                        pass
                finally:
                    app.state.manager.close()  # cancel, wake approvals, persist, then release the server
                    server.should_exit = True

            Thread(target=watch_parent, name="desktop-parent", daemon=True).start()
            print("LEARN_AGENT_READY " + json.dumps(info, ensure_ascii=False), flush=True)
        else:
            info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            print(f"本地工作台：{info['url']}/", flush=True)
            print(f"本地 API：{info['url']}/docs", flush=True)
            print(f"本地访问令牌保存在 {info_path}；仅供本机客户端使用，每次重启更换。", flush=True)
            print("终端保留运行，Ctrl+C 关闭；正在执行的操作会在返回后响应取消。", flush=True)
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            print("\n本地后端已关闭。", flush=True)
        finally:
            if not args.managed and info_path.exists() and json.loads(info_path.read_text(encoding="utf-8")).get("token") == token:
                info_path.unlink()


if __name__ == "__main__":
    main()
