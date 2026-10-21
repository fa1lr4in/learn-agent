"""Authenticated loopback HTTP routes; credentials stay in the Python process."""

import asyncio
import os
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from secrets import compare_digest
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api_runtime import TaskManager
from .providers import ModelProvider
from .tools import TOOL_REGISTRY


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=10000)
    auto_read: bool = False
    stream: bool = False
    max_model_calls: int = Field(default=8, gt=0, le=32)
    max_total_tokens: int | None = Field(default=None, gt=0)
    denied_tools: list[str] = Field(default_factory=list)

    @field_validator("denied_tools")
    @classmethod
    def known_tools(cls, names: list[str]) -> list[str]:
        if any(name not in TOOL_REGISTRY for name in names):
            raise ValueError("denied_tools 只能包含已注册的内置工具名称。")
        return names


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approval_id: str = Field(min_length=1)
    approved: bool


def create_app(workspace: Path, log_dir: Path, api_key: str, token: str,
               *, port: int = 8000, provider: ModelProvider | None = None,
               instance_id: str | None = None) -> FastAPI:
    if not api_key.strip() or not token.strip():
        raise ValueError("后端模型密钥与本地访问令牌不能为空。")
    manager = TaskManager(workspace, log_dir, api_key, provider)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await asyncio.to_thread(manager.close)

    app = FastAPI(title="learn-agent 本地 API", version="1", lifespan=lifespan,
                  description="创建会话、提交任务、查询快照与确认工具。模型密钥只由后端持有。")
    app.state.manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.headers.get("origin") is not None and request.headers["origin"] not in allowed_origins:
            return JSONResponse({"detail": "拒绝来自其他网页的请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if response.headers.get("Content-Type") == "application/json":
            response.headers["Content-Type"] = "application/json; charset=utf-8"
        return response

    bearer = HTTPBearer()

    def authorize(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
        if not compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "本地访问令牌无效。", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    def health():
        return {"status": "ok", "api_version": 1}

    web_dir = Path(__file__).resolve().parent.parent / "web"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def workbench():
        page = (web_dir / "index.html").read_text(encoding="utf-8")
        # The same-origin page receives only this process's local access token, never the model key.
        page = page.replace('name="api-token" content=""', f'name="api-token" content="{escape(token, quote=True)}"')
        page = page.replace('name="workspace" content=""', f'name="workspace" content="{escape(str(manager.workspace), quote=True)}"')
        return HTMLResponse(page, headers={"Content-Security-Policy": "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    protected = [Depends(authorize)]
    identity = {"instance_id": instance_id or uuid4().hex, "pid": os.getpid(),
                "workspace": str(manager.workspace), "api_version": 1}

    @app.get("/instance", dependencies=protected)
    def instance():
        return identity

    @app.post("/sessions", status_code=201, dependencies=protected)
    def create_session():
        return manager.create_session()

    @app.get("/sessions", dependencies=protected)
    def list_sessions():
        return manager.list_sessions()

    @app.get("/sessions/{session_id}", dependencies=protected)
    def read_session(session_id: str):
        return manager.read_session(session_id)

    @app.post("/sessions/{session_id}/tasks", status_code=202, dependencies=protected)
    def submit_task(session_id: str, body: TaskRequest):
        return manager.submit(session_id, body.model_dump())

    @app.get("/tasks/{task_id}", dependencies=protected)
    def read_task(task_id: str):
        return manager.snapshot(task_id)

    @app.get("/tasks", dependencies=protected)
    def list_tasks():
        return manager.list_tasks()

    def event_subscription(task_id: str, after: str | None = None,
                           last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
        if after is not None and last_event_id is not None and after != last_event_id:
            raise HTTPException(422, "after 与 Last-Event-ID 不一致。")
        return manager.open_events(task_id, last_event_id if last_event_id is not None else after)

    @app.get("/tasks/{task_id}/events", dependencies=protected, response_class=EventSourceResponse)
    async def task_events(subscription=Depends(event_subscription)):
        task_id, sequence, snapshot = subscription
        if snapshot is not None:
            frame = {"id": snapshot["last_event_id"], "event": "snapshot", "task_id": task_id,
                     "session_id": snapshot["session_id"], "data": snapshot}
            yield ServerSentEvent(id=frame["id"], event=frame["event"], data=frame)
        while True:
            batch, done = await asyncio.to_thread(manager.wait_events, task_id, sequence)
            for frame in batch:
                yield ServerSentEvent(id=frame["id"], event=frame["event"], data=frame)
                sequence += 1
            if done:
                return

    @app.post("/tasks/{task_id}/approvals", status_code=202, dependencies=protected)
    def approve_task(task_id: str, body: ApprovalRequest):
        return manager.approve(task_id, body.approval_id, body.approved)

    @app.post("/tasks/{task_id}/cancel", status_code=202, dependencies=protected)
    def cancel_task(task_id: str):
        return manager.cancel(task_id)

    return app
