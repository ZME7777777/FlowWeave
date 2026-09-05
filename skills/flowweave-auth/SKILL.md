---
name: flowweave-auth
description: 登录、检查或退出 FlowWeave CLI 用户会话；模型供应商 OAuth 转 flowweave-model-providers，网站凭据转 credential。
---

# FlowWeave CLI 用户登录

**开始前先完整阅读 `../flowweave/SKILL.md`。** 本 Skill 处理访问 FlowWeave 平台本身的用户会话，不处理网站凭据，也不处理模型供应商 OAuth。

## 登录与核对

先配置含真实部署前缀的基础 URL，再交互式登录。密码提示会隐藏输入：

```bash
flowweave config init --base-url https://host.example/flowweave
flowweave auth login
flowweave auth status
flowweave health --ready
```

非交互环境只能通过 `--password-stdin` 输入密码，并确保上游进程不会记录标准输入。不要使用不存在的 `--password` 参数，也不要把密码、Cookie 或 session token 写入命令、环境变量、JSON 请求、日志或 Skill。

CLI 将会话保存到独立的 `0600` 文件，并绑定登录时的规范化 base URL；切换平台地址后必须重新登录。HTTP、multipart 与 WebSocket 命令会自动使用会话，dry-run、`config show` 和 `auth status` 都不会回显 token。不得用 `-H`/`--header` 手工传 Cookie。

## 退出与失败处理

```bash
flowweave auth logout
```

退出会先撤销服务端会话，再删除本地认证文件。遇到 401 时先运行 `auth status`；会话过期或属于其他 base URL 时重新 `auth login`，不要复制浏览器 Cookie 或绕过平台认证。模型 Codex 设备授权使用 `flowweave-model-providers`，网站认证条目使用 `flowweave credential`。
