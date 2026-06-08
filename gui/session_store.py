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
    """返回所有历史会话，按 updated_at 倒序"""
    sessions = _load()
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions


def save_session(session_id: str, title: str, model: str, cwd: str) -> dict:
    """创建或更新会话记录"""
    sessions = _load()
    now = datetime.now().isoformat(timespec="seconds")

    # 查找已有记录
    for s in sessions:
        if s["session_id"] == session_id:
            s["title"] = title or s.get("title", "")
            s["model"] = model
            s["cwd"] = cwd
            s["updated_at"] = now
            _save(sessions)
            return s

    # 新建记录
    entry = {
        "session_id": session_id,
        "title": title or "新会话",
        "model": model,
        "cwd": cwd,
        "created_at": now,
        "updated_at": now,
    }
    sessions.insert(0, entry)
    _save(sessions)
    return entry


def delete_session(session_id: str) -> bool:
    """删除会话记录"""
    sessions = _load()
    new_sessions = [s for s in sessions if s["session_id"] != session_id]
    if len(new_sessions) < len(sessions):
        _save(new_sessions)
        return True
    return False


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


def load_session_history(session_id: str, cwd: str, max_messages: int = 50) -> list[dict]:
    """从 ccb 的 .jsonl 文件中加载历史消息"""
    sanitized = _sanitize_cwd(cwd)
    jsonl_path = Path.home() / ".claude" / "projects" / sanitized / f"{session_id}.jsonl"

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
                    content = obj.get("message", {}).get("content", "")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                break
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
