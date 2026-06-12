# Windows 上的 SSH 客户端设置

如果在 Windows 上添加远程机器时提示"未找到 SSH 客户端"，你有以下几个解决方案：

## 方案 1：安装 OpenSSH（推荐）

### Windows 10/11 图形界面

1. 打开**设置** → **应用** → **应用和功能** → **可选功能**
2. 点击**查看功能**（或直接搜索 OpenSSH）
3. 找到 **OpenSSH 客户端**，点击**安装**
4. 安装完成后，重启 CC Bridge 应用或服务即可自动检测

### 命令行安装（PowerShell 管理员）

```powershell
# Windows 10/11
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0

# 验证安装
ssh -V
```

## 方案 2：使用 WSL 中的 SSH

如果你已安装 WSL（Windows Subsystem for Linux），其中通常已内置 SSH：

1. 在 PowerShell 中验证 WSL 中的 SSH：
   ```powershell
   wsl which ssh
   ```
   
2. CC Bridge 会自动检测 WSL 中的 SSH，无需额外配置

## 方案 3：使用密钥认证（不需要 SSH 客户端）

> **重要提示**：若使用密钥认证，**不需要**本机有 SSH 客户端

CC Bridge 的密钥登录方式支持多种路径：
- 直接粘贴私钥内容
- 指定密钥文件路径（如 `~/.ssh/id_rsa`）
- 或通过 `vendor/` 目录提供的 `paramiko` 库

密钥认证方式下，即使检测不到 SSH 客户端，也能正常工作。

## 常见问题

### 安装后仍显示"未找到 SSH 客户端"

- **重启浏览器或 CC Bridge**：新安装的 SSH 可能需要重启应用才能被检测到
- **检查 PATH**：在 PowerShell 中运行 `ssh -V` 确认 SSH 可用
- **检查防火墙/安全软件**：某些安全软件可能阻止了 SSH 的检测

### 密码登录失败，建议使用密钥

即使检测到了 SSH 客户端，密码登录也可能因以下原因失败：

1. **远程机器不允许密码登录**：检查远程 `sshd_config` 中 `PasswordAuthentication` 的设置
2. **防火墙阻止**：确保本机能连接到远程 SSH 端口（默认 22）
3. **认证错误**：检查用户名和密码是否正确

**建议**：优先使用密钥认证，更安全且不受 SSH 客户端依赖的影响。

## 验证 SSH 连接

在本地 PowerShell 中测试连接：

```powershell
# 密钥认证
ssh -i C:\path\to\key user@remote-host

# 密码认证（会提示输入密码）
ssh user@remote-host
```

如果本机连接正常但 CC Bridge 报错，请检查：
- `config_manager.py` 中的 `_find_ssh_client()` 返回值
- 远程审计日志 `~/.ccb/remote_audit.log`

## 更多帮助

- [OpenSSH 官方文档](https://github.com/PowerShell/Win32-OpenSSH)
- [CC Bridge 远程诊断文档](./remote-diagnostics-plan.md)
