"""
Remote Manager - 远程目标机器（Linux/SSH）的配置与连通性测试

目标信息持久化在 ~/.ccb/remote_targets.json。
默认所有远端操作都走系统自带的 ssh 客户端 subprocess（server 端零三方依赖）：
  · 密钥登录：ssh -i <key>
  · 密码登录：POSIX 用 pty 喂密码，Windows 用 SSH_ASKPASS 非交互喂密码（均为标准库）。
若 vendor/ 内恰好存在 paramiko，则优先用它作为更稳的加速路径。
target 端零安装（只需开启 sshd）。
"""
import json
import os
import re
import sys
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

# 项目内 vendor/ 目录（可选的 paramiko 等放这里，不污染全局 Python，也不入库）。
# 注意：方案 B 之后密码登录已不依赖 paramiko —— 默认走系统 ssh + 标准库；
# 但若 vendor/ 里恰好有 paramiko，会被当作更稳的加速路径优先使用。
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

CCB_DIR = Path.home() / ".ccb"
TARGETS_FILE = CCB_DIR / "remote_targets.json"
KEYS_DIR = CCB_DIR / "keys"  # 粘贴的私钥落地目录（权限收紧，不入库不回显）

# 使用的 ssh 客户端，可用环境变量覆盖（也是测试注入 fake-ssh 的接缝）
_SSH_BIN = os.environ.get("CCB_SSH_BIN") or "ssh"

# 字段白名单，避免把任意键写进配置
_TARGET_FIELDS = ("id", "name", "host", "port", "user", "key_path", "description")


def _managed_key_path(target_id: str) -> Path:
    return KEYS_DIR / target_id


def _write_key_file(path: Path, content: str):
    """把私钥内容写入文件并尽量收紧权限（粘贴的私钥需要落成文件供 ssh -i 使用）。"""
    norm = content.replace("\r\n", "\n").replace("\r", "\n")
    if not norm.endswith("\n"):
        norm += "\n"
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    # 以 0600 创建，避免出现可被他人读取的时间窗口；O_BINARY 防止 Windows 把 \n 还原成 \r\n
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, norm.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        # Windows OpenSSH 对私钥权限敏感：去继承，仅授权当前用户
        user = os.environ.get("USERNAME") or ""
        if user:
            try:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass


def _materialize_temp_key(content: str) -> Path:
    """把私钥内容写到一个临时文件（用于尚未保存的目标做连接测试）。"""
    path = KEYS_DIR / f"tmp_{uuid.uuid4().hex}"
    _write_key_file(path, content)
    return path


def _resolve_key_file(target: dict, key_file: Optional[str] = None) -> Optional[str]:
    """确定 ssh -i 使用的私钥文件。优先级：显式临时文件 > 路径 > 已保存的粘贴私钥。"""
    if key_file:
        return key_file
    kp = str(target.get("key_path") or "").strip()
    if kp:
        return os.path.expanduser(kp)
    tid = target.get("id")
    if tid:
        managed = _managed_key_path(str(tid))
        if managed.exists():
            return str(managed)
    return None


def _load() -> list[dict]:
    if not TARGETS_FILE.exists():
        return []
    try:
        data = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(targets: list[dict]):
    CCB_DIR.mkdir(parents=True, exist_ok=True)
    TARGETS_FILE.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")


def _has_key(target: dict) -> bool:
    if str(target.get("key_path") or "").strip():
        return True
    tid = target.get("id")
    return bool(tid and _managed_key_path(str(tid)).exists())


def _public(target: dict) -> dict:
    """对外返回的目标信息（私钥/密码只回显是否已配置，绝不回显其内容）。"""
    return {
        "id": target.get("id", ""),
        "name": target.get("name", "") or target.get("host", ""),
        "host": target.get("host", ""),
        "port": int(target.get("port") or 22),
        "user": target.get("user", ""),
        "key_path": target.get("key_path", ""),
        "auth_method": target.get("auth_method") or ("password" if target.get("password") else "key"),
        "has_key": _has_key(target),
        "has_password": bool(target.get("password")),
        "description": target.get("description", ""),
    }


def list_targets() -> list[dict]:
    return [_public(t) for t in _load()]


def get_target(target_id: str) -> Optional[dict]:
    for t in _load():
        if t.get("id") == target_id:
            return dict(t)
    return None


def _normalize(data: dict[str, Any]) -> dict:
    cleaned = {k: data.get(k) for k in _TARGET_FIELDS if k in data}
    cleaned["host"] = str(cleaned.get("host", "")).strip()
    cleaned["user"] = str(cleaned.get("user", "")).strip()
    cleaned["name"] = str(cleaned.get("name", "")).strip()
    cleaned["key_path"] = str(cleaned.get("key_path", "")).strip()
    cleaned["description"] = str(cleaned.get("description", "")).strip()
    try:
        cleaned["port"] = int(cleaned.get("port") or 22)
    except (TypeError, ValueError):
        cleaned["port"] = 22
    return cleaned


def save_target(data: dict[str, Any]) -> dict:
    """新建或更新目标，返回保存后的公开信息。"""
    cleaned = _normalize(data)
    if not cleaned["host"] or not cleaned["user"]:
        raise ValueError("host 和 user 不能为空")

    key_text = str(data.get("key_text") or "").strip()
    auth_method = str(data.get("auth_method") or "").strip().lower()
    if auth_method in ("key", "password"):
        cleaned["auth_method"] = auth_method
    # 密码：留空表示沿用旧值，非空才更新
    password = data.get("password")
    has_new_password = password is not None and str(password) != ""

    targets = _load()
    tid = data.get("id")
    if tid:
        for t in targets:
            if t.get("id") == tid:
                t.update(cleaned)
                t["id"] = tid
                if has_new_password:
                    t["password"] = str(password)
                if key_text:
                    _write_key_file(_managed_key_path(tid), key_text)
                _save(targets)
                return _public(t)

    new_id = uuid.uuid4().hex[:12]
    cleaned["id"] = new_id
    if not cleaned.get("auth_method"):
        cleaned["auth_method"] = "password" if has_new_password else "key"
    if has_new_password:
        cleaned["password"] = str(password)
    if key_text:
        _write_key_file(_managed_key_path(new_id), key_text)
    targets.append(cleaned)
    _save(targets)
    return _public(cleaned)


def delete_target(target_id: str) -> bool:
    targets = _load()
    remaining = [t for t in targets if t.get("id") != target_id]
    if len(remaining) < len(targets):
        _save(remaining)
        # 一并删除已保存的私钥文件
        managed = _managed_key_path(target_id)
        try:
            if managed.exists():
                managed.unlink()
        except OSError:
            pass
        return True
    return False


def build_ssh_argv(target: dict, remote_command: Optional[str] = None,
                   key_file: Optional[str] = None, password_mode: bool = False) -> list[str]:
    """根据目标构造 ssh 命令行。remote_command 为 None 时只返回到 user@host 的连接部分。

    password_mode=True 时去掉 BatchMode（允许密码提示），并强制走密码认证、单次提示，
    供 pty / SSH_ASKPASS 非交互喂密码使用。
    """
    host = str(target.get("host", "")).strip()
    user = str(target.get("user", "")).strip()
    port = int(target.get("port") or 22)

    # 使用动态检测的 SSH 路径或环境变量
    ssh_bin = _find_ssh_client() or _SSH_BIN
    argv = [
        ssh_bin,
        "-o", "ConnectTimeout=8",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(port),
    ]
    if password_mode:
        argv += [
            "-o", "BatchMode=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "NumberOfPasswordPrompts=1",
        ]
    else:
        argv += ["-o", "BatchMode=yes"]
        resolved_key = _resolve_key_file(target, key_file)
        if resolved_key:
            argv += ["-i", resolved_key, "-o", "IdentitiesOnly=yes"]
    argv.append(f"{user}@{host}" if user else host)
    if remote_command is not None:
        argv.append(remote_command)
    return argv


def _paramiko_available() -> bool:
    """vendor/ 或全局是否有 paramiko（有则作为更稳的加速路径）。"""
    try:
        import paramiko  # noqa: F401
        return True
    except Exception:
        return False


def _find_ssh_client() -> Optional[str]:
    """查找 ssh 客户端，在 Windows 上检查常见安装位置。"""
    # 先检查环境变量 CCB_SSH_BIN（可覆盖）
    if _SSH_BIN != "ssh":
        return _SSH_BIN if shutil.which(_SSH_BIN) else None

    # 项目目录及同级目录中的 ssh.exe（便于内网离线分发）
    project_dir = Path(__file__).resolve().parent
    for candidate in (
        project_dir / "ssh.exe",
        project_dir / "ssh" / "ssh.exe",
        project_dir.parent / "ssh.exe",
        project_dir.parent / "ssh" / "ssh.exe",
    ):
        if candidate.exists():
            return str(candidate)

    # 标准 PATH 搜索
    found = shutil.which("ssh")
    if found:
        return found

    # Windows 特殊处理：检查常见安装位置
    if os.name == "nt":
        sys_root = os.environ.get("SystemRoot", r"C:\Windows")
        common_paths = [
            Path(sys_root) / "System32" / "OpenSSH" / "ssh.exe",
            Path(os.environ.get("ProgramFiles", "")) / "OpenSSH" / "ssh.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "OpenSSH" / "ssh.exe",
        ]
        for p in common_paths:
            if p.exists():
                return str(p)

    return None


def password_supported() -> bool:
    """是否能进行非交互密码登录。

    方案 B 之后不再依赖 paramiko：有 paramiko 直接行；否则只要有系统 ssh，
    也能通过 pty（POSIX）/ SSH_ASKPASS（Windows）非交互喂密码。
    """
    if _paramiko_available():
        return True
    return _find_ssh_client() is not None


def _load_pkey_from_string(paramiko, text: str):
    from io import StringIO
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            return cls.from_private_key(StringIO(text))
        except Exception:
            continue
    return None


def _load_pkey_from_file(paramiko, path: str):
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            return cls.from_private_key_file(path)
        except Exception:
            continue
    return None


def _paramiko_run(target: dict, command: str, timeout: int,
                  password: Optional[str] = None, key_text: str = "") -> dict:
    """用 paramiko 执行远端命令，支持密码或密钥。"""
    import paramiko
    host = str(target.get("host", "")).strip()
    user = str(target.get("user", "")).strip()
    port = int(target.get("port") or 22)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=host, port=port, username=user,
                  timeout=8, banner_timeout=10, auth_timeout=12,
                  look_for_keys=False, allow_agent=False)
    try:
        if password is not None:
            kwargs["password"] = password
        else:
            pkey = None
            if key_text:
                pkey = _load_pkey_from_string(paramiko, key_text)
            else:
                kf = _resolve_key_file(target)
                if kf and os.path.exists(kf):
                    pkey = _load_pkey_from_file(paramiko, kf)
            if pkey is not None:
                kwargs["pkey"] = pkey
            else:
                # 没有显式密钥时，允许 paramiko 走默认密钥/agent
                kwargs["look_for_keys"] = True
                kwargs["allow_agent"] = True
        client.connect(**kwargs)
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return {"ok": code == 0, "exit_code": code, "stdout": out, "stderr": err}
    except paramiko.AuthenticationException:
        return {"ok": False, "error": "auth_failed"}
    except Exception as exc:  # SSHException / socket 错误等
        return {"ok": False, "error": "ssh_failed", "detail": str(exc)[-600:]}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _system_ssh_run(target: dict, command: str, timeout: int, key_text: str = "") -> dict:
    """回退路径：用系统 ssh 执行（仅密钥），密码方式不支持。"""
    temp_key = None
    try:
        if key_text:
            temp_key = _materialize_temp_key(key_text)
        argv = build_ssh_argv(target, command, key_file=str(temp_key) if temp_key else None)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return {"ok": False, "error": "ssh_not_found"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": proc.stdout or "", "stderr": proc.stderr or ""}
    finally:
        if temp_key:
            try:
                temp_key.unlink()
            except OSError:
                pass


def _looks_like_auth_failure(text: str) -> bool:
    low = (text or "").lower()
    return ("permission denied" in low
            or "authentication failed" in low
            or "too many authentication failures" in low)


# ─── 方案 B：不依赖第三方库的密码登录（系统 ssh + 标准库） ──────────────
# Windows 走 SSH_ASKPASS（让 ssh 调用一个 helper 拿密码），POSIX 走 pty 喂密码。
# 密码只经环境变量传给子进程，绝不出现在命令行参数 / 磁盘 helper / ssh 输出中。

def _ssh_safe_path(p: str) -> str:
    """Windows 下若路径含非 ASCII，转成 8.3 短路径——否则 ssh 的 posix_spawn 拉不起 helper。
    （实测：非 ASCII 路径下 ssh 报 No such file or directory；短路径可解。）"""
    p = str(p)
    if os.name != "nt":
        return p
    try:
        p.encode("ascii")
        return p
    except UnicodeEncodeError:
        pass
    try:
        import ctypes
        from ctypes import wintypes
        gspn = ctypes.windll.kernel32.GetShortPathNameW
        gspn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        gspn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if gspn(p, buf, 1024):
            return buf.value
    except Exception:
        pass
    return p


# helper 本身不含任何密码：仅把环境变量 CCB_SSH_PASSWORD 原样写到 stdout 供 ssh 读取
_ASKPASS_EMIT = (
    "import os, sys\n"
    "sys.stdout.buffer.write(os.environ.get('CCB_SSH_PASSWORD', '').encode('utf-8'))\n"
    "sys.stdout.buffer.flush()\n"
)


def _ensure_askpass_helper() -> str:
    """在 ~/.ccb 写入 askpass helper（emit.py + .cmd 包装），返回供 SSH_ASKPASS 使用的路径。"""
    CCB_DIR.mkdir(parents=True, exist_ok=True)
    emit = CCB_DIR / "ssh_askpass_emit.py"
    emit.write_text(_ASKPASS_EMIT, encoding="utf-8")
    if os.name == "nt":
        # ssh 的 askpass 必须是可被 CreateProcess 拉起的程序；用 .cmd 包装委托给 python
        py = _ssh_safe_path(sys.executable)
        emit_p = _ssh_safe_path(str(emit))
        cmd = CCB_DIR / "ssh_askpass.cmd"
        cmd.write_bytes(('@echo off\r\n"%s" "%s"\r\n' % (py, emit_p)).encode("utf-8"))
        return _ssh_safe_path(str(cmd))
    # POSIX 一般用 pty，不走这里；仍提供一个 shebang 脚本兜底（老 OpenSSH < 8.4 等场景）
    sh = CCB_DIR / "ssh_askpass.sh"
    sh.write_text('#!/bin/sh\nexec "%s" "%s"\n' % (sys.executable, emit), encoding="utf-8")
    try:
        os.chmod(sh, 0o700)
    except OSError:
        pass
    return str(sh)


def _password_ssh_askpass(target: dict, command: str, timeout: int, password: str) -> dict:
    """用系统 ssh + SSH_ASKPASS 非交互密码登录（主要用于 Windows，纯标准库）。"""
    helper = _ensure_askpass_helper()
    env = dict(os.environ)
    env["SSH_ASKPASS"] = helper
    env["SSH_ASKPASS_REQUIRE"] = "force"   # OpenSSH 8.4+：无 tty 也强制用 askpass
    if not env.get("DISPLAY"):
        env["DISPLAY"] = "localhost:0"     # 兼容老版本（< 8.4，靠 DISPLAY 触发 askpass）
    env["CCB_SSH_PASSWORD"] = password     # 只有子进程可见；本进程环境不受影响
    argv = build_ssh_argv(target, command, password_mode=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=env, stdin=subprocess.DEVNULL, creationflags=flags)
    except FileNotFoundError:
        return {"ok": False, "error": "ssh_not_found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    out, err = proc.stdout or "", proc.stderr or ""
    if proc.returncode != 0 and _looks_like_auth_failure(err):
        return {"ok": False, "error": "auth_failed"}
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "stdout": out, "stderr": err}


def _should_send_password(buf_low: bytes, already_sent: bool) -> str:
    """纯逻辑（便于单测）：根据已累计的小写输出决定动作。
    返回 'send'（首次见到密码提示）/ 'authfail'（已发过又再次提示）/ ''（继续等待）。"""
    if (b"password:" in buf_low) or (b"password for" in buf_low):
        return "authfail" if already_sent else "send"
    return ""


_PTY_NOISE = re.compile(
    r"^(?:.*?@.*?'s password:\s*|password:\s*|permission denied.*|"
    r"warning: permanently added.*|pseudo-terminal will not be allocated.*|"
    r"connection to .* closed\.\s*)$", re.IGNORECASE)


def _clean_pty_output(text: str) -> str:
    """去掉伪终端里混入的密码提示 / 连接噪声，规整换行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if not _PTY_NOISE.match(ln)]
    return "\n".join(lines).strip("\n")


def _terminate_child(pid: int):
    import signal, time
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(25):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return
        except OSError:
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass


def _reap_child(pid: int) -> int:
    try:
        _wpid, status = os.waitpid(pid, 0)
    except OSError:
        return -1
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return -1


def _password_ssh_pty(target: dict, command: str, timeout: int, password: str) -> dict:
    """POSIX：用伪终端驱动系统 ssh，在密码提示处喂入密码（纯标准库，版本无关）。"""
    import pty, select, errno, time
    argv = build_ssh_argv(target, command, password_mode=True)
    try:
        pid, master_fd = pty.fork()
    except OSError as exc:
        return {"ok": False, "error": "ssh_failed", "detail": f"pty.fork: {exc}"}
    if pid == 0:  # 子进程：变成 ssh
        try:
            os.execvp(argv[0], argv)
        except OSError:
            os._exit(127)
        os._exit(127)

    pw_bytes = (password + "\n").encode("utf-8")
    out = bytearray()
    seen = bytearray()       # 仅用于跨 chunk 匹配提示词
    sent = False
    auth_failed = False
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.5))
        except (OSError, ValueError):
            break
        if master_fd not in ready:
            continue
        try:
            chunk = os.read(master_fd, 8192)
        except OSError as exc:
            if exc.errno != errno.EIO:   # EIO = 伪终端 EOF（子进程退出），正常收尾
                pass
            break
        if not chunk:
            break
        out += chunk
        seen += chunk
        low = bytes(seen).lower()
        action = _should_send_password(low, sent)
        if action == "send":
            try:
                os.write(master_fd, pw_bytes)
            except OSError:
                pass
            sent = True
            seen.clear()
        elif action == "authfail" or b"permission denied" in low:
            auth_failed = True
            break
        if len(seen) > 8192:
            del seen[:-1024]

    if timed_out or auth_failed:
        _terminate_child(pid)
        try:
            os.close(master_fd)
        except OSError:
            pass
        return {"ok": False, "error": "timeout" if timed_out else "auth_failed"}

    code = _reap_child(pid)
    try:
        os.close(master_fd)
    except OSError:
        pass
    text = _clean_pty_output(bytes(out).decode("utf-8", "replace"))
    return {"ok": code == 0, "exit_code": code, "stdout": text, "stderr": ""}


def _password_run(target: dict, command: str, timeout: int, password: str) -> dict:
    """密码登录统一分发：有 paramiko 用 paramiko（最稳）；否则系统 ssh + 标准库。"""
    if _paramiko_available():
        return _paramiko_run(target, command, timeout, password=password)
    if os.name == "nt":
        return _password_ssh_askpass(target, command, timeout, password)
    return _password_ssh_pty(target, command, timeout, password)


def run_remote_command(target: dict, command: str, timeout: int = 60) -> dict:
    """统一远端执行入口。

    密码登录：有 paramiko 用 paramiko，否则系统 ssh + 标准库（POSIX pty / Windows askpass）。
    密钥登录：有 paramiko 优先（统一行为），否则系统 ssh。
    返回 {ok, exit_code, stdout, stderr, error, detail}。
    """
    host = str(target.get("host", "")).strip()
    user = str(target.get("user", "")).strip()
    if not host or not user:
        return {"ok": False, "error": "missing_host_or_user"}

    auth = str(target.get("auth_method") or "").strip().lower()
    password = target.get("password")
    has_password = password is not None and str(password) != ""
    key_text = str(target.get("key_text") or "").strip()

    use_password = auth == "password" or (has_password and auth != "key")
    if use_password:
        if not has_password:
            return {"ok": False, "error": "missing_password"}
        return _password_run(target, command, timeout, str(password))

    # 密钥方式：有 paramiko 优先（统一行为），否则系统 ssh
    if _paramiko_available():
        return _paramiko_run(target, command, timeout, key_text=key_text)
    return _system_ssh_run(target, command, timeout, key_text=key_text)


def test_target(data_or_id: Any) -> dict:
    """测试与目标的连通性：在目标机执行 echo，验证能登录。"""
    target = data_or_id if isinstance(data_or_id, dict) else get_target(str(data_or_id))
    if not target:
        return {"ok": False, "error": "target_not_found"}
    if not target.get("host") or not target.get("user"):
        return {"ok": False, "error": "missing_host_or_user"}

    res = run_remote_command(target, "echo CCB_REMOTE_OK", timeout=20)
    if res.get("ok") and "CCB_REMOTE_OK" in (res.get("stdout") or ""):
        return {"ok": True}
    err = res.get("error") or "ssh_failed"
    detail = (res.get("detail") or res.get("stderr") or res.get("stdout") or "").strip()
    out = {"ok": False, "error": err}
    if detail:
        out["detail"] = detail[-600:]
    return out

