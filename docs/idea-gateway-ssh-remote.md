# Linux 服务器：IDEA / JetBrains Gateway SSH Remote

本指南让 JetBrains Gateway 通过 SSH 打开 FlowWeave 的**宿主机持久工作区**。它适用于新的和历史的 Agent 会话，以及 FlowRun 节点会话。Gateway 不连接可替换的 Runtime 容器，也不应打开容器内的 `/runtime/workspace/project`。

## 1. 前提

- FlowWeave 运行在目标 Linux 服务器，Docker Compose 正常运行；
- JetBrains Gateway 在开发者电脑上运行；
- 开发者电脑可通过 SSH 访问该 Linux 服务器；
- `.env` 中的 `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` 是 Linux 服务器上的绝对目录，且它正是 Compose 挂载给 API/Worker 的宿主机工作区根目录。

以下示例使用：

```text
服务器地址：flowweave.example.com
SSH 用户：flowweave
SSH 端口：22
工作区根目录：/srv/flowweave/workspaces
```

## 2. 在 Linux 服务器启用 SSH

Ubuntu/Debian：

```bash
sudo apt-get update
sudo apt-get install -y openssh-server
sudo systemctl enable --now ssh
sudo systemctl status ssh
```

RHEL、Rocky 或 CentOS：

```bash
sudo dnf install -y openssh-server
sudo systemctl enable --now sshd
sudo systemctl status sshd
```

从开发者电脑验证网络和账号：

```bash
ssh -p 22 flowweave@flowweave.example.com
```

若服务器有防火墙或云安全组，放行开发者电脑到 SSH 端口的 TCP 流量。

## 3. 配置 FlowWeave

在**Linux 服务器上的 FlowWeave 仓库根目录**编辑 `.env`：

```dotenv
FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT=/srv/flowweave/workspaces
IDE_SSH_HOST=flowweave.example.com
IDE_SSH_USER=flowweave
IDE_SSH_PORT=22
```

要求：

- `IDE_SSH_HOST` 必须是运行 JetBrains Gateway 的电脑能访问到的服务器 IP 或域名，不能填 Docker 容器名；
- `IDE_SSH_USER` 必须是实际拥有 SSH 登录权限、并能读取工作区目录的 Linux 用户；
- `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` 必须与现有部署使用的宿主机持久工作区根目录一致。不得为了 IDEA 新建一个不同目录，否则历史会话不会出现在其中。

确认目录存在，并确认 SSH 用户至少有读取和进入目录的权限：

```bash
sudo mkdir -p /srv/flowweave/workspaces
sudo chown -R flowweave:flowweave /srv/flowweave/workspaces
sudo -u flowweave find /srv/flowweave/workspaces -maxdepth 1 -type d -print
```

如果运行 FlowWeave 的 Docker 服务使用不同的宿主机 UID/GID，按现有部署的目录所有权策略处理；不要递归修改不属于 FlowWeave 的目录。

## 4. 重启受影响服务

配置变更后，在 Linux 服务器仓库根目录执行：

```bash
docker compose --env-file .env -f infra/compose.yaml up -d --no-deps --force-recreate api worker
docker compose --env-file .env -f infra/compose.yaml ps api worker
curl -fsS http://127.0.0.1:8080/health
```

预期 API 返回：

```json
{"status":"ok"}
```

## 5. 在 JetBrains Gateway 打开会话工作区

1. 刷新 FlowWeave 页面，打开目标 Agent 会话或 FlowRun 节点会话。
2. 右侧“IDEA / Gateway”区域应显示 SSH 命令和一个以 `/srv/flowweave/workspaces/` 开头的目录。
3. 在 JetBrains Gateway 选择 **SSH**，填写该区域给出的主机、端口和用户，完成认证。
4. 选择 **Open**，粘贴页面显示的**宿主机目录**。

历史会话可以直接打开：只要其持久工作区没有被删除，页面给出的宿主机目录仍对应相同的项目文件。

不要在 Gateway 中打开 `/runtime/workspace/project`。它仅在 Runtime 容器内存在，Runtime generation 被替换后该容器随时可能消失。

## 6. 故障排查

| 现象 | 检查方式 | 处理 |
|---|---|---|
| 页面显示“未配置 SSH Remote” | `docker compose ... exec api env | grep '^IDE_SSH'` | 检查 `.env` 的三项 `IDE_SSH_*`，然后重建 API 和 Worker。 |
| Gateway 无法连接 | `ssh -p <port> <user>@<host>` | 检查 SSH 服务、防火墙、安全组、DNS 和账号认证。 |
| Gateway 连接后目录不存在 | `sudo -u <user> ls -la <页面显示的目录>` | 确认 `FLOWWEAVE_RUNTIME_HOST_WORKSPACE_ROOT` 未改变，且工作区未被清理。 |
| Gateway 能连接但无法读写 | `sudo -u <user> touch <页面显示目录>/.gateway-write-test && rm ...` | 修复该 FlowWeave 工作区的 Linux 用户/组权限；不要通过容器内路径规避权限。 |

## 7. 本机 macOS 开发说明

本机 Docker 开发时，服务器就是 macOS 宿主机，`IDE_SSH_HOST` 应为 `127.0.0.1`，用户为本机用户名，工作区根目录为本机 `.env` 中已有的绝对路径。另需在“系统设置 → 通用 → 共享 → 远程登录”启用 SSH（或运行 `sudo systemsetup -setremotelogin on`）。这只用于本机验证；部署到 Linux 后必须改为 Linux 服务器的实际 SSH 信息。
