# 定时任务北京时间迁移

2026-10-23，代码提交 `91309177052452dd38d811e94d4e898ea8d9f7f1`。

Agent 会话与 FlowRun 定时任务均以 `Asia/Shanghai` 的年月日、星期和时分匹配五段
Cron；数据库继续保存 UTC 时间点。两个定时任务页面固定按北京时间显示时间，
仅替换现有 UTC 提示，不新增说明区。Agent 日与星期字段同步采用标准 Cron 匹配规则。

`0158_schedule_china_timezone` 在同一事务内锁定两类任务表，并临时使用迁移级租户
旁路处理所有用户的任务。启用和暂停任务的表达式均转换为北京时间，增加行版本，
保留原 `next_run_at`、`last_run_at`、状态及执行记录。例如 `0 2 * * *` 转为
`0 10 * * *`，实际运行时刻不变。星期跨天同步移动；无法用单个五段 Cron 精确保留
的月末或混合星期规则会使整次迁移失败回滚，不能只修改小时。

验证结果：33 项 Python 检查、3 项 Chromium 定向回归通过，包括浏览器处于纽约时区
时仍显示北京时间、Cron 原值提交、跨日／周末／月末／年末和标准日／星期语义。
PostgreSQL 临时表执行正式 upgrade／downgrade 与失败回滚，覆盖两类表的 10 条任务；
该检查未修改持久业务数据。Web typecheck、定向 ESLint、production build、Ruff、
diff 检查通过。Agent schedules 的严格 Pyright 仍有两项既有私有符号使用诊断，
与父提交逐项一致，无新增诊断。

正式发布使用该提交的 platform／web 最小归档，通过预检并构建 linux/amd64 镜像。
切换前停止旧 API／stream-api／Worker，避免旧 UTC 代码读取已转换表达式；正式迁移
退出码为零，数据库版本为 `0158_schedule_china_timezone`。4 个已有任务全部转换，
逐条核对原定时间点、暂停状态、上次运行及执行记录数量保持一致；随后恢复新服务。
api／stream-api／worker／web 的 revision 与健康检查通过，Runtime Provider 身份不变。

真实 Chromium 经受保护的临时隧道访问实际 Web 服务，深层定时任务路由、登录页、
带部署前缀的 API 请求与未认证 401 边界通过。本机配置的历史公网入口仍返回 502，
未将直连结果记为该历史公网入口恢复；未修改入口配置。

platform／web 的 `remote-release-finish` 均完成，按引用保护回收三份过期受管上下文，
保留最近三份成功上下文及所有引用版本；精确镜像删除候选为零。
