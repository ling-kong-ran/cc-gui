"""
CCB Bridge - 管理 ccb.exe 子进程的生命周期和流式通信

策略：每条消息启动一个 ccb -p 子进程，通过 --resume 实现多轮对话。
ccb 的 stream-json input 模式不可靠，改为 text stdin + stream-json output。
"""
import asyncio
import json
import os
import sys
import time
import uuid
from typing import Optional, Callable, Any
from pathlib import Path

import shutil

def _detect_available_clis() -> list[dict]:
    """检测所有可用的 CLI，返回列表 [{name, path, source}]"""
    available = []
    # 1. 同级目录的 ccb.exe（与 start.bat 同层）
    local_ccb = Path(__file__).parent / "ccb.exe"
    if local_ccb.exists():
        available.append({
            "name": "ccb (本地)",
            "path": str(local_ccb),
            "source": "local",
        })
    # 2. start.bat 所在目录的上一层（即仓库根目录的父目录）
    parent_ccb = Path(__file__).parent.parent / "ccb.exe"
    if parent_ccb.exists() and parent_ccb != local_ccb:
        available.append({
            "name": "ccb (上级目录)",
            "path": str(parent_ccb),
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
DEFAULT_CWD = str(Path(__file__).parent.resolve())  # 项目根目录
_slash_command_cache: dict[str, dict] = {}

def get_current_cli() -> str:
    return _current_cli

def set_current_cli(path: str):
    global _current_cli
    _current_cli = path

def refresh_clis() -> list[dict]:
    """重新检测可用 CLI；若当前选中的 CLI 已失效则切换到第一个可用项。"""
    global _current_cli
    available = _detect_available_clis()
    paths = [c["path"] for c in available]
    if available and _current_cli not in paths:
        _current_cli = available[0]["path"]
    return available


async def discover_slash_commands(
    model: str,
    cwd: Optional[str] = None,
    skip_permissions: bool = True,
    cache_ttl: int = 300,
) -> dict:
    """Read dynamic slash command metadata from the CLI init event."""
    run_cwd = cwd or DEFAULT_CWD
    cli = get_current_cli()
    cache_key = json.dumps(
        {"cli": cli, "model": model, "cwd": run_cwd, "skip": skip_permissions},
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = _slash_command_cache.get(cache_key)
    now = time.time()
    if cached and now - cached.get("time", 0) < cache_ttl:
        return dict(cached["data"])

    cmd = [
        cli,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
    ]
    if skip_permissions:
        cmd += ["--dangerously-skip-permissions"]

    proc = None
    stderr_lines: list[str] = []
    stderr_task = None
    probe_session_id = None  # 本次探测启动产生的会话 id，结束后删除其残留 jsonl

    async def read_stderr(process: asyncio.subprocess.Process):
        if not process.stderr:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                stderr_lines.append(line.decode("utf-8", errors="replace").strip())
        except asyncio.CancelledError:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=run_cwd,
            limit=1024 * 1024 * 5,
        )
        stderr_task = asyncio.create_task(read_stderr(proc))

        if proc.stdin:
            proc.stdin.write(b"/help\n")
            await proc.stdin.drain()
            proc.stdin.close()

        init_event = None
        deadline = time.time() + 10
        while time.time() < deadline and proc.stdout:
            timeout = max(0.1, deadline - time.time())
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init_event = event
                probe_session_id = event.get("session_id") or event.get("sessionId") or None
                break

        data = {
            "slash_commands": [],
            "skills": [],
            "agents": [],
            "model": model,
            "cli": cli,
            "error": None,
        }
        if init_event:
            data.update({
                "slash_commands": init_event.get("slash_commands") or [],
                "skills": init_event.get("skills") or [],
                "agents": init_event.get("agents") or [],
                "model": init_event.get("model") or model,
                "version": init_event.get("claude_code_version") or "",
            })
            _slash_command_cache[cache_key] = {"time": now, "data": dict(data)}
            return data

        data["error"] = "\n".join(line for line in stderr_lines if line).strip() or "CLI 未返回初始化事件"
        return data
    except Exception as exc:
        return {
            "slash_commands": [],
            "skills": [],
            "agents": [],
            "model": model,
            "cli": cli,
            "error": str(exc),
        }
    finally:
        if proc:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        if stderr_task:
            stderr_task.cancel()
        # 删除本次探测启动留下的空会话 jsonl，避免每次刷新都在历史里堆积空"新会话"
        if probe_session_id:
            try:
                from session_store import _delete_session_files
                _delete_session_files(probe_session_id, run_cwd)
            except Exception:
                pass


class CCBSession:
    """管理一个逻辑会话（可能对应多个 ccb 子进程）"""

    def __init__(self):
        self.session_id: Optional[str] = None
        self.model: str = "claude-sonnet-4-6"
        self.cwd: Optional[str] = None
        self.is_running = False
        self.skip_permissions: bool = True  # 默认跳过权限
        self.remote_target: Optional[dict] = None  # 绑定的远程目标
        self.allow_mutate: bool = False  # 是否允许读写模式（变更类工具）
        self.cli: Optional[str] = None  # 本会话使用的 CLI，None 时回退到全局当前 CLI
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._on_event: Optional[Callable[[dict], Any]] = None
        self._read_task: Optional[asyncio.Task] = None
        self._mcp_config_path: Optional[Path] = None

    async def start(
        self,
        model: str = "claude-sonnet-4-6",
        cwd: Optional[str] = None,
        resume_id: Optional[str] = None,
        on_event: Optional[Callable[[dict], Any]] = None,
        skip_permissions: bool = True,
        remote_target: Optional[dict] = None,
        allow_mutate: bool = False,
        cli: Optional[str] = None,
    ):
        """初始化会话参数"""
        self.model = model
        self.cwd = cwd or DEFAULT_CWD
        self.session_id = resume_id
        self._on_event = on_event
        self.skip_permissions = skip_permissions
        self.remote_target = remote_target or None
        self.allow_mutate = bool(allow_mutate)
        self.cli = cli or None
        self.is_running = True

    def _build_remote_mcp(self) -> tuple[Optional[str], Optional[str]]:
        """为绑定了远程目标的会话写出 MCP 配置文件，返回 (配置文件路径, 追加系统提示)。"""
        if not self.remote_target:
            return None, None

        bridge_path = str(Path(__file__).parent / "remote_bridge.py")
        audit_path = str((Path.home() / ".ccb" / "remote_audit.log"))
        config = {
            "mcpServers": {
                "remote": {
                    "command": sys.executable,
                    "args": [bridge_path],
                    "env": {
                        "CCB_REMOTE_TARGET": json.dumps(self.remote_target, ensure_ascii=False),
                        "CCB_REMOTE_ALLOW_MUTATE": "1" if self.allow_mutate else "0",
                        "CCB_REMOTE_AUDIT": audit_path,
                    },
                }
            }
        }
        mcp_dir = Path.home() / ".ccb" / "mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        if not self._mcp_config_path:
            self._mcp_config_path = mcp_dir / f"remote_{uuid.uuid4().hex[:8]}.json"
        self._mcp_config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        # 配置内含目标密码/密钥环境变量，收紧文件权限
        try:
            os.chmod(self._mcp_config_path, 0o600)
        except OSError:
            pass

        t = self.remote_target
        label = t.get("name") or t.get("host", "")
        mutate_line = (
            "用户已开启「允许远程写入」，必要时可用 mcp__remote__remote_exec 执行变更类命令，执行前先说明将要做的改动。"
            if self.allow_mutate else
            "当前为只读模式，只能查看；如需变更系统请提示用户在 GUI 开启「允许远程写入」。"
        )
        prompt = (
            f"本会话用于排查一台远程 Linux 机器（{label}，{t.get('user','')}@{t.get('host','')}），该机器未安装 Claude。"
            "请优先使用名称以 mcp__remote__ 开头的远程工具（remote_run/remote_tail/remote_read_file/"
            "remote_grep/remote_list/remote_sysinfo）在目标机上查看日志、跑只读命令，不要用本地 Bash 操作目标机。"
            + mutate_line
        )
        return str(self._mcp_config_path), prompt

    async def send_message(self, content: str):
        """发送一条消息：启动 ccb 子进程处理"""
        if not self.is_running:
            return

        # 如果上一个进程还在跑，先终止
        await self._kill_proc()

        cmd = [
            self.cli or get_current_cli(),
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", self.model,
        ]

        mcp_config, remote_prompt = self._build_remote_mcp()
        if mcp_config:
            cmd += ["--mcp-config", mcp_config]
        if remote_prompt:
            cmd += ["--append-system-prompt", remote_prompt]

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
        # 清理本会话的临时 MCP 配置文件
        if self._mcp_config_path:
            try:
                self._mcp_config_path.unlink()
            except OSError:
                pass
            self._mcp_config_path = None

    async def interrupt(self):
        """仅终止当前回复生成，保留逻辑会话以便继续补充。"""
        if not self.is_running:
            return
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

    def create_session(self, client_id: Optional[str] = None) -> tuple[str, CCBSession]:
        if not client_id:
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
