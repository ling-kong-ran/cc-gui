"""
CC Bridge Server - 纯 Python 标准库实现
HTTP 静态文件 + REST API + SSE (Server-Sent Events) 通信
使用 SSE 替代 WebSocket 避免 Windows asyncio 兼容性问题
"""
import asyncio
import json
import os
import sys
import socket
import uuid
import ipaddress
import base64
import shlex
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))

from ccb_bridge import SessionManager, discover_slash_commands, get_available_clis, get_current_cli, set_current_cli, refresh_clis
from config_manager import (
    get_settings,
    save_settings,
    get_env_config,
    update_env_config,
    get_gui_settings,
    update_gui_settings,
    get_env_profiles,
    save_env_profile,
    delete_env_profile,
    list_skills,
    list_agents,
    get_available_models,
)
from session_store import list_sessions, save_session, add_session_usage, delete_session, load_session_history, rename_session
import remote_manager

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_CWD = str(Path(__file__).parent.resolve())  # 项目根目录作为默认 CWD
HOST = "0.0.0.0"  # 监听所有网卡，允许局域网设备访问
BROWSER_HOST = "127.0.0.1"
DEFAULT_PORT = 17878
MAX_REQUEST_BODY_BYTES = 100 * 1024 * 1024


def get_lan_ips() -> list[str]:
    """获取本机局域网 IPv4 地址，用于提示手机访问地址。"""
    ips = []
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = item[4][0]
            if ip.startswith("127.") or ip in ips:
                continue
            ips.append(ip)
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if not ip.startswith("127.") and ip not in ips:
                ips.insert(0, ip)
    except OSError:
        pass

    return ips


def is_localhost_ip(ip: str) -> bool:
    """判断请求来源是否为本机地址。"""
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def lan_access_enabled() -> bool:
    """读取是否允许非 localhost 访问。"""
    return bool(get_gui_settings().get("lan_access_enabled", True))


def get_client_ip(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return ""


def get_access_context(writer: asyncio.StreamWriter) -> dict:
    client_ip = get_client_ip(writer)
    return {
        "client_ip": client_ip,
        "is_localhost": is_localhost_ip(client_ip),
        "lan_access_enabled": lan_access_enabled(),
    }


def is_request_allowed(writer: asyncio.StreamWriter) -> bool:
    context = get_access_context(writer)
    return context["is_localhost"] or context["lan_access_enabled"]


def is_client_allowed(client_id: str) -> bool:
    ip = client_ips.get(client_id, "")
    return is_localhost_ip(ip) or lan_access_enabled()


def bind_client_ip(client_id: str, writer: asyncio.StreamWriter) -> bool:
    """把 client_id 绑定到首次连接来源，避免复用 client_id 绕过本地 CLI 权限。"""
    ip = get_client_ip(writer)
    if not client_id or not ip:
        return False
    bound_ip = client_ips.get(client_id)
    if bound_ip and bound_ip != ip:
        return False
    client_ips[client_id] = ip
    return True


def is_cli_access_allowed(client_id: str, writer: asyncio.StreamWriter) -> bool:
    if not bind_client_ip(client_id, writer):
        return False
    return is_client_allowed(client_id)


async def reject_client_access(client_id: str, writer: asyncio.StreamWriter):
    """拒绝已越权的客户端，并确保无法继续驱动本地 CLI。"""
    session = session_manager.get_session(client_id)
    if session:
        await session.stop()
        await session_manager.remove_session(client_id)
    client_meta.pop(client_id, None)
    client_last_msg.pop(client_id, None)
    client_session_ids.pop(client_id, None)
    client_ips.pop(client_id, None)
    await send_response(writer, 403, "application/json", b'{"ok":false,"error":"LAN access disabled"}')


async def revoke_lan_clients():
    """关闭所有非 localhost 客户端的本地 CLI 会话。"""
    for client_id, ip in list(client_ips.items()):
        if is_localhost_ip(ip):
            continue
        session = session_manager.get_session(client_id)
        if session:
            await session.stop()
            await session_manager.remove_session(client_id)
        client_meta.pop(client_id, None)
        client_last_msg.pop(client_id, None)
        client_session_ids.pop(client_id, None)
        await push_event(client_id, "error", {"message": "LAN access disabled"})
        await push_event(client_id, "session_stopped", {})

session_manager = SessionManager()

# SSE 客户端连接池: client_id -> asyncio.Queue
sse_clients: dict[str, asyncio.Queue] = {}

# 每个 client 的最后一条用户消息（用于会话标题）
client_last_msg: dict[str, str] = {}

# 每个 client 关联的 ccb session id
client_session_ids: dict[str, str] = {}

# 每个 client 的会话参数（model, cwd）
client_meta: dict[str, dict] = {}

# 每个 client 的来源 IP，用于局域网访问开关生效后收紧已有连接
client_ips: dict[str, str] = {}


def extract_result_tokens(event: dict) -> dict:
    """从 result 事件中提取本轮 token 用量。"""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}

    def read_int(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is None:
                value = event.get(key)
            try:
                number = int(value or 0)
            except (TypeError, ValueError):
                number = 0
            if number > 0:
                return number
        return 0

    return {
        "input": read_int("input_tokens"),
        "output": read_int("output_tokens"),
        "cache_creation": read_int("cache_creation_input_tokens", "cache_creation_tokens"),
        "cache_read": read_int("cache_read_input_tokens", "cache_read_tokens"),
    }


def persist_result_usage(client_id: str, event: dict) -> dict:
    """把单轮 result 费用和 token 用量累加到当前会话，并把累计值附加给前端。"""
    try:
        turn_cost = float(event.get("total_cost_usd") or 0)
    except (TypeError, ValueError):
        turn_cost = 0
    turn_tokens = extract_result_tokens(event)

    sid = event.get("session_id") or client_session_ids.get(client_id)
    if not sid:
        return event

    updated = dict(event)
    if turn_cost > 0 or any(turn_tokens.values()):
        totals = add_session_usage(sid, turn_cost, turn_tokens)
        total_cost = float(totals.get("total_cost_usd") or 0)
        total_tokens = totals.get("total_tokens") or {}
        if total_cost > 0:
            updated["session_total_cost_usd"] = total_cost
        if any(total_tokens.values()):
            updated["session_total_tokens"] = total_tokens
        if any(turn_tokens.values()):
            updated["turn_tokens"] = turn_tokens

    return updated


def extract_tool_result_ids(event: dict) -> list[str]:
    """从 user 事件中提取 tool_result 块的 tool_use_id（用于判断 subagent/工具是否结束）。"""
    msg = event.get("message") or {}
    content = msg.get("content")
    ids = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                ids.append(block["tool_use_id"])
    return ids


def get_default_model() -> str:
    models = get_available_models()
    return models[0] if models else "claude-sonnet-4-6"


def format_slash_commands(discovered: dict) -> dict:
    """Build frontend command items from CLI-discovered slash command names."""
    local_skills = {item.get("name"): item for item in list_skills() if item.get("name")}
    cli_skills = set(discovered.get("skills") or [])
    commands = []
    seen = set()

    for raw_name in discovered.get("slash_commands") or []:
        if not raw_name:
            continue
        name = str(raw_name).strip()
        if not name:
            continue
        display_name = name if name.startswith("/") else f"/{name}"
        if display_name in seen:
            continue
        seen.add(display_name)

        skill_name = name[1:] if name.startswith("/") else name
        skill = local_skills.get(skill_name)
        source = "skill" if skill_name in cli_skills or skill else "cli"
        description = ""
        if skill:
            description = skill.get("description") or "运行该技能"
        elif source == "skill":
            description = "运行该技能"
        else:
            description = "CLI 动态命令"

        commands.append({
            "name": display_name,
            "description": description,
            "source": source,
        })

    return {
        "commands": sorted(commands, key=lambda item: item["name"].lower()),
        "model": discovered.get("model") or "",
        "version": discovered.get("version") or "",
        "error": discovered.get("error"),
    }

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
}


# ─── 文件+目录浏览 ──────────────────────────────────────────
def browse_files(path: str) -> dict:
    """列出指定目录下的子目录和文件（用于附件选择器）"""
    import string

    if not path or path == "/":
        if sys.platform == "win32":
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:/"
                if os.path.isdir(drive):
                    drives.append({"name": f"{letter}:/", "path": drive, "type": "drive"})
            return {"current": "/", "parent": None, "items": drives}
        else:
            path = "/"

    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return {"current": path, "parent": None, "items": [], "error": "路径不存在"}

    parent = os.path.dirname(path)
    if parent == path:
        parent = "/"

    items = []
    try:
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if entry.startswith('.'):
                continue
            if os.path.isdir(full):
                if entry in ('node_modules', '__pycache__', '.git', 'venv', '.venv'):
                    continue
                items.append({"name": entry, "path": full.replace("\\", "/"), "type": "dir"})
            elif os.path.isfile(full):
                items.append({"name": entry, "path": full.replace("\\", "/"), "type": "file"})
    except PermissionError:
        return {"current": path, "parent": parent, "items": [], "error": "无权限访问"}

    return {
        "current": path.replace("\\", "/"),
        "parent": parent.replace("\\", "/") if parent != "/" else "/",
        "items": items,
    }


def search_files(path: str, query: str, max_results: int = 200) -> dict:
    """在指定目录及其子目录中搜索文件（用于附件选择器搜索）。"""
    query = (query or "").strip().lower()
    if not query:
        return browse_files(path)

    if not path or path == "/":
        if sys.platform == "win32":
            return {"current": "/", "parent": None, "items": [], "error": "请先选择一个具体目录后再搜索"}
        path = "/"

    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return {"current": path, "parent": None, "items": [], "error": "路径不存在"}

    excluded_dirs = {'node_modules', '__pycache__', '.git', 'venv', '.venv'}
    items = []

    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in excluded_dirs)

            for dirname in list(dirs):
                full = os.path.join(root, dirname)
                rel = os.path.relpath(full, path).replace("\\", "/")
                if query in dirname.lower() or query in rel.lower():
                    items.append({
                        "name": dirname,
                        "display": rel,
                        "path": full.replace("\\", "/"),
                        "type": "dir",
                    })
                    if len(items) >= max_results:
                        return {
                            "current": path.replace("\\", "/"),
                            "parent": os.path.dirname(path).replace("\\", "/"),
                            "items": items,
                            "truncated": True,
                        }

            for filename in sorted(files):
                if filename.startswith('.'):
                    continue
                full = os.path.join(root, filename)
                rel = os.path.relpath(full, path).replace("\\", "/")
                if query not in filename.lower() and query not in rel.lower():
                    continue
                items.append({
                    "name": filename,
                    "display": rel,
                    "path": full.replace("\\", "/"),
                    "type": "file",
                })
                if len(items) >= max_results:
                    return {
                        "current": path.replace("\\", "/"),
                        "parent": os.path.dirname(path).replace("\\", "/"),
                        "items": items,
                        "truncated": True,
                    }
    except PermissionError:
        return {"current": path.replace("\\", "/"), "parent": None, "items": [], "error": "无权限访问"}

    items.sort(key=lambda item: (item["type"] != "dir", item.get("display", item["name"]).lower()))
    return {
        "current": path.replace("\\", "/"),
        "parent": os.path.dirname(path).replace("\\", "/"),
        "items": items,
        "truncated": False,
    }


# ─── 目录浏览 ──────────────────────────────────────────────
def browse_directory(path: str) -> dict:
    import string

    if not path or path == "/":
        drives = []
        if sys.platform == "win32":
            for letter in string.ascii_uppercase:
                drive = f"{letter}:/"
                if os.path.isdir(drive):
                    drives.append({"name": f"{letter}:/", "path": drive, "type": "drive"})
            return {"current": "/", "parent": None, "items": drives}
        else:
            path = "/"

    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return {"current": path, "parent": None, "items": [], "error": "路径不存在"}

    parent = os.path.dirname(path)
    if parent == path:
        parent = "/"

    items = []
    try:
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                if entry.startswith('.') and entry not in ('.claude',):
                    continue
                if entry in ('node_modules', '__pycache__', '.git', 'venv', '.venv'):
                    continue
                items.append({
                    "name": entry,
                    "path": full.replace("\\", "/"),
                    "type": "dir",
                })
    except PermissionError:
        return {"current": path, "parent": parent, "items": [], "error": "无权限访问"}

    return {
        "current": path.replace("\\", "/"),
        "parent": parent.replace("\\", "/") if parent != "/" else "/",
        "items": items,
    }


def remote_upload_dir(cwd: str = "") -> Path:
    base = Path(cwd) if cwd and os.path.isdir(cwd) else UPLOAD_DIR_FALLBACK.parent
    upload_dir = base / ".gui-uploads" / "remote"
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def shell_quote(value: str) -> str:
    return shlex.quote(str(value or ""))


def remote_ls(target_id: str, path: str) -> dict:
    target = remote_manager.get_target(target_id or "")
    if not target:
        return {"ok": False, "error": "target_not_found"}
    remote_path = path or "."
    # 纯 shell 实现，不依赖远程 Python
    # 用 stat 逐个输出 type|size|name，兼容性好于 find -printf
    qpath = shell_quote(remote_path)
    command = (
        f"_D=$(cd {qpath} 2>/dev/null && pwd) || exit 1; "
        f"echo \"DIR:$_D\"; "
        f"for f in \"$_D\"/*; do "
        f"[ -e \"$f\" ] || continue; "
        f"_N=$(basename \"$f\"); "
        f"if [ -d \"$f\" ]; then _T=d; else _T=f; fi; "
        f"_S=$(stat -c%s \"$f\" 2>/dev/null || echo 0); "
        f"echo \"$_T|$_S|$_N\"; "
        f"done"
    )
    res = remote_manager.run_remote_command(target, command, timeout=30)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or res.get("stderr") or "remote_failed"}
    stdout = (res.get("stdout") or "").strip()
    lines = stdout.splitlines()
    if not lines:
        return {"ok": False, "error": "empty_response"}
    # 解析当前目录
    current = remote_path
    if lines[0].startswith("DIR:"):
        current = lines[0][4:]
        lines = lines[1:]
    parent = os.path.dirname(current) or "/"
    items = []
    for line in lines:
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        ftype, size_str, name = parts
        if not name or name.startswith("."):
            continue
        typ = "dir" if ftype == "d" else "file"
        try:
            size = int(size_str)
        except ValueError:
            size = 0
        full = current.rstrip("/") + "/" + name
        items.append({"name": name, "path": full, "type": typ, "size": size})
    items.sort(key=lambda x: x["name"])
    return {"ok": True, "current": current, "parent": parent, "items": items}


def remote_cache_file(target_id: str, path: str, cwd: str = "") -> dict:
    target = remote_manager.get_target(target_id or "")
    if not target:
        return {"ok": False, "error": "target_not_found"}
    remote_path = path or ""
    if not remote_path:
        return {"ok": False, "error": "missing_path"}
    name = Path(remote_path).name or "remote-file"
    local_name = f"{uuid.uuid4().hex[:8]}_{name}"
    local_path = remote_upload_dir(cwd) / local_name
    command = "base64 " + shell_quote(remote_path)
    res = remote_manager.run_remote_command(target, command, timeout=120)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or res.get("stderr") or "remote_failed"}
    try:
        data = base64.b64decode((res.get("stdout") or "").encode("ascii"), validate=False)
    except (ValueError, UnicodeEncodeError) as exc:
        return {"ok": False, "error": f"decode_failed: {exc}"}
    local_path.write_bytes(data)
    return {
        "ok": True,
        "name": name,
        "path": str(local_path.resolve()).replace("\\", "/"),
        "source": "remote",
        "original_path": remote_path,
        "remote_target_name": target.get("name") or target.get("host") or target_id,
        "size": len(data),
    }


# ─── CLI 安装 ─────────────────────────────────────────────
INSTALL_CLI_COMMAND = "npm install -g @anthropic-ai/claude-code"
_install_lock = asyncio.Lock()


async def install_cli() -> dict:
    """通过 npm 全局安装 Claude Code CLI，返回安装结果。"""
    import shutil as _shutil

    npm = _shutil.which("npm")
    if not npm:
        return {"ok": False, "error": "npm_not_found"}

    if _install_lock.locked():
        return {"ok": False, "error": "install_in_progress"}

    async with _install_lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                npm, "install", "-g", "@anthropic-ai/claude-code",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024 * 5,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            output = (stdout or b"").decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                return {"ok": False, "error": "install_failed", "output": output[-4000:]}
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"ok": False, "error": "install_timeout"}
        except Exception as exc:
            return {"ok": False, "error": "install_failed", "output": str(exc)}

    available = refresh_clis()
    return {
        "ok": bool(available),
        "available": available,
        "current": get_current_cli() if available else "",
        "output": output[-4000:],
        "error": None if available else "cli_not_detected_after_install",
    }


# ─── 自动更新 ─────────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent
_update_lock = asyncio.Lock()


async def _run_git(*args, timeout: int = 30) -> tuple[int, str]:
    """在仓库目录运行 git 子命令，返回 (returncode, 合并输出)。git 不存在时 returncode=-1。"""
    import shutil as _shutil

    git = _shutil.which("git")
    if not git:
        return -1, "git_not_found"
    try:
        proc = await asyncio.create_subprocess_exec(
            git, *args,
            cwd=str(REPO_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024 * 5,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        return proc.returncode, output
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, UnboundLocalError):
            pass
        return -2, "timeout"
    except Exception as exc:
        return -3, str(exc)


async def check_update() -> dict:
    """检查远端 origin/master 是否有比本地 HEAD 更新的提交。"""
    # 必须先确认是 git 仓库
    code, _ = await _run_git("rev-parse", "--is-inside-work-tree", timeout=10)
    if code != 0:
        return {"ok": False, "error": "git_unavailable"}

    code, _ = await _run_git("fetch", "--quiet", "origin", "master", timeout=30)
    if code != 0:
        return {"ok": False, "error": "fetch_failed"}

    code_l, local = await _run_git("rev-parse", "HEAD", timeout=10)
    code_r, remote = await _run_git("rev-parse", "origin/master", timeout=10)
    if code_l != 0 or code_r != 0:
        return {"ok": False, "error": "rev_parse_failed"}

    has_update = bool(local) and bool(remote) and local != remote
    commits = ""
    if has_update:
        _, commits = await _run_git("log", "--oneline", "-20", "HEAD..origin/master", timeout=10)

    return {
        "ok": True,
        "has_update": has_update,
        "local": local,
        "remote": remote,
        "local_short": local[:7],
        "remote_short": remote[:7],
        "commits": commits,
        "error": None,
    }


async def apply_update() -> dict:
    """git pull --ff-only origin master 拉取更新。"""
    if _update_lock.locked():
        return {"ok": False, "error": "update_in_progress"}
    async with _update_lock:
        code, output = await _run_git("pull", "--ff-only", "origin", "master", timeout=120)
    if code != 0:
        return {"ok": False, "error": "pull_failed", "output": output[-4000:]}
    return {"ok": True, "output": output[-4000:], "error": None}


def restart_server():
    """用 os.execv 原地重启服务进程（best-effort）。"""
    try:
        os.execv(sys.executable, [sys.executable, str(REPO_DIR / "server.py")])
    except Exception:
        # 重启失败时不抛出，前端会提示手动重启
        pass


# ─── SSE 推送 ──────────────────────────────────────────────
async def push_event(client_id: str, event_type: str, data: dict):
    """向指定 SSE 客户端推送事件"""
    queue = sse_clients.get(client_id)
    if queue:
        await queue.put({"event": event_type, "data": data})


UPLOAD_DIR_FALLBACK = Path(__file__).parent / "uploads"
UPLOAD_DIR_FALLBACK.mkdir(exist_ok=True)


def is_allowed_upload_path(path: str) -> Path | None:
    """校验上传缓存路径，仅允许 fallback uploads 或任意 .gui-uploads 下的普通文件。"""
    try:
        fp = Path(path).resolve()
        fallback = UPLOAD_DIR_FALLBACK.resolve()
        is_fallback = fp == fallback or fallback in fp.parents
        is_gui_upload = any(part == ".gui-uploads" for part in fp.parts)
        if not (is_fallback or is_gui_upload):
            return None
        return fp
    except Exception:
        return None


def delete_uploaded_files(paths: list[str]) -> dict:
    """删除 GUI 上传缓存文件；只删除文件，不删除用户通过文件选择器引用的原始路径。"""
    deleted = []
    failed = []
    for path in paths or []:
        fp = is_allowed_upload_path(str(path or ""))
        if not fp:
            failed.append({"path": path, "error": "forbidden"})
            continue
        try:
            if fp.exists() and fp.is_file():
                fp.unlink()
                deleted.append(str(fp).replace("\\", "/"))
        except OSError as exc:
            failed.append({"path": path, "error": str(exc)})
    return {"ok": True, "deleted": deleted, "failed": failed}


# ─── 文件上传 ─────────────────────────────────────────────
async def handle_upload(headers: dict, body: bytes, writer: asyncio.StreamWriter):
    """处理 multipart 文件上传，保存到工作目录的 .gui-uploads/ 下"""
    content_type = headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        await send_response(writer, 400, "application/json", b'{"error":"need multipart"}')
        return

    # 提取 boundary
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[9:].strip('"')
            break

    if not boundary:
        await send_response(writer, 400, "application/json", b'{"error":"no boundary"}')
        return

    # 解析 multipart body
    boundary_bytes = f"--{boundary}".encode()
    parts = body.split(boundary_bytes)
    saved_files = []
    upload_dir = UPLOAD_DIR_FALLBACK  # 默认
    cwd_value = None

    for part in parts:
        if not part or part == b"--\r\n" or part == b"--":
            continue

        # 分离 headers 和 content
        if b"\r\n\r\n" not in part:
            continue
        header_section, file_data = part.split(b"\r\n\r\n", 1)

        # 去掉尾部 \r\n
        if file_data.endswith(b"\r\n"):
            file_data = file_data[:-2]

        # 从 Content-Disposition 提取字段名和文件名
        header_str = header_section.decode("utf-8", errors="replace")
        filename = ""
        field_name = ""
        for line in header_str.split("\r\n"):
            if "Content-Disposition" in line:
                if 'name="' in line:
                    ni = line.index('name="') + 6
                    field_name = line[ni:line.index('"', ni)]
                if "filename=" in line:
                    idx = line.index("filename=")
                    fname = line[idx + 9:].split(";")[0].strip('" ')
                    if fname:
                        filename = fname
                break

        # 如果是 cwd 字段
        if field_name == "cwd" and not filename:
            cwd_value = file_data.decode("utf-8", errors="replace").strip()
            continue

        if not file_data or not filename:
            continue

        # 确定上传目录
        if cwd_value and os.path.isdir(cwd_value):
            upload_dir = Path(cwd_value) / ".gui-uploads"
            upload_dir.mkdir(exist_ok=True)

        # 保存文件（UUID 前缀避免冲突）
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        file_path = upload_dir / safe_name
        file_path.write_bytes(file_data)
        saved_files.append(str(file_path.resolve()).replace("\\", "/"))

    # 如果 cwd 还没处理（cwd 字段在 file 后面），重新移动文件
    if cwd_value and os.path.isdir(cwd_value) and saved_files:
        target_dir = Path(cwd_value) / ".gui-uploads"
        target_dir.mkdir(exist_ok=True)
        new_files = []
        for fp in saved_files:
            src = Path(fp)
            if src.parent != target_dir:
                dst = target_dir / src.name
                dst.write_bytes(src.read_bytes())
                src.unlink()
                new_files.append(str(dst.resolve()).replace("\\", "/"))
            else:
                new_files.append(fp)
        saved_files = new_files

    resp = json.dumps({"files": saved_files}, ensure_ascii=False).encode("utf-8")
    await send_response(writer, 200, "application/json; charset=utf-8", resp)


# ─── HTTP 请求处理 ─────────────────────────────────────────
async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """处理 HTTP 请求"""
    sock = writer.get_extra_info("socket")
    if sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not request_line:
            writer.close()
            return

        request_str = request_line.decode("utf-8", errors="replace").strip()
        parts = request_str.split(" ")
        if len(parts) < 3:
            writer.close()
            return

        method, path = parts[0], parts[1]

        # 读取 headers
        headers = {}
        while True:
            line = await reader.readline()
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                break
            if ":" in line_str:
                key, value = line_str.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        # 读取 body
        body = b""
        content_length = int(headers.get("content-length", 0))
        if content_length > MAX_REQUEST_BODY_BYTES:
            await send_response(writer, 413, "application/json", b'{"error":"request too large"}')
            return
        if content_length > 0:
            body = await reader.readexactly(content_length)

        # 路由
        parsed = urlparse(path)
        route_path = parsed.path
        query = parse_qs(parsed.query)

        if not is_request_allowed(writer):
            await send_response(writer, 403, "text/plain", b"LAN access disabled")
            return

        if route_path == "/sse":
            await handle_sse(query, writer)
            return  # SSE 连接由 handle_sse 管理生命周期

        elif method == "POST" and route_path == "/api/upload":
            await handle_upload(headers, body, writer)

        elif method == "POST" and route_path == "/api/action":
            await handle_action(body, writer)

        elif method == "GET" and route_path.startswith("/api/"):
            await handle_api_get(route_path, writer, query)

        elif method == "POST" and route_path.startswith("/api/"):
            await handle_api_post(route_path, body, writer)

        else:
            await handle_static(route_path, writer)

    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        try:
            await send_response(writer, 500, "text/plain", str(e).encode())
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ─── SSE 长连接 ────────────────────────────────────────────
async def handle_sse(query: dict, writer: asyncio.StreamWriter):
    """处理 SSE 连接 - 保持长连接推送事件"""
    client_id = query.get("id", [str(uuid.uuid4())])[0]

    # 发送 SSE 头
    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream; charset=utf-8\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: keep-alive\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "\r\n"
    )
    writer.write(header.encode())
    await writer.drain()

    # 注册客户端
    queue: asyncio.Queue = asyncio.Queue()
    sse_clients[client_id] = queue
    client_ips[client_id] = get_client_ip(writer)

    # 发送初始 connected 事件
    await _sse_write(writer, "connected", {"client_id": client_id})

    try:
        while True:
            # 等待事件（带心跳）
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                await _sse_write(writer, event["event"], event["data"])
            except asyncio.TimeoutError:
                # 发送心跳保活
                writer.write(b": heartbeat\n\n")
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        sse_clients.pop(client_id, None)
        client_ips.pop(client_id, None)
        # 清理关联的 ccb session
        await session_manager.remove_session(client_id)
        try:
            writer.close()
        except Exception:
            pass


async def _sse_write(writer: asyncio.StreamWriter, event: str, data: dict):
    """写入一条 SSE 事件"""
    payload = json.dumps(data, ensure_ascii=False)
    msg = f"event: {event}\ndata: {payload}\n\n"
    writer.write(msg.encode("utf-8"))
    await writer.drain()


# ─── Action API (客户端发送消息/命令) ──────────────────────
async def handle_action(body: bytes, writer: asyncio.StreamWriter):
    """处理客户端 action (new_session, send_message, stop)"""
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        await send_response(writer, 400, "application/json", b'{"error":"invalid json"}')
        return

    client_id = data.get("client_id", "")
    action = data.get("action", "")

    if not is_cli_access_allowed(client_id, writer):
        await reject_client_access(client_id, writer)
        return

    if action == "new_session":
        model = data.get("model") or get_default_model()
        cwd = data.get("cwd")
        skip_perms = data.get("skip_permissions", True)
        remote_target = remote_manager.get_target(data.get("remote_target_id") or "")
        allow_mutate = bool(data.get("allow_remote_mutate", False))
        cli = data.get("cli") or get_current_cli()

        # 清理旧 session
        old_session = session_manager.get_session(client_id)
        if old_session:
            await old_session.stop()

        _, session = session_manager.create_session(client_id)

        # 记录元数据
        remote_target_id = (remote_target or {}).get("id", "")
        client_meta[client_id] = {"model": model, "cwd": cwd, "remote_target_id": remote_target_id, "cli": cli}
        client_last_msg.pop(client_id, None)
        client_session_ids.pop(client_id, None)

        async def on_event(event: dict):
            evt_type = event.get("type", "unknown")
            # 拦截 session_id_captured 事件，保存到 store
            if evt_type == "session_id_captured":
                sid = event.get("session_id", "")
                client_session_ids[client_id] = sid
                title = client_last_msg.get(client_id, "新会话")
                meta = client_meta.get(client_id, {})
                save_session(sid, title, meta.get("model", model), meta.get("cwd", cwd or ""),
                             remote_target_id=meta.get("remote_target_id", ""), cli=meta.get("cli", ""))
                await push_event(client_id, "session_id_captured", event)
            elif evt_type == "result":
                await push_event(client_id, evt_type, persist_result_usage(client_id, event))
            elif evt_type == "user":
                # tool_result 表示某个工具调用（含 Task subagent）已结束，只转发 ID，省去大体积内容
                ids = extract_tool_result_ids(event)
                if ids:
                    await push_event(client_id, "tool_result", {
                        "tool_use_ids": ids,
                        "parent_tool_use_id": event.get("parent_tool_use_id"),
                    })
            elif evt_type in ("assistant", "system", "error", "process_ended", "model_changed"):
                # ccb 高层事件直接按类型推送，前端有对应 listener
                await push_event(client_id, evt_type, event)
            # 其他事件（hook_started 等）忽略

        await session.start(model=model, cwd=cwd, on_event=on_event, skip_permissions=skip_perms,
                            remote_target=remote_target, allow_mutate=allow_mutate, cli=cli)
        await push_event(client_id, "session_started", {"model": model, "remote_target_id": remote_target_id, "cli": cli})
        await send_response(writer, 200, "application/json", b'{"ok":true}')

    elif action == "resume_session":
        resume_id = data.get("session_id", "")
        model = data.get("model") or get_default_model()
        cwd = data.get("cwd")
        skip_perms = data.get("skip_permissions", True)
        remote_target = remote_manager.get_target(data.get("remote_target_id") or "")
        allow_mutate = bool(data.get("allow_remote_mutate", False))
        remote_target_id = (remote_target or {}).get("id", "")
        cli = data.get("cli") or get_current_cli()

        # 清理旧 session
        old_session = session_manager.get_session(client_id)
        if old_session:
            await old_session.stop()

        _, session = session_manager.create_session(client_id)

        client_meta[client_id] = {"model": model, "cwd": cwd, "remote_target_id": remote_target_id, "cli": cli}
        client_session_ids[client_id] = resume_id

        async def on_event_resume(event: dict):
            evt_type = event.get("type", "unknown")
            if evt_type == "session_id_captured":
                sid = event.get("session_id", "")
                client_session_ids[client_id] = sid
                meta = client_meta.get(client_id, {})
                save_session(sid, "", meta.get("model", model), meta.get("cwd", cwd or ""),
                             remote_target_id=meta.get("remote_target_id", ""), cli=meta.get("cli", ""))
                await push_event(client_id, "session_id_captured", event)
            elif evt_type == "result":
                await push_event(client_id, evt_type, persist_result_usage(client_id, event))
            elif evt_type == "user":
                ids = extract_tool_result_ids(event)
                if ids:
                    await push_event(client_id, "tool_result", {
                        "tool_use_ids": ids,
                        "parent_tool_use_id": event.get("parent_tool_use_id"),
                    })
            elif evt_type in ("assistant", "system", "error", "process_ended", "model_changed"):
                await push_event(client_id, evt_type, event)
            # 其他事件忽略

        await session.start(model=model, cwd=cwd, resume_id=resume_id, on_event=on_event_resume, skip_permissions=skip_perms,
                            remote_target=remote_target, allow_mutate=allow_mutate, cli=cli)
        await push_event(client_id, "session_started", {"model": model, "resumed": True, "session_id": resume_id, "remote_target_id": remote_target_id, "cli": cli})
        await send_response(writer, 200, "application/json", b'{"ok":true}')

    elif action == "send_message":
        content = data.get("content", "")
        requested_model = data.get("model") or ""
        session = session_manager.get_session(client_id)
        if session and session.is_running and content:
            if requested_model and requested_model != session.model:
                session.model = requested_model
                meta = client_meta.setdefault(client_id, {})
                meta["model"] = requested_model
                await push_event(client_id, "model_changed", {"model": requested_model})
            # 允许会话中手动切换远程目标和读写模式，下一条消息生效
            if "remote_target_id" in data:
                remote_target = remote_manager.get_target(data.get("remote_target_id") or "")
                session.remote_target = remote_target or None
                meta = client_meta.setdefault(client_id, {})
                meta["remote_target_id"] = (remote_target or {}).get("id", "")
            if "allow_remote_mutate" in data:
                session.allow_mutate = bool(data.get("allow_remote_mutate"))
            # 允许会话中切换 CLI，下一条消息生效
            if data.get("cli"):
                session.cli = data.get("cli")
                meta = client_meta.setdefault(client_id, {})
                meta["cli"] = data.get("cli")
            # 使用最新用户消息作为会话标题
            title = content.strip()[:50]
            client_last_msg[client_id] = title
            sid = client_session_ids.get(client_id)
            if sid:
                meta = client_meta.get(client_id, {})
                save_session(sid, title, meta.get("model", ""), meta.get("cwd", ""),
                             remote_target_id=meta.get("remote_target_id", ""), cli=meta.get("cli", ""))
            await session.send_message(content)
            await send_response(writer, 200, "application/json", b'{"ok":true}')
        else:
            await push_event(client_id, "error", {"message": "Session not running"})
            await send_response(writer, 200, "application/json", b'{"ok":false,"error":"no session"}')

    elif action == "stop":
        session = session_manager.get_session(client_id)
        if session:
            await session.stop()
        await push_event(client_id, "session_stopped", {})
        await send_response(writer, 200, "application/json", b'{"ok":true}')

    elif action == "interrupt":
        session = session_manager.get_session(client_id)
        if session and session.is_running:
            await session.interrupt()
        await push_event(client_id, "generation_interrupted", {})
        await send_response(writer, 200, "application/json", b'{"ok":true}')

    else:
        await send_response(writer, 400, "application/json", b'{"error":"unknown action"}')


# ─── REST API ──────────────────────────────────────────────
async def handle_api_get(path: str, writer: asyncio.StreamWriter, query: dict = None):
    if path == "/api/settings":
        data = get_settings()
    elif path == "/api/gui-settings":
        data = get_gui_settings()
        data.update(get_access_context(writer))
        data["default_cwd"] = DEFAULT_CWD
    elif path == "/api/env":
        data = get_env_config()
    elif path == "/api/check-update":
        result = await check_update()
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/env-profiles":
        data = get_env_profiles()
    elif path == "/api/skills":
        data = list_skills()
    elif path == "/api/agents":
        data = list_agents()
    elif path == "/api/models":
        data = get_available_models()
    elif path == "/api/slash-commands":
        if not is_request_allowed(writer):
            await send_response(writer, 403, "application/json", b'{"error":"LAN access disabled"}')
            return
        query = query or {}
        model = query.get("model", [get_default_model()])[0] or get_default_model()
        cwd = query.get("cwd", [DEFAULT_CWD])[0] or DEFAULT_CWD
        discovered = await discover_slash_commands(model=model, cwd=cwd)
        data = format_slash_commands(discovered)
    elif path == "/api/clis":
        available = refresh_clis()
        data = {
            "available": available,
            "current": get_current_cli() if available else "",
            "install_command": INSTALL_CLI_COMMAND,
        }
    elif path == "/api/default-cwd":
        data = {"cwd": DEFAULT_CWD}
    elif path == "/api/remote-targets":
        data = {"targets": remote_manager.list_targets(), "password_supported": remote_manager.password_supported()}
    elif path == "/api/sessions":
        data = list_sessions()
    elif path == "/api/file":
        # 提供上传文件（图片预览）
        file_path = (query or {}).get("path", [""])[0]
        if not file_path:
            await send_response(writer, 400, "text/plain", b"missing path")
            return
        fp = is_allowed_upload_path(file_path)
        if not fp:
            await send_response(writer, 403, "text/plain", b"forbidden")
            return
        if not fp.exists():
            await send_response(writer, 404, "text/plain", b"not found")
            return
        ext = fp.suffix.lower()
        ct = MIME_TYPES.get(ext, "application/octet-stream")
        await send_response(writer, 200, ct, fp.read_bytes())
        return
    else:
        await send_response(writer, 404, "application/json", b'{"error":"not found"}')
        return

    resp_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    await send_response(writer, 200, "application/json; charset=utf-8", resp_body)


async def handle_api_post(path: str, body: bytes, writer: asyncio.StreamWriter):
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        await send_response(writer, 400, "application/json", b'{"error":"invalid json"}')
        return

    if path == "/api/settings":
        save_settings(data)
    elif path == "/api/gui-settings":
        if "lan_access_enabled" in data and not is_localhost_ip(get_client_ip(writer)):
            await send_response(writer, 403, "application/json", b'{"error":"localhost only"}')
            return
        result = update_gui_settings(data)
        if data.get("lan_access_enabled") is False:
            await revoke_lan_clients()
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/env":
        update_env_config(data)
    elif path == "/api/env-profiles":
        name = str(data.get("name", "")).strip()
        env = data.get("env")
        if not name or not isinstance(env, dict):
            await send_response(writer, 400, "application/json", b'{"error":"name and env required"}')
            return
        save_env_profile(name, env)
        await send_response(writer, 200, "application/json", b'{"ok":true}')
        return
    elif path == "/api/env-profiles/delete":
        name = str(data.get("name", "")).strip()
        if not name:
            await send_response(writer, 400, "application/json", b'{"error":"name required"}')
            return
        delete_env_profile(name)
        await send_response(writer, 200, "application/json", b'{"ok":true}')
        return
    elif path == "/api/browse":
        result = browse_directory(data.get("path", ""))
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/browse-files":
        result = browse_files(data.get("path", ""))
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/search-files":
        result = search_files(data.get("path", ""), data.get("query", ""))
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/remote-files/list":
        result = await asyncio.get_event_loop().run_in_executor(None, remote_ls, data.get("target_id", ""), data.get("path", ""))
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/remote-files/cache":
        result = await asyncio.get_event_loop().run_in_executor(None, remote_cache_file, data.get("target_id", ""), data.get("path", ""), data.get("cwd", ""))
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/sessions/delete":
        sid = data.get("session_id", "")
        cwd = data.get("cwd", "")
        delete_session(sid, cwd)
        await send_response(writer, 200, "application/json", b'{"ok":true}')
        return
    elif path == "/api/sessions/rename":
        ok, error = rename_session(data.get("session_id", ""), data.get("title", ""))
        status = 200 if ok else 400
        resp = json.dumps({"ok": ok, "error": error}, ensure_ascii=False).encode("utf-8")
        await send_response(writer, status, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/sessions/history":
        sid = data.get("session_id", "")
        cwd = data.get("cwd", "")
        history = load_session_history(sid, cwd)
        resp = json.dumps(history, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/upload/delete":
        result = delete_uploaded_files(data.get("paths") or [])
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/remote-targets":
        try:
            saved = remote_manager.save_target(data)
        except ValueError as exc:
            resp = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            await send_response(writer, 400, "application/json; charset=utf-8", resp)
            return
        resp = json.dumps(saved, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/remote-targets/delete":
        remote_manager.delete_target(data.get("id", ""))
        await send_response(writer, 200, "application/json", b'{"ok":true}')
        return
    elif path == "/api/remote-targets/test":
        target = data if data.get("host") else data.get("id", "")
        result = await asyncio.get_event_loop().run_in_executor(None, remote_manager.test_target, target)
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/install-cli":
        result = await install_cli()
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/update":
        result = await apply_update()
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/restart":
        await send_response(writer, 200, "application/json", b'{"ok":true}')
        # 先把响应发出去，再延迟重启，确保前端能进入轮询
        asyncio.get_event_loop().call_later(0.5, restart_server)
        return
    elif path == "/api/clis":
        cli_path = data.get("path", "")
        if cli_path:
            set_current_cli(cli_path)
            # 持久化为上次选择，重启后恢复
            update_gui_settings({"cli_path": cli_path})
            await send_response(writer, 200, "application/json", b'{"ok":true}')
        else:
            await send_response(writer, 400, "application/json", b'{"error":"missing path"}')
        return
    else:
        await send_response(writer, 404, "application/json", b'{"error":"not found"}')
        return

    await send_response(writer, 200, "application/json", b'{"ok":true}')


# ─── 静态文件 ──────────────────────────────────────────────
async def handle_static(path: str, writer: asyncio.StreamWriter):
    if path == "/" or path == "":
        path = "/index.html"

    if path.startswith("/static/"):
        file_path = STATIC_DIR / path[8:]
    else:
        file_path = STATIC_DIR / path.lstrip("/")

    try:
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())):
            await send_response(writer, 403, "text/plain", b"Forbidden")
            return
    except Exception:
        await send_response(writer, 400, "text/plain", b"Bad request")
        return

    if not file_path.exists() or not file_path.is_file():
        await send_response(writer, 404, "text/plain", b"Not Found")
        return

    ext = file_path.suffix.lower()
    content_type = MIME_TYPES.get(ext, "application/octet-stream")
    content = file_path.read_bytes()

    # 给 HTML 中的 app.js / style.css 注入基于文件修改时间的版本号，
    # 避免浏览器使用缓存的旧脚本/样式。
    if ext in (".html", ".htm"):
        try:
            text = content.decode("utf-8")
            for asset in ("app.js", "style.css"):
                asset_path = STATIC_DIR / asset
                if asset_path.exists():
                    ver = int(asset_path.stat().st_mtime)
                    text = text.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
            content = text.encode("utf-8")
        except Exception:
            pass

    await send_response(writer, 200, content_type, content)


async def send_response(writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes):
    status_text = {
        200: "OK",
        201: "Created",
        204: "No Content",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        413: "Payload Too Large",
        500: "Internal Server Error",
    }
    response = (
        f"HTTP/1.1 {status} {status_text.get(status, 'Unknown')}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-cache, no-store\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    writer.write(response.encode() + body)
    await writer.drain()


async def main():
    # 恢复上次选中的 CLI（启动时 _current_cli 默认是第一个检测到的，这里覆盖为用户上次的选择）
    saved_cli = get_gui_settings().get("cli_path", "")
    if saved_cli and saved_cli in [c["path"] for c in get_available_clis()]:
        set_current_cli(saved_cli)

    server = None
    last_error = None
    for port in range(DEFAULT_PORT, 65536):
        try:
            server = await asyncio.start_server(handle_http, HOST, port)
            break
        except OSError as exc:
            last_error = exc

    if server is None:
        raise RuntimeError(f"Unable to bind port {DEFAULT_PORT}-65535: {last_error}")

    local_url = f"http://{BROWSER_HOST}:{port}"
    lan_urls = [f"http://{ip}:{port}" for ip in get_lan_ips()]
    if port != DEFAULT_PORT:
        print(f"[CC Bridge] Port {DEFAULT_PORT} is unavailable, using {port}")
    print(f"[CC Bridge] Server running at {local_url}")
    for lan_url in lan_urls:
        print(f"[CC Bridge] LAN access: {lan_url}")
    if not lan_urls:
        print("[CC Bridge] LAN access: no LAN IPv4 address detected")
    print(f"[CC Bridge] Press Ctrl+C to stop")

    # 自动打开浏览器
    import webbrowser
    webbrowser.open(local_url)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[CC Bridge] Server stopped.")
