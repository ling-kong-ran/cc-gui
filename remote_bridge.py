"""
Remote Bridge - 一个用 Python 标准库实现的 stdio MCP server。

它向 Claude Code CLI 暴露一组「远程只读/读写」工具，内部通过系统自带的 ssh 客户端
在目标 Linux 机器上执行命令。target 端零安装（只需开启 sshd）。

由 cc-bridge 在启动 CLI 时通过 --mcp-config 加载。运行所需信息从环境变量读取：
  CCB_REMOTE_TARGET        目标机器 JSON（host/user/port/key_path…）
  CCB_REMOTE_ALLOW_MUTATE  "1" 时额外暴露可变更（读写模式）工具，默认只读模式
  CCB_REMOTE_AUDIT         审计日志路径（可选）

协议：MCP 走 stdio + 行分隔的 JSON-RPC 2.0。stdout 只输出协议消息，日志一律走 stderr。
"""
import sys
import os
import json
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from remote_manager import run_remote_command
except Exception:  # pragma: no cover - 兜底，避免 import 失败导致 MCP 起不来
    def run_remote_command(target, command, timeout=60):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=accept-new",
                "-p", str(int(target.get("port") or 22))]
        if target.get("key_path"):
            argv += ["-i", target["key_path"]]
        argv.append(f"{target.get('user','')}@{target.get('host','')}")
        argv.append(command)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return {"ok": False, "error": "ssh_not_found"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": proc.stdout or "", "stderr": proc.stderr or ""}

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ccb-remote-bridge"
SERVER_VERSION = "0.1.0"

MAX_OUTPUT_CHARS = 60_000
DEFAULT_TIMEOUT = 60

# 只读模式允许的命令首词（常用查看命令、本身不改系统）
READ_ONLY_ALLOW = {
    "cat", "head", "tail", "ls", "stat", "file", "wc", "grep", "egrep", "zgrep",
    "find", "ps", "top", "df", "du", "free", "uptime", "uname", "hostname",
    "whoami", "id", "date", "printenv", "which", "echo", "pwd",
    "journalctl", "dmesg", "systemctl", "service", "ss", "netstat", "ip",
    "ifconfig", "ping", "lsof", "vmstat", "iostat", "sar", "awk", "sed",
    "sort", "uniq", "cut", "tr", "nl", "tac", "getent", "lsblk", "mount",
}
# systemctl/service 只读子命令
_SERVICE_READONLY = {"status", "show", "is-active", "is-enabled", "is-failed",
                     "list-units", "list-unit-files", "cat", "list-dependencies"}
# 危险重定向/链式符号，只读模式下禁止
_READONLY_FORBIDDEN_TOKENS = (">", ">>", "|tee", "| tee", "&&", "||", ";", "`", "$(")
# 即便在变更模式下也硬阻断的灾难性命令
_CATASTROPHIC = (
    "mkfs", "dd of=/dev", "of=/dev/sd", "of=/dev/nvme",
    ":(){", "rm -rf /\x00", "rm -rf / ", "rm -rf /*", "rm -rf --no-preserve-root",
    "> /dev/sda", "wipefs",
)


def _audit(line: str):
    path = os.environ.get("CCB_REMOTE_AUDIT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _load_target() -> dict:
    raw = os.environ.get("CCB_REMOTE_TARGET", "")
    try:
        target = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        target = {}
    return target if isinstance(target, dict) else {}


TARGET = _load_target()
ALLOW_MUTATE = os.environ.get("CCB_REMOTE_ALLOW_MUTATE") == "1"


def _is_catastrophic(command: str) -> bool:
    low = command.strip().lower()
    if low in ("rm -rf /", "rm -fr /"):
        return True
    return any(tok in low for tok in _CATASTROPHIC)


def _readonly_violation(command: str) -> str:
    """返回只读模式下的违规原因；通过则返回空串。"""
    stripped = command.strip()
    if not stripped:
        return "命令为空"
    for tok in _READONLY_FORBIDDEN_TOKENS:
        if tok in stripped:
            return f"只读模式禁止使用 `{tok.strip()}`，如需变更请在 GUI 开启允许远程写入"
    try:
        parts = shlex.split(stripped)
    except ValueError:
        return "命令解析失败"
    if not parts:
        return "命令为空"
    head = os.path.basename(parts[0])
    # 允许管道：对每一段做首词校验
    if "|" in stripped:
        for segment in stripped.split("|"):
            seg = segment.strip()
            if not seg:
                continue
            try:
                seg_parts = shlex.split(seg)
            except ValueError:
                return "命令解析失败"
            if not seg_parts:
                continue
            cmd = os.path.basename(seg_parts[0])
            if cmd not in READ_ONLY_ALLOW:
                return f"只读模式不允许命令 `{cmd}`"
            if cmd in ("systemctl", "service") and len(seg_parts) >= 2 and seg_parts[1] not in _SERVICE_READONLY:
                return f"只读模式下 {cmd} 仅允许只读子命令（如 status）"
        return ""
    if head not in READ_ONLY_ALLOW:
        return f"只读模式不允许命令 `{head}`，仅支持诊断类命令"
    if head in ("systemctl", "service") and len(parts) >= 2 and parts[1] not in _SERVICE_READONLY:
        return f"只读模式下 {head} 仅允许只读子命令（如 status）"
    return ""


def _run_remote(remote_command: str, mode: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """在目标机执行命令（密码/密钥自动选择），返回 {ok, text}。"""
    if not TARGET.get("host") or not TARGET.get("user"):
        return {"ok": False, "text": "未配置远程目标（host/user 缺失）"}

    if _is_catastrophic(remote_command):
        _audit(f"{datetime.now().isoformat(timespec='seconds')}\tBLOCKED\t{mode}\t{TARGET.get('host')}\t{remote_command}")
        return {"ok": False, "text": "命令被安全策略阻断（灾难性操作）"}

    stamp = datetime.now().isoformat(timespec="seconds")
    _audit(f"{stamp}\tRUN\t{mode}\t{TARGET.get('host')}\t{remote_command}")
    res = run_remote_command(TARGET, remote_command, timeout=timeout)

    if res.get("error") and "exit_code" not in res:
        reasons = {
            "ssh_not_found": "server 上未找到 ssh 客户端",
            "timeout": f"命令在 {timeout}s 内未完成（超时）",
            "auth_failed": "认证失败：请检查密码/密钥",
            "missing_password": "未配置密码",
            "missing_host_or_user": "未配置远程目标（host/user 缺失）",
            "ssh_failed": "SSH 连接失败",
        }
        msg = reasons.get(res["error"], res.get("detail") or res["error"])
        _audit(f"{stamp}\t{res['error'].upper()}\t{mode}\t{TARGET.get('host')}\t{remote_command}")
        return {"ok": False, "text": msg}

    code = res.get("exit_code", -1)
    out = res.get("stdout") or ""
    err = res.get("stderr") or ""
    body = out
    if err.strip():
        body += ("\n[stderr]\n" + err)
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n…[输出已截断]"
    text = f"$ {remote_command}\n[exit={code}]\n{body}".rstrip()
    _audit(f"{stamp}\tEXIT={code}\t{mode}\t{TARGET.get('host')}\t{remote_command}")
    return {"ok": res.get("ok", False), "text": text}


# ─── 工具实现 ──────────────────────────────────────────────
def tool_remote_run(args: dict) -> dict:
    command = str(args.get("command", "")).strip()
    if not command:
        return {"ok": False, "text": "command 不能为空"}
    violation = _readonly_violation(command)
    if violation:
        return {"ok": False, "text": violation}
    return _run_remote(command, mode="ro")


def tool_remote_read_file(args: dict) -> dict:
    path = str(args.get("path", "")).strip()
    if not path:
        return {"ok": False, "text": "path 不能为空"}
    max_bytes = int(args.get("max_bytes") or 200_000)
    cmd = f"head -c {max_bytes} -- {shlex.quote(path)}"
    return _run_remote(cmd, mode="ro")


def tool_remote_tail(args: dict) -> dict:
    path = str(args.get("path", "")).strip()
    if not path:
        return {"ok": False, "text": "path 不能为空"}
    lines = int(args.get("lines") or 200)
    cmd = f"tail -n {lines} -- {shlex.quote(path)}"
    return _run_remote(cmd, mode="ro")


def tool_remote_list(args: dict) -> dict:
    path = str(args.get("path", ".")).strip() or "."
    cmd = f"ls -lah -- {shlex.quote(path)}"
    return _run_remote(cmd, mode="ro")


def tool_remote_grep(args: dict) -> dict:
    pattern = str(args.get("pattern", ""))
    path = str(args.get("path", "")).strip()
    if not pattern or not path:
        return {"ok": False, "text": "pattern 和 path 不能为空"}
    max_lines = int(args.get("max_lines") or 200)
    cmd = f"grep -nIr -e {shlex.quote(pattern)} -- {shlex.quote(path)} | head -n {max_lines}"
    return _run_remote(cmd, mode="ro")


def tool_remote_sysinfo(args: dict) -> dict:
    cmd = (
        "echo '== uname =='; uname -a; "
        "echo; echo '== uptime/load =='; uptime; "
        "echo; echo '== cpu/mem =='; free -h; "
        "echo; echo '== disk =='; df -h; "
        "echo; echo '== top processes =='; ps -eo pid,ppid,user,%cpu,%mem,comm --sort=-%cpu | head -n 12; "
        "echo; echo '== failed services =='; systemctl --failed --no-legend 2>/dev/null | head -n 20; "
        "echo; echo '== recent errors =='; journalctl -p err -n 30 --no-pager 2>/dev/null"
    )
    return _run_remote(cmd, mode="ro", timeout=45)


def tool_remote_exec(args: dict) -> dict:
    command = str(args.get("command", "")).strip()
    if not command:
        return {"ok": False, "text": "command 不能为空"}
    if not ALLOW_MUTATE:
        return {"ok": False, "text": "未开启允许远程写入：请在 GUI 中为本会话打开「允许远程写入」后重试"}
    return _run_remote(command, mode="mutate")


# 工具注册表：name -> (描述, inputSchema, 处理函数, 是否需要 mutate)
def _build_tools():
    tools = [
        ("remote_run", "在远程 Linux 机器上执行只读命令（cat/tail/grep/systemctl status 等），用于查看问题。",
         {"type": "object", "properties": {"command": {"type": "string", "description": "要执行的只读 shell 命令"}}, "required": ["command"]},
         tool_remote_run, False),
        ("remote_read_file", "读取远程机器上某个文件的内容（默认最多 200KB）。",
         {"type": "object", "properties": {"path": {"type": "string"}, "max_bytes": {"type": "integer"}}, "required": ["path"]},
         tool_remote_read_file, False),
        ("remote_tail", "查看远程日志文件末尾若干行（默认 200 行）。",
         {"type": "object", "properties": {"path": {"type": "string"}, "lines": {"type": "integer"}}, "required": ["path"]},
         tool_remote_tail, False),
        ("remote_list", "列出远程目录内容（ls -lah）。",
         {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
         tool_remote_list, False),
        ("remote_grep", "在远程目录中递归搜索关键字（grep -nIr）。",
         {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "max_lines": {"type": "integer"}}, "required": ["pattern", "path"]},
         tool_remote_grep, False),
        ("remote_sysinfo", "采集远程机器健康快照：系统信息、负载、内存、磁盘、Top 进程、失败服务、近期错误日志。",
         {"type": "object", "properties": {}, "required": []},
         tool_remote_sysinfo, False),
    ]
    if ALLOW_MUTATE:
        tools.append((
            "remote_exec", "在远程机器上执行可变更系统的命令（重启服务、改配置等）。仅在用户开启「允许远程写入」后可用，请谨慎使用。",
            {"type": "object", "properties": {"command": {"type": "string", "description": "要执行的命令"}}, "required": ["command"]},
            tool_remote_exec, True))
    return tools


TOOLS = _build_tools()
TOOL_HANDLERS = {name: handler for (name, _d, _s, handler, _m) in TOOLS}


# ─── JSON-RPC over stdio ───────────────────────────────────
def _write(msg: dict):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle(msg: dict):
    method = msg.get("method")
    req_id = msg.get("id")
    is_request = req_id is not None

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        _result(req_id, {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "tools/list":
        _result(req_id, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for (name, desc, schema, _h, _m) in TOOLS
        ]})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            _error(req_id, -32602, f"未知工具: {name}")
            return
        try:
            outcome = handler(arguments)
        except Exception as exc:  # pragma: no cover
            _result(req_id, {"content": [{"type": "text", "text": f"工具执行异常: {exc}"}], "isError": True})
            return
        _result(req_id, {
            "content": [{"type": "text", "text": outcome.get("text", "")}],
            "isError": not outcome.get("ok", False),
        })
    elif method == "ping":
        _result(req_id, {})
    elif is_request:
        _error(req_id, -32601, f"未实现的方法: {method}")
    # 通知（无 id）不回应


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as exc:  # pragma: no cover
            sys.stderr.write(f"[remote-bridge] handler error: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
