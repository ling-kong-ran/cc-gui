"""
CCB Bridge - 管理 ccb.exe 子进程的生命周期和流式通信

策略：每条消息启动一个 ccb -p 子进程，通过 --resume 实现多轮对话。
ccb 的 stream-json input 模式不可靠，改为 text stdin + stream-json output。
"""
import asyncio
import json
import os
from typing import Optional, Callable, Any
from pathlib import Path

import shutil

def _detect_available_clis() -> list[dict]:
    """检测所有可用的 CLI，返回列表 [{name, path, source}]"""
    available = []
    # 1. 同级目录的 ccb.exe
    local_ccb = Path(__file__).parent.parent / "ccb.exe"
    if local_ccb.exists():
        available.append({
            "name": "ccb (本地)",
            "path": str(local_ccb),
            "source": "local",
        })
    # 2. PATH 中的 ccb
    found = shutil.which("ccb")
    if found:
        available.append({
            "name": "ccb (PATH)",
            "path": found,
            "source": "path",
        })
    # 3. PATH 中的 claude
    found = shutil.which("claude")
    if found:
        available.append({
            "name": "claude (PATH)",
            "path": found,
            "source": "path",
        })
    return available

def get_available_clis() -> list[dict]:
    """供 API 调用，返回可用 CLI 列表"""
    return _detect_available_clis()

# 当前选中的 CLI（默认取第一个可用的）
_available = _detect_available_clis()
_current_cli = _available[0]["path"] if _available else "claude"
DEFAULT_CWD = str(Path(__file__).parent.parent.resolve())  # 项目根目录

def get_current_cli() -> str:
    return _current_cli

def set_current_cli(path: str):
    global _current_cli
    _current_cli = path


class CCBSession:
    """管理一个逻辑会话（可能对应多个 ccb 子进程）"""

    def __init__(self):
        self.session_id: Optional[str] = None
        self.model: str = "claude-sonnet-4-6"
        self.cwd: Optional[str] = None
        self.is_running = False
        self.skip_permissions: bool = True  # 默认跳过权限
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._on_event: Optional[Callable[[dict], Any]] = None
        self._read_task: Optional[asyncio.Task] = None

    async def start(
        self,
        model: str = "claude-sonnet-4-6",
        cwd: Optional[str] = None,
        resume_id: Optional[str] = None,
        on_event: Optional[Callable[[dict], Any]] = None,
        skip_permissions: bool = True,
    ):
        """初始化会话参数"""
        self.model = model
        self.cwd = cwd or DEFAULT_CWD
        self.session_id = resume_id
        self._on_event = on_event
        self.skip_permissions = skip_permissions
        self.is_running = True

    async def send_message(self, content: str):
        """发送一条消息：启动 ccb 子进程处理"""
        if not self.is_running:
            return

        # 如果上一个进程还在跑，先终止
        await self._kill_proc()

        cmd = [
            get_current_cli(),
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self.model,
        ]

        if self.skip_permissions:
            cmd += ["--dangerously-skip-permissions"]

        if self.session_id:
            cmd += ["--resume", self.session_id]

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            limit=1024 * 1024 * 20,  # 20MB buffer for large image responses
        )

        # 写入消息并关闭 stdin
        self._proc.stdin.write(content.encode("utf-8"))
        await self._proc.stdin.drain()
        self._proc.stdin.close()

        # 启动输出读取
        self._read_task = asyncio.create_task(self._stream_output())

    async def stop(self):
        """终止当前进程"""
        self.is_running = False
        await self._kill_proc()

    async def _kill_proc(self):
        """终止子进程"""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None

    async def _stream_output(self):
        """读取 ccb 子进程的 stdout + stderr，逐行解析并推送事件"""
        if not self._proc or not self._proc.stdout:
            return

        # 同时读取 stderr 用于错误诊断
        async def _read_stderr():
            stderr_lines = []
            if self._proc and self._proc.stderr:
                try:
                    while True:
                        line = await self._proc.stderr.readline()
                        if not line:
                            break
                        stderr_lines.append(line.decode("utf-8", errors="replace").strip())
                except (asyncio.CancelledError, Exception):
                    pass
            return stderr_lines

        stderr_task = asyncio.create_task(_read_stderr())

        try:
            got_any_event = False
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    # 非 JSON 行可能是 ccb 的文本错误输出（stdout guard 可能漏过来）
                    continue

                got_any_event = True

                # 从 init 或 result 事件中捕获 session_id
                sid = event.get("session_id")
                if sid and sid != self.session_id:
                    self.session_id = sid
                    # 通知上层 session_id 已捕获
                    if self._on_event:
                        capture_evt = {"type": "session_id_captured", "session_id": sid}
                        if asyncio.iscoroutinefunction(self._on_event):
                            await self._on_event(capture_evt)
                        else:
                            self._on_event(capture_evt)

                # 推送事件给前端
                if self._on_event:
                    if asyncio.iscoroutinefunction(self._on_event):
                        await self._on_event(event)
                    else:
                        self._on_event(event)

            # 等待进程结束，获取退出码
            exit_code = None
            if self._proc:
                try:
                    exit_code = await asyncio.wait_for(self._proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass

            # 如果没有收到任何事件，检查 stderr
            if not got_any_event and self._on_event:
                stderr_lines = await stderr_task
                stderr_text = "\n".join(stderr_lines).strip()
                if stderr_text:
                    err = {"type": "error", "message": stderr_text}
                else:
                    err = {"type": "error", "message": "ccb 进程未返回任何输出，请检查配置和 API Key"}
                if asyncio.iscoroutinefunction(self._on_event):
                    await self._on_event(err)
                else:
                    self._on_event(err)
            else:
                stderr_task.cancel()

            # 始终发送 process_ended 事件，确保前端退出 responding 状态
            if self._on_event:
                end_evt = {"type": "process_ended", "exit_code": exit_code}
                if asyncio.iscoroutinefunction(self._on_event):
                    await self._on_event(end_evt)
                else:
                    self._on_event(end_evt)

        except asyncio.CancelledError:
            stderr_task.cancel()
        except asyncio.TimeoutError:
            stderr_task.cancel()
        except Exception as e:
            stderr_task.cancel()
            if self._on_event:
                err = {"type": "error", "message": str(e)}
                if asyncio.iscoroutinefunction(self._on_event):
                    await self._on_event(err)
                else:
                    self._on_event(err)


class SessionManager:
    """管理多个 CCB 会话"""

    def __init__(self):
        self.sessions: dict[str, CCBSession] = {}
        self._counter = 0

    def create_session(self) -> tuple[str, CCBSession]:
        self._counter += 1
        client_id = f"session_{self._counter}"
        session = CCBSession()
        self.sessions[client_id] = session
        return client_id, session

    def get_session(self, client_id: str) -> Optional[CCBSession]:
        return self.sessions.get(client_id)

    async def remove_session(self, client_id: str):
        session = self.sessions.pop(client_id, None)
        if session:
            await session.stop()

    async def cleanup_all(self):
        for client_id in list(self.sessions.keys()):
            await self.remove_session(client_id)
