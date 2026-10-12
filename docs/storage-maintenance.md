# 原生 Linux 定时容量维护

OPS-07 在已有发布保留规则上增加服务器本地 systemd oneshot 与 timer。每天 Asia/Shanghai 03:30
触发，最多随机延后十分钟；服务器关机错过的执行在恢复后补跑。无需 Codex 常驻或 SSH 定时连接。
单次执行最多二十分钟，低 CPU／I/O 优先级；日志只包含候选数量、结果和可用空间，使用系统 journal
轮转并限制日志速率，不创建不断增长的独立日志文件。

## 清理边界

- 复用 `remote-release.py` 的受管 marker 和路径引用保护，保留每类最近三份成功发布上下文。
- 当前镜像、所有运行或退出容器、每个治理仓库最近三个 rollback 镜像及其源码 revision 均保护。
- 失败／未完成上下文至少保留七天；旧镜像无法映射 revision 时继续保护这些上下文。
- 只回收全部 tag 均属于 flowweave-platform／flowweave-web rollback 命名的过期镜像。
- 未知旧目录、符号链接、外部／候选 tag、共享 BuildKit、卷、Workspace、附件、会话、备份均保留。
- HOME 下载缓存通过 OPS-06 默认禁用持久写入治理，本任务不递归清空 HOME 或 IDE 内容。

与发布 prepare／finish 共用非阻塞锁；存在包安装／镜像构建时跳过。删除前再次核对容器、镜像、
挂载、进程路径引用、目录身份及 tag 指向；新增引用或身份变化立即停止。目录和镜像各超过 100 个
候选时整次拒绝。镜像仅精确去除治理 tag，禁止 force 或任何全局 prune。

## 安装与复验

先完整读取 deployment.md 与 local-build-and-deploy.md，再确认正式提交及受保护的本地配置：

```bash
python3 scripts/install-storage-maintenance.py --config .local/remote-deploy.env \
  --commit <包含定时维护工具的已提交 SHA>
```

安装工具先运行 other 范围正式预检，所有连接事实仅从本地私有配置读取。服务器侧生成权限 0600 的
最小路径配置，不持久化连接凭据；脚本与 unit 从目标提交读取，按固定 commit 安装。未知现有 unit
或安装目录拒绝覆盖。更新已有受管任务前保留 unit 权限受限备份，原子切换版本，失败只恢复此次任务；
安装历史仅保留当前和上一个固定版本。安装先执行只读候选审计，再实际运行一次服务并确认退出成功，
最后启用 timer。首次审计遇到活动构建或锁冲突会停止安装，待活动结束重新运行。

服务器侧可查看任务状态、下一执行时间及计数日志：

```bash
systemctl status flowweave-storage-maintenance.timer
systemctl show flowweave-storage-maintenance.service -p Result -p ExecMainStatus
journalctl -u flowweave-storage-maintenance.service -n 20 --no-pager
```

安装不会更新 Compose／env、部署平台／Runtime 镜像或重启生产服务。停用只需 disable --now 对应
maintenance.timer；不要使用 Docker 或数据清理替代停用。

## OPS-07 验收记录

实现专项与服务器安装验收完成后在此更新结果；FR-567 的并行迁移修复另行管理。
