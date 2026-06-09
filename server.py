"""
CCB GUI Server - 纯 Python 标准库实现
HTTP 静态文件 + REST API + SSE (Server-Sent Events) 通信
使用 SSE 替代 WebSocket 避免 Windows asyncio 兼容性问题
"""
import asyncio
import json
import os
import sys
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))

from ccb_bridge import SessionManager, get_available_clis, get_current_cli, set_current_cli
from config_manager import (
    get_settings,
    save_settings,
    get_env_config,
    update_env_config,
    get_gui_settings,
    update_gui_settings,
    list_skills,
    list_agents,
    get_available_models,
)
from session_store import list_sessions, save_session, delete_session, load_session_history

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_CWD = str(Path(__file__).parent.resolve())  # 项目根目录作为默认 CWD
HOST = "127.0.0.1"

session_manager = SessionManager()

# SSE 客户端连接池: client_id -> asyncio.Queue
sse_clients: dict[str, asyncio.Queue] = {}

# 每个 client 的最后一条用户消息（用于会话标题）
client_last_msg: dict[str, str] = {}

# 每个 client 关联的 ccb session id
client_session_ids: dict[str, str] = {}

# 每个 client 的会话参数（model, cwd）
client_meta: dict[str, dict] = {}

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


# ─── SSE 推送 ──────────────────────────────────────────────
async def push_event(client_id: str, event_type: str, data: dict):
    """向指定 SSE 客户端推送事件"""
    queue = sse_clients.get(client_id)
    if queue:
        await queue.put({"event": event_type, "data": data})


UPLOAD_DIR_FALLBACK = Path(__file__).parent / "uploads"
UPLOAD_DIR_FALLBACK.mkdir(exist_ok=True)


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
        if content_length > 0:
            body = await reader.readexactly(content_length)

        # 路由
        parsed = urlparse(path)
        route_path = parsed.path
        query = parse_qs(parsed.query)

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

    if action == "new_session":
        model = data.get("model", "claude-sonnet-4-6")
        cwd = data.get("cwd")
        skip_perms = data.get("skip_permissions", True)

        # 清理旧 session
        old_session = session_manager.get_session(client_id)
        if old_session:
            await old_session.stop()

        _, session = session_manager.create_session()
        session_manager.sessions[client_id] = session

        # 记录元数据
        client_meta[client_id] = {"model": model, "cwd": cwd}
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
                save_session(sid, title, meta.get("model", model), meta.get("cwd", cwd or ""))
                await push_event(client_id, "session_id_captured", event)
            elif evt_type in ("assistant", "system", "result", "error", "process_ended"):
                # ccb 高层事件直接按类型推送，前端有对应 listener
                await push_event(client_id, evt_type, event)
            # 其他事件（hook_started 等）忽略

        await session.start(model=model, cwd=cwd, on_event=on_event, skip_permissions=skip_perms)
        await push_event(client_id, "session_started", {"model": model})
        await send_response(writer, 200, "application/json", b'{"ok":true}')

    elif action == "resume_session":
        resume_id = data.get("session_id", "")
        model = data.get("model", "claude-sonnet-4-6")
        cwd = data.get("cwd")
        skip_perms = data.get("skip_permissions", True)

        # 清理旧 session
        old_session = session_manager.get_session(client_id)
        if old_session:
            await old_session.stop()

        _, session = session_manager.create_session()
        session_manager.sessions[client_id] = session

        client_meta[client_id] = {"model": model, "cwd": cwd}
        client_session_ids[client_id] = resume_id

        async def on_event_resume(event: dict):
            evt_type = event.get("type", "unknown")
            if evt_type == "session_id_captured":
                sid = event.get("session_id", "")
                client_session_ids[client_id] = sid
                meta = client_meta.get(client_id, {})
                save_session(sid, "", meta.get("model", model), meta.get("cwd", cwd or ""))
                await push_event(client_id, "session_id_captured", event)
            elif evt_type in ("assistant", "system", "result", "error", "process_ended"):
                await push_event(client_id, evt_type, event)
            # 其他事件忽略

        await session.start(model=model, cwd=cwd, resume_id=resume_id, on_event=on_event_resume, skip_permissions=skip_perms)
        await push_event(client_id, "session_started", {"model": model, "resumed": True, "session_id": resume_id})
        await send_response(writer, 200, "application/json", b'{"ok":true}')

    elif action == "send_message":
        content = data.get("content", "")
        session = session_manager.get_session(client_id)
        if session and session.is_running and content:
            # 使用最新用户消息作为会话标题
            title = content.strip()[:50]
            client_last_msg[client_id] = title
            sid = client_session_ids.get(client_id)
            if sid:
                meta = client_meta.get(client_id, {})
                save_session(sid, title, meta.get("model", ""), meta.get("cwd", ""))
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

    else:
        await send_response(writer, 400, "application/json", b'{"error":"unknown action"}')


# ─── REST API ──────────────────────────────────────────────
async def handle_api_get(path: str, writer: asyncio.StreamWriter, query: dict = None):
    if path == "/api/settings":
        data = get_settings()
    elif path == "/api/gui-settings":
        data = get_gui_settings()
    elif path == "/api/env":
        data = get_env_config()
    elif path == "/api/skills":
        data = list_skills()
    elif path == "/api/agents":
        data = list_agents()
    elif path == "/api/models":
        data = get_available_models()
    elif path == "/api/clis":
        data = {"available": get_available_clis(), "current": get_current_cli()}
    elif path == "/api/default-cwd":
        data = {"cwd": DEFAULT_CWD}
    elif path == "/api/sessions":
        data = list_sessions()
    elif path == "/api/file":
        # 提供上传文件（图片预览）
        file_path = (query or {}).get("path", [""])[0]
        if not file_path:
            await send_response(writer, 400, "text/plain", b"missing path")
            return
        fp = Path(file_path)
        # 安全检查：只允许访问 .gui-uploads 或 fallback uploads 目录
        try:
            resolved = str(fp.resolve())
            is_fallback = resolved.startswith(str(UPLOAD_DIR_FALLBACK.resolve()))
            is_gui_upload = ".gui-uploads" in resolved
            if not (is_fallback or is_gui_upload):
                await send_response(writer, 403, "text/plain", b"forbidden")
                return
        except Exception:
            await send_response(writer, 400, "text/plain", b"bad path")
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
        result = update_gui_settings(data)
        resp = json.dumps(result, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/env":
        update_env_config(data)
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
    elif path == "/api/sessions/delete":
        sid = data.get("session_id", "")
        delete_session(sid)
        await send_response(writer, 200, "application/json", b'{"ok":true}')
        return
    elif path == "/api/sessions/history":
        sid = data.get("session_id", "")
        cwd = data.get("cwd", "")
        history = load_session_history(sid, cwd)
        resp = json.dumps(history, ensure_ascii=False).encode("utf-8")
        await send_response(writer, 200, "application/json; charset=utf-8", resp)
        return
    elif path == "/api/clis":
        cli_path = data.get("path", "")
        if cli_path:
            set_current_cli(cli_path)
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
    await send_response(writer, 200, content_type, content)


async def send_response(writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes):
    status_text = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 500: "Internal Server Error"}
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


# ─── 主入口 ────────────────────────────────────────────────
def find_free_port() -> int:
    """找一个系统未占用的端口"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main():
    port = find_free_port()
    server = await asyncio.start_server(handle_http, HOST, port)
    url = f"http://{HOST}:{port}"
    print(f"[CCB GUI] Server running at {url}")
    print(f"[CCB GUI] Press Ctrl+C to stop")

    # 自动打开浏览器
    import webbrowser
    webbrowser.open(url)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[CCB GUI] Server stopped.")
