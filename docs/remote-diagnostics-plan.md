# 远程诊断方案：让 server 端 AI agent 操作无 Claude 的目标机器

> 状态：设计方案（未实现）。目的是让运行在 server 机器上的 AI agent（ccb/claude CLI）
> 能够查看/操作另一台**没有安装 Claude 等工具**的目标机器，用于排查日志、执行诊断命令。

---

## 1. 背景与目标

- **现状**：AI agent 跑在 server 机器上（即本项目 cc-bridge 所在机器），工具调用都在 server 本地执行。
- **诉求**：目标机器（target）出了问题，但上面没有 Claude/ccb，也不方便安装。希望 server 上的 agent
  能去 target 上查日志、跑命令，辅助定位问题。
- **目标**：
  - target 端**零安装**或极轻量（不装 Claude，最好不装任何自研程序）。
  - agent 的远程操作**显式、可审计、可控权限**。
  - 与 cc-bridge 现有架构与"Python 标准库、subprocess 包装 CLI"的风格一致。

---

## 2. 核心约束（为什么不能"直接实现"）

在 cc-bridge 里，`server.py` 只做三件事：拉起 `ccb/claude -p` 子进程、把用户输入写进 stdin、
转发 stdout 的 stream-json。**真正执行 Bash/Read/Glob 等工具的是 CLI 自己**，server 看不到也拦不住。

因此无法"把本地 Bash 偷偷改成在远端跑"。正确做法是给 agent 一条**显式的远程执行通道**，让它知道
"这些工具作用在 target 上"，并主动调用。这条通道就是下面方案的核心。

---

## 3. 推荐方案：SSH 通道 + MCP 桥接

### 3.1 总体架构

```
┌─────────────── server 机器 ───────────────┐         ┌──── target 机器 ────┐
│                                            │         │                     │
│  浏览器 ── cc-bridge server ── ccb/claude CLI │         │   sshd (系统自带)    │
│                                  │         │   SSH   │      │              │
│                          remote-bridge ────┼────────▶│   cmd/pwsh/bash      │
│                          (MCP server)      │         │   日志、命令、文件     │
└────────────────────────────────────────────┘         └─────────────────────┘
```

- **remote-bridge**：一个小型 **MCP server**（Python 标准库实现，JSON-RPC over stdio，
  风格与 `server.py` 手写 HTTP 一致）。它向 CLI 暴露一组"远程诊断工具"。
- 每个远程工具的实现，就是**在 server 上 `subprocess` 调用系统的 `ssh` / `scp`**，把命令送到 target 执行，
  拿回 stdout/stderr/退出码。
- Claude Code CLI 原生支持加载 MCP server（`settings.json` 的 `mcpServers` 或 `.mcp.json`）。
  agent 会看到 `mcp__remote__run` 这类工具，调用即作用于 target。

### 3.2 为什么是这个组合

- **target 零安装**：只需开启系统自带的 SSH 服务（见 §4），不装任何自研程序、不装 Claude。
- **server 零三方依赖**：bridge 只是 `subprocess` 调 `ssh`，延续本项目"包装 CLI"的模式，
  不引入 paramiko 等库（Windows 10+ / 各 Unix 都自带 `ssh` 客户端）。
- **agent 友好**：远程工具是显式的、带语义的（"读远端文件""tail 远端日志"），
  比让 agent 自己拼 `ssh host "..."` 更稳，引号转义、超时、权限都由 bridge 统一兜底。
- **可审计/可控**：所有远程命令都过 bridge 这一个咽喉，天然适合加日志、加白名单、加只读开关。

---

## 4. 目标机器接入方式（按操作系统）

| 目标 OS | 接入方式 | 说明 |
|--------|---------|------|
| Linux / macOS | OpenSSH `sshd` | 几乎都自带，密钥登录即可 |
| Windows 10/11、Server 2019+ | **OpenSSH Server**（系统可选功能） | 无需第三方；`ssh user@host "powershell -Command ..."` |
| Windows（企业域环境） | WinRM / PowerShell Remoting | `Invoke-Command`，但配置与认证更重，作为备选 |

推荐统一走 **OpenSSH**：跨平台一致，bridge 实现最简单（一种 transport 通吃）。
Windows target 默认 sshd shell 是 cmd，可在命令里显式 `powershell -Command "..."` 来跑 PowerShell。

---

## 5. 远程工具集（只读优先）

第一期只做**只读诊断**，把"能查问题"做扎实，把"能改系统"的风险隔离开。

**只读类（默认启用）**
- `remote_run(command, timeout)` —— 在 target 执行命令并返回 stdout/stderr/exit_code（受白名单约束，见 §7）
- `remote_read_file(path, max_bytes)` —— 读取远端文件（cat / Get-Content）
- `remote_tail(path, lines)` —— tail 远端日志末尾 N 行
- `remote_list(path)` —— 列目录
- `remote_grep(pattern, path)` —— 远端搜索日志关键字
- `remote_sysinfo()` —— 一键采集：OS 版本、负载、磁盘、内存、关键服务状态、最近报错
- `remote_fetch(path)` —— 把远端文件拉回 server（scp）供 agent 进一步分析

**变更类（默认禁用，需显式开启 + 二次确认）**
- `remote_exec_mutating(command)` —— 允许重启服务、改配置等。仅在用户为该会话显式开启"允许变更"后可用，
  且每条都走 Claude Code 权限确认。

---

## 6. 与 cc-bridge 的集成

- **目标机器配置**：在侧栏/设置里新增"远程目标"管理：`host / port / user / 认证方式（密钥路径）/ OS 类型 / 只读或可变更`。
  存到 `~/.ccb/remote_targets.json`（延续 GUI 偏好持久化模式）。
- **会话绑定 target**：新建会话时可选一个目标机器。cc-bridge 把当前选中的 target 写入
  `~/.ccb/current_target.json`，bridge 启动时读取它（或每个工具调用带 `target` 参数）。
  这样同一个 bridge 能服务不同 target，而不必为每台机器配一份 MCP。
- **连接状态**：设置里加"测试连接"按钮（实际就是 `ssh host echo ok`），显示绿/红状态，类似现有 CLI 检测。
- **远程命令审计面板**：在对话区或独立面板，实时显示 bridge 执行过的每条远程命令、退出码、耗时——
  既是审计，也帮用户看清 agent 到底在 target 上做了什么。
- **提示词引导**：为绑定了 target 的会话，自动注入一段说明（system/CLAUDE.md 风格）：
  "本会话的诊断对象是远程机器 X，请使用 `mcp__remote__*` 工具操作它，不要用本地 Bash。"

---

## 7. 安全模型（重点）

远程命令执行是高敏感能力，方案必须自带护栏：

1. **认证**：只用 **SSH 密钥**，禁用密码登录；密钥放在 server 用户目录、权限收紧，**不进仓库**。
2. **最小权限**：target 上为诊断单独建一个低权限账号，只读相关日志/目录；能 sudo 的范围用
   `sudoers` 精确限制到具体诊断命令。
3. **默认只读**：bridge 默认只放行只读工具；变更类工具需用户对该会话显式开启。
4. **命令白名单 / 黑名单**：bridge 内置允许的命令前缀（如 `tail/cat/grep/systemctl status/journalctl/Get-*`），
   拦截危险命令（`rm -rf`、`shutdown`、`format`、管道写文件等）。白名单可配置。
5. **二次确认**：变更类命令走 Claude Code 既有权限确认机制，不"跳过权限"。
6. **全量审计日志**：每条远程命令连同时间、target、命令、退出码、调用者会话写入 server 端审计文件，
   不可被 agent 关闭。
7. **超时与配额**：每条命令设超时、输出大小上限（避免拉爆内存，类似现有 stdout 缓冲限制思路）；
   可加频率限制。
8. **网络面**：target 的 sshd 仅对 server IP 开放（防火墙/安全组），不暴露公网；
   有条件走跳板机或内网专线。
9. **凭证隔离**：bridge 不把密钥/口令回显给 agent；agent 只看到命令结果，看不到连接凭证。

---

## 8. 凭证管理

- SSH 私钥存 `~/.ssh/`，在 `remote_targets.json` 里只存**密钥路径**而非密钥内容。
- 多 target 多账号时，用 `~/.ssh/config` 的 Host 别名管理，bridge 直接 `ssh <别名>`，
  连接细节交给 OpenSSH，bridge 不碰明文凭证。
- 审计文件、target 配置都落在用户目录，纳入 `.gitignore`，绝不入库。

---

## 9. 备选方案与取舍

| 方案 | 做法 | 优点 | 缺点 / 适用 |
|------|------|------|------------|
| **A. SSH + MCP 桥接（推荐）** | bridge 暴露远程工具，内部 shell out 到 `ssh` | target 零安装、agent 友好、可审计、零三方依赖 | 需要实现一个 MCP server；target 要开 sshd |
| B. 提示词 + ssh 包装脚本 | 给 agent 一个 `rexec "<cmd>"` 脚本（内部 ssh），靠 CLAUDE.md 要求它都走这个 | 实现最快，几乎不写代码 | 不稳：agent 可能忘记、引号/交互命令易出错、难收口权限。适合临时验证 |
| C. target 端轻量执行器 | 在 target 跑一个 stdlib 小程序，server 用 HTTP/socket 下发命令 | 不依赖 sshd；可走反向隧道穿透入站限制 | 要在 target 部署东西，违背"零安装"；自研 RCE 通道安全负担更重 |
| D. WinRM / PowerShell Remoting | Windows 原生 `Invoke-Command` | 纯 Windows 车队体验好 | 跨平台不统一、配置认证偏重；作为 Windows 专项备选 |
| E. 成熟运维工具 | Ansible（agentless，走 SSH/WinRM）、AWS SSM、SaltStack 等 | 生产级、成熟生态 | 偏重、超出本项目轻量定位；可在大规模车队时考虑，bridge 也可改为调用它们 |

**结论**：以 **A** 为主线落地；**B** 可作为最小验证的过渡；Windows 重场景再叠加 **D**；
大规模车队再考虑 **E**。

---

## 10. 分阶段落地

- **Phase 0（验证，~0.5 天）**：用方案 B，手动配好 `ssh` 免密，给 agent 一个 `rexec` 脚本 + CLAUDE.md 说明，
  跑通"查一台 Linux 的 `journalctl` / `tail` 日志"，确认价值与体验。
- **Phase 1（MVP）**：实现 remote-bridge MCP server（Python 标准库），只做**只读工具**（§5 只读类），
  单 target、密钥登录、命令白名单、审计日志。Linux/macOS 优先。
- **Phase 2（集成）**：cc-bridge 加"远程目标"配置 UI、会话绑定 target、连接测试、远程命令审计面板。
- **Phase 3（Windows & 变更能力）**：支持 Windows target（OpenSSH Server），加变更类工具 + 二次确认 + sudo 收口。
- **Phase 4（车队）**：多 target 选择、批量诊断、（可选）对接 Ansible/SSM。

---

## 11. 待决策问题

落地前需要先明确，会影响方案细节：

1. **target 是什么系统？** Linux / Windows / 混合？（决定 transport 与 shell 处理）
2. **target 能否开 SSH？** 还是网络被锁、只能反向连出（影响选 A 还是 C）。
3. **单台还是车队？** 只查一两台，还是要管理一批机器。
4. **只读诊断，还是也要远程修复（变更）？** 决定是否需要 Phase 3 的变更能力与更强护栏。
5. **谁拥有 target？** 是否同一管理员授权——远程执行务必在已授权、自有/受管的机器上进行。
