"""
Session Store - 会话元数据持久化
存储位置: ~/.claude/gui_sessions.json
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

STORE_PATH = Path.home() / ".claude" / "gui_sessions.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
HIDDEN_PATH = Path.home() / ".claude" / "gui_hidden_sessions.json"


def _load_hidden() -> set[str]:
    """读取被隐藏（已从 GUI 删除）的会话 id 集合。"""
    if not HIDDEN_PATH.exists():
        return set()
    try:
        data = json.loads(HIDDEN_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data if x}
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def _save_hidden(hidden: set[str]):
    HIDDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    HIDDEN_PATH.write_text(json.dumps(sorted(hidden), ensure_ascii=False, indent=2), encoding="utf-8")



def _load() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(sessions: list[dict]):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")


def list_sessions() -> list[dict]:
    """返回本机所有历史会话，按底层 jsonl 修改时间倒序。"""
    indexed_sessions = _load()
    hidden = _load_hidden()
    changed = False
    for s in indexed_sessions:
        if "total_cost_usd" not in s:
            s["total_cost_usd"] = 0
            changed = True
        last_user_msg = get_last_user_message(s.get("session_id", ""), s.get("cwd", ""))
        if last_user_msg and s.get("title") != last_user_msg[:50]:
            s["title"] = last_user_msg[:50]
            changed = True
    if changed:
        _save(indexed_sessions)

    sessions_by_id = {
        s.get("session_id"): dict(s)
        for s in indexed_sessions
        if s.get("session_id") and s.get("session_id") not in hidden
    }

    for discovered in discover_local_sessions():
        sid = discovered.get("session_id")
        if not sid or sid in hidden:
            continue
        existing = sessions_by_id.get(sid, {})
        merged = dict(discovered)
        if existing:
            merged["title"] = discovered.get("title") or existing.get("title", "")
            merged["model"] = discovered.get("model") or existing.get("model", "")
            merged["cwd"] = discovered.get("cwd") or existing.get("cwd", "")
            merged["total_cost_usd"] = float(existing.get("total_cost_usd") or 0)
            merged["created_at"] = existing.get("created_at") or discovered.get("created_at", "")
            merged["source"] = existing.get("source") or "gui"
        sessions_by_id[sid] = merged

    sessions = list(sessions_by_id.values())
    sessions.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    return sessions


def discover_local_sessions() -> list[dict]:
    """扫描 ~/.claude/projects 下的顶层会话 jsonl。"""
    if not PROJECTS_DIR.exists():
        return []

    sessions = []
    try:
        project_dirs = [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]
    except OSError:
        return []

    for project_dir in project_dirs:
        try:
            jsonl_files = [p for p in project_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"]
        except OSError:
            continue
        for jsonl_path in jsonl_files:
            entry = parse_session_jsonl(jsonl_path)
            if entry:
                sessions.append(entry)
    return sessions


def parse_session_jsonl(jsonl_path: Path) -> dict | None:
    session_id = jsonl_path.stem
    cwd = ""
    model = ""
    title = ""
    last_prompt = ""
    first_ts = ""
    last_ts = ""

    try:
        stat = jsonl_path.stat()
        mtime = stat.st_mtime
        updated_at = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                session_id = obj.get("sessionId") or obj.get("session_id") or session_id
                if obj.get("cwd"):
                    cwd = obj.get("cwd", "")
                timestamp = obj.get("timestamp")
                if timestamp:
                    if not first_ts:
                        first_ts = timestamp
                    last_ts = timestamp
                msg = obj.get("message", {})
                if isinstance(msg, dict) and msg.get("model"):
                    model = msg.get("model", "")
                if obj.get("type") == "user":
                    text = _extract_user_text(obj)
                    if text:
                        title = text[:50]
                elif obj.get("type") == "last-prompt":
                    prompt = _clean_user_text(obj.get("lastPrompt", ""))
                    if prompt:
                        last_prompt = prompt[:50]
    except OSError:
        return None

    # 探测类启动（如读取 slash 命令的 /help 短命会话）只会留下没有真实用户
    # 消息、也没有 last-prompt 的空 jsonl。跳过它们，避免列表里冒出空"新会话"。
    if (
        not last_prompt
        and (not title or title == "Unknown skill: help")
    ):
        return None

    return {
        "session_id": session_id,
        "title": last_prompt or title or "新会话",
        "model": model,
        "cwd": cwd,
        "total_cost_usd": 0,
        "created_at": first_ts or updated_at,
        "updated_at": updated_at,
        "mtime": mtime,
        "source": "local",
    }


def save_session(session_id: str, title: str, model: str, cwd: str,
                 remote_target_id: str = "") -> dict:
    """创建或更新会话记录"""
    sessions = _load()
    now = datetime.now().isoformat(timespec="seconds")

    # 若该会话之前被删除（隐藏），重新激活时取消隐藏
    hidden = _load_hidden()
    if session_id in hidden:
        hidden.discard(session_id)
        _save_hidden(hidden)

    # 查找已有记录
    for s in sessions:
        if s["session_id"] == session_id:
            s["title"] = title or s.get("title", "")
            s["model"] = model
            s["cwd"] = cwd
            s["total_cost_usd"] = float(s.get("total_cost_usd") or 0)
            s["updated_at"] = now
            if remote_target_id:
                s["remote_target_id"] = remote_target_id
            _save(sessions)
            return s

    # 新建记录
    entry = {
        "session_id": session_id,
        "title": title or "新会话",
        "model": model,
        "cwd": cwd,
        "total_cost_usd": 0,
        "remote_target_id": remote_target_id,
        "created_at": now,
        "updated_at": now,
    }
    sessions.insert(0, entry)
    _save(sessions)
    return entry


def add_session_cost(session_id: str, cost_usd: float) -> float:
    """累加会话费用并返回最新累计值。"""
    if not session_id or cost_usd <= 0:
        return 0

    sessions = _load()
    now = datetime.now().isoformat(timespec="seconds")
    for s in sessions:
        if s["session_id"] == session_id:
            total = float(s.get("total_cost_usd") or 0) + float(cost_usd)
            s["total_cost_usd"] = round(total, 8)
            s["updated_at"] = now
            _save(sessions)
            return s["total_cost_usd"]

    return 0


def _delete_session_files(session_id: str, cwd: str = "") -> bool:
    """删除会话的本地转录文件 ~/.claude/projects/<dir>/<session_id>.jsonl。

    优先用 cwd 推导路径，同时扫描所有项目目录兜底（cwd 可能缺失或与实际目录不一致）。
    返回是否至少删除了一个文件。
    """
    targets = []
    if cwd:
        targets.append(_jsonl_path(session_id, cwd))
    if PROJECTS_DIR.exists():
        try:
            for project_dir in PROJECTS_DIR.iterdir():
                if project_dir.is_dir():
                    targets.append(project_dir / f"{session_id}.jsonl")
        except OSError:
            pass

    deleted = False
    seen = set()
    for path in targets:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.exists() and path.is_file():
                path.unlink()
                deleted = True
        except OSError:
            pass
    return deleted


def delete_session(session_id: str, cwd: str = "") -> bool:
    """彻底删除会话：移除 GUI 索引并删除本地转录文件，不可恢复。

    若转录文件因被占用等原因无法删除，则记入隐藏集合，保证其不再出现在列表中。
    """
    if not session_id:
        return False

    sessions = _load()
    new_sessions = [s for s in sessions if s["session_id"] != session_id]
    if len(new_sessions) < len(sessions):
        _save(new_sessions)

    removed_file = _delete_session_files(session_id, cwd)

    if not removed_file:
        # 文件未能删除（可能正被占用），隐藏以免重新出现
        hidden = _load_hidden()
        if session_id not in hidden:
            hidden.add(session_id)
            _save_hidden(hidden)
    return True


def get_session(session_id: str) -> Optional[dict]:
    """获取单条会话记录"""
    for s in _load():
        if s["session_id"] == session_id:
            return s
    return None


def _sanitize_cwd(cwd: str) -> str:
    """将 cwd 转为 ccb 的项目目录名格式 (与 ccb sanitizePath 一致: 所有非字母数字→'-')"""
    import re
    return re.sub(r'[^a-zA-Z0-9]', '-', cwd)


def _jsonl_path(session_id: str, cwd: str) -> Path:
    sanitized = _sanitize_cwd(cwd)
    return Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"


def _extract_user_text(obj: dict) -> str:
    content = obj.get("message", {}).get("content", "")
    if isinstance(content, str):
        return _clean_user_text(content)
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "text":
                return _clean_user_text(block.get("text", "") or "")
    return ""


def _clean_user_text(text: str) -> str:
    text = (text or "").strip()
    if (
        text.startswith("<local-command-")
        or text.startswith("<command-name>")
        or text.startswith("This session is being continued from a previous conversation")
        or text.startswith("Unknown skill:")
    ):
        return ""
    return text


def get_last_user_message(session_id: str, cwd: str) -> str:
    """读取会话文件中的最后一条用户消息。"""
    if not session_id or not cwd:
        return ""

    jsonl_path = _jsonl_path(session_id, cwd)
    if not jsonl_path.exists():
        return ""

    last_text = ""
    last_prompt = ""
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type", "") == "user":
                    text = _extract_user_text(obj)
                    if text:
                        last_text = text
                elif obj.get("type") == "last-prompt":
                    prompt = _clean_user_text(obj.get("lastPrompt", ""))
                    if prompt:
                        last_prompt = prompt
    except OSError:
        return ""

    return last_prompt or last_text


def load_session_history(session_id: str, cwd: str, max_messages: int = 50) -> list[dict]:
    """从 ccb 的 .jsonl 文件中加载历史消息"""
    jsonl_path = _jsonl_path(session_id, cwd)

    if not jsonl_path.exists():
        return []

    messages = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")

                if msg_type == "user":
                    text = _extract_user_text(obj)
                    if text:
                        messages.append({"role": "user", "text": text})

                elif msg_type == "assistant":
                    content = obj.get("message", {}).get("content", [])
                    blocks = []
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "text" and block.get("text"):
                                blocks.append({"type": "text", "text": block["text"]})
                            elif block.get("type") == "tool_use":
                                blocks.append({
                                    "type": "tool_use",
                                    "name": block.get("name", ""),
                                    "input": block.get("input", {}),
                                })
                    if blocks:
                        messages.append({"role": "assistant", "blocks": blocks})

    except OSError:
        return []

    # 只返回最后 max_messages 条
    return messages[-max_messages:]
