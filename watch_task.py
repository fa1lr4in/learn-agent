"""Observe the local task SSE API, without submitting tasks or approving tools."""

import argparse
import http.client
import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent / "logs"


def parse_events(lines):
    """Read this API's JSON envelopes; incomplete frames never advance the cursor."""
    fields, data = {}, []
    for raw in lines:
        line = raw.decode("utf-8-sig").rstrip("\r\n")
        if not line:
            if data:
                frame = json.loads("\n".join(data))
                if not isinstance(frame, dict) or frame.get("id") != fields.get("id") or frame.get("event") != fields.get("event"):
                    raise ValueError("SSE 字段与事件内容不一致。")
                yield frame
            fields, data = {}, []
        elif not line.startswith(":"):
            key, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "data":
                data.append(value)
            elif key in ("id", "event"):
                fields[key] = value


def event_sequence(cursor: str, task_id: str) -> int:
    owner, separator, number = cursor.partition(":")
    if owner != task_id or not separator or not number.isascii() or not number.isdigit() or len(number) > 12:
        raise ValueError("续收游标不属于本任务或格式无效。")
    return int(number)


def save_cursor(path: Path, task_id: str, cursor: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".watch-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"task_id": task_id, "last_event_id": cursor}, stream)
        for attempt in range(6):
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                # Windows readers may temporarily deny replacement of an open file.
                if getattr(error, "winerror", None) not in (5, 32, 33):
                    raise
                if attempt == 5:
                    raise PermissionError(f"续收游标文件被占用或没有写入权限：{path}。未推进保存的游标；后台任务继续运行，可稍后用 --resume 续收。") from error
                time.sleep(.025 * 2 ** attempt)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def show_event(frame: dict) -> None:
    kind, data = frame["event"], frame["data"]
    label = f"[{frame['id'].rsplit(':', 1)[1]}] {kind}"
    if kind == "model_delta":
        delta = data["delta"]
        print(f"{label} {delta.get('content') or '（工具请求片段）'}", flush=True)
    elif kind in ("snapshot", "task_finished"):
        print(f"{label} · {data['state']} / {data['reason']} · finished={data['finished']}", flush=True)
        if kind == "snapshot":
            print(f"快照包含 {len(data['events'])} 条运行记录。", flush=True)
        if data.get("pending_approval"):
            print(f"待确认：{data['pending_approval']['action']}", flush=True)
        if data.get("final_answer"):
            print(data["final_answer"], flush=True)
    elif kind == "approval_required":
        print(f"{label} {data['action']}（在提交任务的终端确认）", flush=True)
    elif kind == "tool_result":
        print(f"{label} {data['tool']} · ok={data['result'].get('ok')}", flush=True)
    else:
        print(label, flush=True)


def watch(info: dict, task_id: str, cursor_path: Path, cursor: str | None = None, limit: int | None = None) -> None:
    address = urllib.parse.urlsplit(info["url"])
    if address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost") or address.username or address.password or address.path or address.query or address.fragment:
        raise ValueError("仅连接本机 HTTP 后端，不向其他地址发送本地访问令牌。")
    sequence = event_sequence(cursor, task_id) if cursor is not None else -1
    received, retries = 0, 0
    while True:
        headers = {"Authorization": f"Bearer {info['token']}", "Accept": "text/event-stream"}
        if cursor is not None:
            headers["Last-Event-ID"] = cursor
        request = urllib.request.Request(f"{info['url']}/tasks/{task_id}/events", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ValueError("后端没有返回 SSE。")
                for frame in parse_events(response):
                    if frame.get("task_id") != task_id:
                        raise ValueError("收到其他任务的事件，已停止观察。")
                    current = event_sequence(frame["id"], task_id)
                    if current <= sequence:
                        continue
                    if frame["event"] != "snapshot" and current != sequence + 1:
                        raise ValueError("事件序号不连续，请重新获取快照。")
                    show_event(frame)
                    save_cursor(cursor_path, task_id, frame["id"])
                    cursor, sequence, retries = frame["id"], current, 0
                    received += 1
                    if frame["event"] == "task_finished" or (frame["event"] == "snapshot" and frame["data"]["finished"]):
                        return
                    if limit is not None and received >= limit:
                        print(f"已主动断开观察连接；任务继续运行。续收游标：{cursor}", flush=True)
                        return
            # A completed stream resumed exactly at its final cursor has nothing left to replay.
            check = urllib.request.Request(f"{info['url']}/tasks/{task_id}", headers={"Authorization": headers["Authorization"]})
            with urllib.request.urlopen(check, timeout=10) as response:
                snapshot = json.load(response)
            if snapshot["finished"] and snapshot["last_event_id"] == cursor:
                print(f"任务已结束：{snapshot['state']} / {snapshot['reason']}。", flush=True)
                return
        except urllib.error.HTTPError as error:
            raise ValueError(f"HTTP {error.code}；请检查服务、任务 ID 与游标，不会重新提交任务。") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, UnicodeError):
            pass
        retries += 1
        if retries > 3:
            raise OSError("连接中断，自动续收三次仍失败；原任务没有被取消，可稍后使用 --resume。")
        print("观察连接断开，按已保存游标重连……", flush=True)
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="只读观察任务事件；断开或 Ctrl+C 不取消任务")
    parser.add_argument("task_id", help="提交任务时返回的 id")
    parser.add_argument("--resume", action="store_true", help="从本任务上次保存的游标补收")
    parser.add_argument("--limit", type=int, help="收到指定条数后主动断开，用于验证续收")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.task_id) or (args.limit is not None and args.limit <= 0):
        parser.error("task_id 必须是后端返回的 32 位 ID，limit 必须为正整数。")
    path = LOG_DIR / f".watch-{args.task_id}.json"
    try:
        info = json.loads((LOG_DIR / ".api-server.json").read_text(encoding="utf-8"))
        cursor = None
        if args.resume:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["task_id"] != args.task_id:
                raise ValueError("保存的游标属于其他任务。")
            cursor = saved["last_event_id"]
        watch(info, args.task_id, path, cursor, args.limit)
    except KeyboardInterrupt:
        print("\n已停止观察；后台任务继续运行，稍后可用 --resume 续收。")
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"无法观察任务：{error}") from None


if __name__ == "__main__":
    main()
