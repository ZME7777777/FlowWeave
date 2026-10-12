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

OPS-07 已完成。18 项无 DB／Docker 依赖专项、Ruff check／format 和 diff 检查通过。
另修复进程 cwd 为 `/`／构建根祖先时误保护全部上下文的问题；真实文件引用与容器挂载祖先仍保护。
安装首次尝试发现远程源码入口分隔错误，原生 systemd 初装又发现未加载 unit 的 reset-failed 不适用；
两项均已修正并固定提交，失败阶段没有启用定时器，已触及 unit 均按安装回滚逻辑恢复。

服务器从 `2759ae4b3561b6d9d2cfb41ca478c059905d898c` 安装成功，重复安装同提交也通过。
脚本和 unit 的 SHA-256 与固定提交逐项一致；路径配置为 owner-only。timer 为 enabled／active，
首次及重复安装的实际服务均 Result=success、ExecMainStatus=0；计数报告明确为 apply，而非跳过。
当前正式上下文与回滚镜像候选均为零，没有重复计算 OPS-06 已释放空间。下一次触发为北京时间
2026-10-13 03:30:55；后续每天 03:30 起随机错峰。

原生 Linux／Python 3.9 隔离目录测试创建五份受管上下文，只读审计选出两份，实际仅删除最旧两份、
保留最近三份；该测试屏蔽真实镜像删除，仍使用真实 Docker revision、挂载与 /proc 引用保护，测试目录
已回收。全部生产容器 ID／image ID、卷清单和 Compose／env 摘要前后一致；API、stream-api、
Runtime Provider、PostgreSQL 健康，Worker／Web 运行，两 API health／readiness 通过。
没有发布或重启生产服务，没有删除持久数据；下一独立重构切片为 FR-571，本次没有开始。
